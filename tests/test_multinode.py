import copy
import http.client
import json
import os
import secrets
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from proxy_stack import render, rules
from proxy_stack.control import Hub, Server, decode_code, join, origin, request
from proxy_stack.model import Settings
from proxy_stack.runtime import Runtime, validate_desired
from proxy_stack.storage import Store, atomic_file


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'hub')
        self.settings = Settings('hub.example.com', 'Hub', local_node=False, lease_seconds=10)
        self.original = self.store.init(self.settings)
        self.hub = Hub(self.store)

    def enroll(self, address='node.example.com', name='Node'):
        code = decode_code(self.hub.code())
        data = {'secret': code['secret'], 'settings': asdict(Settings(address, name, role='node')),
                'ws_path': '/ws/' + 'b' * 48,
                'peer': {'id': secrets.token_hex(24), 'token': secrets.token_hex(24)}}
        peer = self.hub.enroll(data)
        return peer, data

    def auth(self, peer):
        return 'Bearer ' + peer['id'] + ':' + peer['token']

    def exchange(self, peer, action='poll', generation=0):
        return self.hub.exchange(action, {'generation': generation}, self.auth(peer))

    def config(self, user='owner'):
        state = self.store.state()
        return json.loads((self.store.current() / 'sub' / state['users'][user]['token'] / 'clash.yaml').read_text())

    def test_hub_only_has_zero_nodes_and_actionable_qx(self):
        self.assertEqual(self.config()['proxies'], [])
        state = self.store.state()
        text = (self.store.current() / 'sub' / state['users']['owner']['token'] / 'qx/full.conf').read_text()
        self.assertIn('Ready nodes: 0', text)
        self.assertNotIn('localhost:443', text)
        self.assertEqual(self.config()['proxy-groups'][0]['proxies'], ['REJECT'])

    def test_one_use_expiry_tamper_and_scoped_identity(self):
        peer, data = self.enroll()
        with self.assertRaises(PermissionError):
            self.hub.enroll(data)
        with self.assertRaises(PermissionError):
            self.hub.enroll(dict(data, secret='0' * 48))
        with patch('proxy_stack.control.time.time', return_value=time.time() + 4000):
            expired = decode_code(self.hub.code())
        # Create a currently valid code, then move past its TTL.
        expired = decode_code(self.hub.code())
        with patch('proxy_stack.control.time.time', return_value=time.time() + 700):
            with self.assertRaises(PermissionError):
                self.hub.enroll(dict(data, secret=expired['secret']))
        desired = self.exchange(peer)
        self.assertEqual(set(peer), {'id', 'token'})
        self.assertEqual(set(desired), {'generation', 'lease_seconds', 'ws_path', 'users'})
        self.assertNotIn(self.original['users']['owner']['token'], json.dumps(desired))
        stored = self.store.state()['nodes'][peer['id']]
        self.assertNotEqual(stored['auth'], peer['token'])
        for auth in (None, '', self.auth(peer) + 'x', 'Bearer ' + peer['id'] + ':' + '0' * 48,
                     'Bearer ' + '0' * 48 + ':' + peer['token']):
            with self.assertRaises(PermissionError):
                self.hub.exchange('poll', {'generation': 0}, auth)
        for bad in ('not-base64', 'a' * 1025):
            with self.assertRaises(ValueError):
                decode_code(bad)
        for bad in ('http://hub.example.com', 'https://127.0.0.1', 'https://hub.example.com/path',
                    'https://a:b@hub.example.com', 'https://hub.example.com?x=1', 'https://hub.example.com:443'):
            with self.assertRaises(ValueError):
                origin(bad)

    def test_aggregate_endpoints_hub_resources_independent_users(self):
        peers = [self.enroll('8.8.8.8', 'IPv4')[0], self.enroll('2606:4700:4700::1111', 'IPv6')[0],
                 self.enroll('node.example.com', 'Domain')[0]]
        self.store.mutate_user('add', 'alice')
        self.store.mutate_user('add', 'bob')
        for peer in peers:
            desired = self.exchange(peer)
            self.assertEqual(self.config()['proxies'], [])
            # Remaining peers are still pending; this one becomes visible alone.
            self.exchange(peer, 'ack', desired['generation'])
            break
        for peer in peers[1:]:
            desired = self.exchange(peer)
            self.exchange(peer, 'ack', desired['generation'])
        owner, alice = self.config(), self.config('alice')
        self.assertEqual([n['server'] for n in alice['proxies']], ['8.8.8.8', '2606:4700:4700::1111', 'node.example.com'])
        self.assertEqual(alice['proxy-groups'][0]['proxies'], ['IPv4', 'IPv6', 'Domain'])
        self.assertTrue(all(n['uuid'] == self.store.state()['users']['alice']['uuid'] for n in alice['proxies']))
        self.assertNotEqual(alice['proxies'][0]['uuid'], owner['proxies'][0]['uuid'])
        self.assertTrue(all(p['url'].startswith('https://hub.example.com/' + self.store.state()['users']['alice']['token'] + '/rules/') for p in alice['rule-providers'].values()))
        base = self.store.current() / 'sub' / self.store.state()['users']['alice']['token']
        bootstrap = json.loads((base / 'rules/bootstrap.yaml').read_text())['payload']
        for entry in ('DOMAIN,hub.example.com', 'DOMAIN,node.example.com', 'IP-CIDR,8.8.8.8/32,no-resolve',
                      'IP-CIDR6,2606:4700:4700::1111/128,no-resolve'):
            self.assertIn(entry, bootstrap)
        qx = (base / 'qx/nodes.txt').read_text()
        self.assertEqual(len(qx.splitlines()), 3)
        self.assertIn('vless=[2606:4700:4700::1111]:443', qx)
        self.assertIn('static = VPS, IPv4, IPv6, Domain', (base / 'qx/full.conf').read_text())

    def test_revocation_ack_removal_and_no_heartbeat_render(self):
        peer, _ = self.enroll()
        desired = self.exchange(peer)
        self.exchange(peer, 'ack', desired['generation'])
        current = self.store.current()
        self.exchange(peer, generation=desired['generation'])
        self.exchange(peer, 'ack', desired['generation'])
        self.assertEqual(self.store.current(), current)
        self.store.mutate_user('add', 'alice')
        owner = self.store.state()['users']['owner']
        self.assertEqual(self.hub.status()[0]['status'], 'pending')
        with self.assertRaises(ValueError):
            self.exchange(peer, 'ack', desired['generation'])
        updated = self.exchange(peer)
        self.exchange(peer, 'ack', updated['generation'])
        alice = self.store.state()['users']['alice']
        self.store.mutate_user('revoke', 'alice')
        self.assertFalse((self.store.current() / 'sub' / alice['token']).exists())
        self.assertEqual(self.hub.status()[0]['status'], 'pending')
        desired = self.exchange(peer, generation=updated['generation'])
        self.assertNotIn(alice['uuid'], desired['users'])
        self.assertEqual(self.store.state()['users']['owner'], owner)
        self.exchange(peer, 'ack', desired['generation'])
        self.hub.remove(peer['id'])
        self.assertEqual(self.config()['proxies'], [])
        with self.assertRaises(PermissionError):
            self.exchange(peer)
        self.assertEqual(self.hub.status()[0]['status'], 'removed')

    def test_offline_reaper_and_recovery(self):
        peer, _ = self.enroll()
        self.exchange(peer)
        self.exchange(peer, 'ack', 1)
        with patch('proxy_stack.control.time.time', return_value=time.time() + 11):
            self.assertEqual(self.hub.status()[0]['status'], 'offline')
            self.hub.reap()
            self.assertEqual(self.config()['proxies'], [])
        self.exchange(peer)
        self.exchange(peer, 'ack', 1)
        self.assertEqual(self.hub.status()[0]['status'], 'synced')

    def test_validation_limits_and_duplicates(self):
        peer, data = self.enroll()
        for bad in ({}, {'generation': True}, {'generation': -1}, {'generation': 1, 'extra': True}):
            with self.assertRaises(ValueError):
                self.hub.exchange('poll', bad, self.auth(peer))
        code = decode_code(self.hub.code())
        with self.assertRaises(ValueError):
            self.hub.enroll(dict(data, secret=code['secret']))
        with self.assertRaises(ValueError):
            self.hub.enroll(dict(data, secret=code['secret'], ws_path='/arbitrary'))
        for bad in ({}, {'generation': 1, 'lease_seconds': 0, 'users': [], 'ws_path': data['ws_path']}):
            with self.assertRaises(ValueError):
                validate_desired(bad, data['ws_path'])

    def test_http_auth_schema_and_body_bounds(self):
        peer, _ = self.enroll()
        server = Server(('127.0.0.1', 0), self.hub)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def post(path, body, headers=None):
            conn = http.client.HTTPConnection(*server.server_address, timeout=2)
            try:
                conn.request('POST', path, body, headers or {'Content-Type': 'application/json'})
                res = conn.getresponse()
                res.read()
                return res.status
            finally:
                conn.close()
        self.assertEqual(post('/control/v1/poll', '{"generation":0}'), 403)
        headers = {'Content-Type': 'application/json', 'Authorization': self.auth(peer)}
        self.assertEqual(post('/control/v1/poll', '{"generation":0}', headers), 200)
        self.assertEqual(post('/control/v1/poll', '{}', headers), 400)
        self.assertEqual(post('/control/v1/poll', 'x' * 65537, headers), 413)
        self.assertEqual(post('/control/v1/poll?secret=x', '{}', headers), 404)
        self.assertEqual(post('/control/v1/anything', '{}', headers), 404)
        self.assertEqual(post('/control/v1/poll', '{}', {'Content-Type': 'text/plain'}), 415)
        self.assertEqual(post('/control/v1/poll', 'garbage', headers), 400)

    def test_lost_enrollment_response_recovers_same_scoped_identity(self):
        import urllib.error
        node = Store(Path(self.temp.name) / 'joined')
        node.init(Settings('node.example.com', 'Joined', role='node'))
        code = self.hub.code()
        def transport(origin, action, data, auth=None):
            try:
                return self.hub.enroll(data) if action == 'enroll' else self.hub.exchange(action, data, auth)
            except PermissionError:
                raise urllib.error.HTTPError(origin,403,'unauthorized',None,None)
        def lost_response(origin, action, data, auth=None):
            transport(origin, action, data, auth)
            raise OSError('response lost after enrollment committed')
        with patch('proxy_stack.control.request', side_effect=lost_response):
            with self.assertRaises(OSError):
                join(node, code)
        pending = node.read(node.model / 'pending-peer.json')
        import base64
        other_code = base64.urlsafe_b64encode(json.dumps({'origin':'https://other.example.com','secret':'f'*48}).encode()).decode()
        with patch('proxy_stack.control.request') as transport_mock:
            with self.assertRaisesRegex(ValueError,'another hub'):
                join(node,other_code)
            transport_mock.assert_not_called()
        self.assertEqual(node.read(node.model/'pending-peer.json'),pending)
        with patch('proxy_stack.control.request', side_effect=transport):
            join(node, code)
        self.assertEqual(node.read(node.model / 'peer.json'), pending)
        self.assertFalse((node.model / 'pending-peer.json').exists())
        self.assertEqual(len(self.hub.status()), 1)
        self.assertNotIn(pending['token'], json.dumps(self.store.state()))
        # Explicit detach also clears pending enrollment when its hub is offline.
        (node.model/'peer.json').rename(node.model/'pending-peer.json')
        from proxy_stack.cli import main
        import contextlib,io
        output=io.StringIO()
        with patch.dict(os.environ,{'PROXY_STACK_DATA':str(node.root)}), patch('proxy_stack.control.request',side_effect=OSError()), contextlib.redirect_stdout(output):
            self.assertEqual(main(['detach']),0)
        self.assertFalse((node.model/'pending-peer.json').exists())
        self.assertIn('pending',output.getvalue())
        self.assertNotIn(pending['token'],output.getvalue())

    def test_https_client_uses_trust_no_proxy_or_redirect_and_bounds(self):
        import io
        with patch('proxy_stack.control.urllib.request.build_opener') as build:
            response = io.BytesIO(b'{"ok":true}')
            response.status = 200
            build.return_value.open.return_value = response
            self.assertEqual(request(self.settings.origin,'poll',{'generation':0}),{'ok':True})
            handlers = build.call_args.args
            self.assertEqual(handlers[0].proxies,{})
            self.assertEqual(handlers[2]._context.verify_mode,__import__('ssl').CERT_REQUIRED)
            self.assertTrue(handlers[2]._context.check_hostname)
            self.assertIsNone(handlers[1].redirect_request(None,None,None,None,None,None))
            self.assertEqual(build.return_value.open.call_args.kwargs,{'timeout':10})
        with self.assertRaises(ValueError):
            request('http://hub.example.com','poll',{'generation':0})

    def test_slow_rule_download_does_not_block_node_or_user_mutations(self):
        peer, _ = self.enroll()
        entered, release = threading.Event(), threading.Event()
        def fetch(name):
            entered.set()
            if not release.wait(5):
                raise TimeoutError()
            return rules.bootstrap()['lists'][name]
        with ThreadPoolExecutor(max_workers=2) as pool:
            updater = pool.submit(self.store.update, fetch)
            self.assertTrue(entered.wait(2))
            try:
                mutation = pool.submit(self.store.mutate_user,'add','alice')
                mutation.result(timeout=2)
                desired = self.exchange(peer)
                self.assertIn(self.store.state()['users']['alice']['uuid'],desired['users'])
            finally:
                release.set()
            updater.result(timeout=5)
        self.assertIn('alice',self.store.state()['users'])
        self.assertIn(peer['id'],self.store.state()['nodes'])

    def test_concurrent_updater_user_and_peer_do_not_lose_state(self):
        peer, _ = self.enroll()
        def update():
            self.store.update(lambda name: rules.bootstrap()['lists'][name])
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(update), pool.submit(self.store.mutate_user, 'add', 'alice'),
                       pool.submit(self.exchange, peer)]
            for future in futures:
                future.result(timeout=10)
        self.assertIn('alice', self.store.state()['users'])
        self.assertIn(peer['id'], self.store.state()['nodes'])
        self.assertEqual(self.store.health()['rules'], 'upstream')



class FakeSupervisor:
    def __init__(self):
        self.lock = threading.RLock()
        self.generation = 0
        self.running = False
        self.fail = False

    def alive(self):
        return self.running

    def stop(self, force=False):
        self.running, self.generation = False, 0

    def apply(self, desired, on_start=lambda: None):
        if self.fail:
            raise RuntimeError('startup failed')
        on_start()
        self.generation, self.running = desired['generation'], True


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'runtime')
        self.state = self.store.init(Settings('node.example.com', 'Local', lease_seconds=10))
        self.supervisor = FakeSupervisor()
        self.now = 100
        self.runtime = Runtime(self.store, self.supervisor, lambda: self.now)

    def test_start_failure_never_acknowledged(self):
        self.supervisor.fail = True
        self.runtime.tick()
        node = self.store.state()['nodes']['local']
        self.assertFalse(node['ready'])
        self.assertEqual(node['acked'], 0)
        self.assertEqual(self.supervisor.generation, 0)
        self.supervisor.fail = False
        self.runtime.tick()
        self.assertTrue(self.store.state()['nodes']['local']['ready'])

    def test_lease_expiry_stops_last_good_and_restart_needs_fresh_poll(self):
        self.runtime.tick()
        self.assertTrue(self.supervisor.alive())
        with patch.object(self.runtime, 'exchange', side_effect=OSError()):
            self.now += 5
            self.runtime.tick()
            self.assertTrue(self.supervisor.alive())
            self.now += 6
            self.runtime.tick()
            self.assertFalse(self.supervisor.alive())
        self.runtime.tick()
        restarted = Runtime(self.store, self.supervisor, lambda: self.now)
        with patch.object(restarted, 'exchange', side_effect=OSError()):
            restarted.tick()
        self.assertFalse(self.supervisor.alive())

    def test_watchdog_stops_child_while_poll_thread_is_blocked(self):
        runtime = Runtime(self.store,self.supervisor)
        runtime.tick()
        runtime.deadline = time.monotonic() + .15
        entered, release, stop = threading.Event(), threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            release.wait(3)
            raise OSError()
        with patch.object(runtime,'exchange',side_effect=blocked):
            poller = threading.Thread(target=runtime.tick)
            guard = threading.Thread(target=runtime.guard,args=(stop,))
            poller.start()
            guard.start()
            try:
                self.assertTrue(entered.wait(1))
                deadline = time.monotonic()+1
                while self.supervisor.alive() and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertFalse(self.supervisor.alive())
                self.assertTrue(poller.is_alive())
                self.assertFalse(self.store.read(self.store.runtime/'node-status.json')['running'])
            finally:
                release.set()
                stop.set()
                poller.join(3)
                guard.join(3)

    def test_removed_node_stops_without_waiting_lease(self):
        self.runtime.tick()
        Hub(self.store).remove('local')
        self.runtime.tick()
        self.assertFalse(self.supervisor.alive())

    def test_invalid_or_stale_desired_never_acked(self):
        self.runtime.tick()
        desired = self.runtime.exchange('poll', 1)
        for changes in ({'users': []}, {'users': ['bad']}, {'ws_path': '/ws/' + 'a' * 48},
                        {'generation': True}, {'lease_seconds': 99999}):
            with self.assertRaises(ValueError):
                validate_desired(dict(desired, **changes), self.state['ws_path'])
