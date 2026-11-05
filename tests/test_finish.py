"""Completion must prove the own HTTPS asset and a live acknowledged node."""
import contextlib
import io
import json
import os
import ssl
import tempfile
import time
import unittest
import urllib.error
from dataclasses import asdict
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from proxy_stack import finish
from proxy_stack.cli import main
from proxy_stack.control import Hub
from proxy_stack.model import Settings
from proxy_stack.routes import labelled_paths
from proxy_stack.storage import Store, atomic_file


class Response(io.BytesIO):
    status = 200

    def __init__(self, body, mime='application/yaml', cache='no-store'):
        super().__init__(body)
        self.headers = Message()
        self.headers['Content-Type'] = mime
        self.headers['Cache-Control'] = cache


class FinishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name).resolve() / 'data')
        self.store.init(Settings('proxy.example.com', 'Test', mode='existing-caddy', lease_seconds=10))
        self.opener = patch('proxy_stack.finish.urllib.request.build_opener').start()
        self.addCleanup(patch.stopall)
        self.opener.return_value.open.side_effect = lambda *_a, **_k: Response(self.asset())

    def asset(self):
        token = self.store.state()['users']['owner']['token']
        return (self.store.current() / ('sub/' + token + '/clash.yaml')).read_bytes()

    def ack(self):
        hub = Hub(self.store)
        generation = hub.exchange('poll', {'generation': 0}, local=True)['generation']
        hub.exchange('ack', {'generation': generation}, local=True)

    def run_finish(self, wait=0):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = finish.finish(self.store, wait)
        return result, output.getvalue()

    def assert_private(self, output):
        user = self.store.state()['users']['owner']
        for secret in (user['token'], user['uuid'], '/sub/'):
            self.assertNotIn(secret, output)

    def test_verified_own_asset_and_ack_happy_path(self):
        self.store = Store(self.store.root.parent / 'custom-port')
        self.store.init(Settings('proxy.example.com', 'Test', port=34567))
        self.ack()
        code, output = self.run_finish()
        self.assertEqual(code, 0, output)
        self.assertIn('订阅就绪：HTTPS 内容已校验；已确认节点 1', output)
        self.assertIn(self.store.state()['users']['owner']['token'], output)
        self.assertEqual(output.count('https://'), 5)
        token = self.store.state()['users']['owner']['token']
        menu = io.StringIO()
        with patch.dict(os.environ, PROXY_STACK_DATA=str(self.store.root)), contextlib.redirect_stdout(menu):
            self.assertEqual(main(['links', '--labelled']), 0)
        for label, path in labelled_paths(token):
            url = 'https://proxy.example.com:34567' + path
            self.assertIn(label + '（私密）：' + url, output)
            self.assertIn(label + ': ' + url, menu.getvalue())
        self.assertIn('server_remote', output)
        self.assertIn('filter_remote；需完整配置的 VPS 策略', output)
        self.assertIn('filter_remote；内置 proxy/direct', output)
        self.assertIn(f'https://proxy.example.com:34567/{token}/quantumult-x/rules-portable', output)
        self.assertLessEqual(len(output.splitlines()), 10)
        silent = io.StringIO()
        with contextlib.redirect_stdout(silent):
            self.assertEqual(finish.finish(self.store, silent=True), 0)
        self.assertEqual(silent.getvalue(), '')

    def test_failed_tls_offline_wrong_identity_and_headers_never_disclose(self):
        self.ack()
        failures = (ssl.SSLCertVerificationError('secret https://host/sub/private'),
                    OSError('offline'), Response(b'other installation'),
                    Response(self.asset(), mime='text/html'), Response(self.asset(), cache='public'),
                    Response(b'x' * (finish.MAX_ASSET + 1)))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                if isinstance(failure, Exception):
                    self.opener.return_value.open.side_effect = failure
                else:
                    self.opener.return_value.open.side_effect = None
                    self.opener.return_value.open.return_value = failure
                code, output = self.run_finish()
                self.assertEqual(code, 1)
                self.assertIn('HTTPS 待验证', output)
                self.assertIn('host-caddy.snippet', output)
                self.assertIn('TLS/global', output)
                self.assertNotIn('secret', output)
                self.assert_private(output)
                self.assertLessEqual(len(output.splitlines()), 7)

    def test_transport_policy_one_get_no_redirect_no_proxy_verified_tls(self):
        # Run synchronously to inspect the constructed opener; check() adds a cap.
        class Pipe:
            def send(self, value):
                pass
            def close(self):
                pass
        self.ack()
        finish.probe(str(self.store.root), Pipe())
        handlers = self.opener.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsNone(handlers[1].redirect_request(None, None, 302, '', {}, 'https://other'))
        self.assertEqual(handlers[2]._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(handlers[2]._context.check_hostname)
        self.opener.return_value.open.assert_called_once()
        request = self.opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.full_url, 'https://proxy.example.com/' + self.store.state()['users']['owner']['token'] + '/clash')
        self.assertEqual(self.opener.return_value.open.call_args.kwargs, {'timeout': 2})

    def test_classified_failures_cross_pipe_without_exception_urls_or_identity(self):
        self.ack()
        private = 'https://private.example/' + self.store.state()['users']['owner']['token']
        failures = [(TimeoutError(private), 'timeout'), (ConnectionRefusedError(private), 'connect'),
                    (urllib.error.URLError(private), 'connect'),
                    (finish.http.client.BadStatusLine(private), 'http'),
                    (urllib.error.URLError(ssl.SSLCertVerificationError(private)), 'tls'),
                    (urllib.error.HTTPError(private, 302, private, None, None), 'http'),
                    (Response(self.asset(), mime='text/html'), 'http'), (Response(b'wrong'), 'content')]
        class Pipe:
            def __init__(self): self.messages = []
            def send(self, value): self.messages.append(value)
            def close(self): pass
        for failure, reason in failures:
            pipe = Pipe()
            if isinstance(failure, Exception):
                self.opener.return_value.open.side_effect = failure
            else:
                self.opener.return_value.open.side_effect = None
                self.opener.return_value.open.return_value = failure
            finish.probe(str(self.store.root), pipe)
            self.assertEqual(pipe.messages[-1]['reason'], reason)
            serialized = json.dumps(pipe.messages)
            self.assertNotIn(private, serialized)
            self.assert_private(serialized)
            if isinstance(failure, Response):
                self.opener.return_value.open.return_value = Response(
                    b'wrong' if reason == 'content' else self.asset(),
                    mime='text/html' if reason == 'http' else 'application/yaml')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = finish.finish(self.store, silent=True, progress=True)
            self.assertEqual(code, 1)
            self.assertIn('失败（' + reason + '）', output.getvalue())
            self.assert_private(output.getvalue())
        with patch.object(finish, 'snapshot', side_effect=OSError(private)):
            pipe = Pipe()
            finish.probe(str(self.store.root), pipe)
            self.assertEqual(pipe.messages[-1]['reason'], 'state')
            self.assertNotIn(private, json.dumps(pipe.messages))

    def test_wait_reports_progress_even_when_probe_is_blocked_with_hard_cap(self):
        reports = []
        with patch.object(finish, 'snapshot', side_effect=lambda *_: time.sleep(10)), \
             patch.object(finish, 'PROGRESS_SECONDS', .1), patch.object(finish, 'ATTEMPT_SECONDS', .4):
            started = time.monotonic()
            result = finish.check(str(self.store.root), wait=.7,
                                  progress=lambda elapsed, value: reports.append((elapsed, dict(value))))
        self.assertLess(time.monotonic() - started, 1.3)
        self.assertGreaterEqual(len(reports), 3)
        self.assertEqual(result['reason'], 'state')
        self.assertTrue(all(value['reason'] == 'state' for _, value in reports))
        self.assert_private(json.dumps(reports))

    def test_success_at_progress_boundary_does_not_report_pending(self):
        def completed(_root, pipe):
            time.sleep(.15)
            pipe.send({'ready': True, 'https': True, 'reason': 'node'})
            pipe.close()
        reports = []
        with patch.object(finish, 'probe', completed), patch.object(finish, 'PROGRESS_SECONDS', .1):
            def report(*args):
                reports.append((args[0], dict(args[1])))
            # The pipe becomes readable just before the next progress boundary;
            # handling it crosses the boundary before the final success check.
            original = finish.completion_ready
            def delayed(value):
                time.sleep(.1)
                return original(value)
            with patch.object(finish, 'completion_ready', side_effect=delayed):
                result = finish.check(str(self.store.root), https_only=True, progress=report)
        self.assertTrue(result['ready'])
        self.assertFalse(any(value.get('ready') for _, value in reports))

    def test_https_ready_but_pending_offline_removed_or_stale_generation_node(self):
        code, output = self.run_finish()
        self.assertEqual(code, 1)
        self.assertIn('HTTPS 已验证', output)
        self.assertIn('等待就绪节点回执', output)
        self.assert_private(output)
        self.ack()
        atomic_file(self.store.model / 'heartbeats.json', '{"local": 0}')
        code, output = self.run_finish()
        self.assertEqual(code, 1)
        self.assert_private(output)
        self.ack()
        self.store.mutate_user('add', 'alice')
        self.assertEqual(self.run_finish()[0], 1)
        self.ack()
        Hub(self.store).remove('local')
        self.assertEqual(self.run_finish()[0], 1)

    def test_hub_only_needs_ready_remote_node(self):
        self.store = Store(self.store.root.parent / 'hub-only')
        self.store.init(Settings('hub.example.com', 'Hub', local_node=False))
        code, output = self.run_finish()
        self.assertEqual(code, 1)
        self.assertIn('HTTPS 已验证', output)
        self.assert_private(output)
        # Enroll, poll and acknowledge a remote node using the real control plane.
        from proxy_stack.control import decode_code
        hub = Hub(self.store)
        invitation = decode_code(hub.code())
        peer = hub.enroll({'secret': invitation['secret'],
                           'settings': asdict(Settings('node.example.com', 'Remote', role='node')),
                           'ws_path': '/ws/' + 'c' * 48,
                           'peer': {'id': 'a' * 48, 'token': 'b' * 48}})
        auth = 'Bearer ' + peer['id'] + ':' + peer['token']
        desired = hub.exchange('poll', {'generation': 0}, auth)
        hub.exchange('ack', {'generation': desired['generation']}, auth)
        self.assertEqual(self.run_finish()[0], 0)

    def test_concurrent_revoke_cannot_endorse_old_response(self):
        self.ack()
        def changed(*_a, **_k):
            old = self.asset()
            self.store.mutate_user('add', 'alice')
            return Response(old)
        self.opener.return_value.open.side_effect = changed
        code, output = self.run_finish()
        self.assertEqual(code, 1)
        self.assert_private(output)

    def test_network_wait_does_not_hold_state_lock(self):
        self.ack()
        def mutate(*_a, **_k):
            # This would deadlock if the network probe held operation.lock.
            self.store.mutate_user('add', 'alice')
            return Response(self.asset())
        self.opener.return_value.open.side_effect = mutate
        start = time.monotonic()
        self.assertEqual(self.run_finish()[0], 1)
        self.assertLess(time.monotonic() - start, 2)
        self.assertIn('alice', self.store.state()['users'])

    def test_hard_cap_covers_dns_reads_and_lock_waits_and_releases_locks(self):
        def blocked(*_a, **_k):
            time.sleep(10)
        for target in ('proxy_stack.finish.urllib.request.OpenerDirector.open', 'proxy_stack.finish.snapshot'):
            with self.subTest(target=target), patch(target, side_effect=blocked), patch.object(finish, 'ATTEMPT_SECONDS', .2):
                # Mock opener's own open for the network case.
                if target.endswith('.open'):
                    self.opener.return_value.open.side_effect = blocked
                start = time.monotonic()
                self.assertFalse(finish.check(str(self.store.root), wait=1)['ready'])
                self.assertLess(time.monotonic() - start, 1.6)
                with self.store.locked():
                    pass
        with self.store.locked(), patch.object(finish, 'ATTEMPT_SECONDS', .2):
            start = time.monotonic()
            self.assertFalse(finish.check(str(self.store.root))['ready'])
            self.assertLess(time.monotonic() - start, .8)

    def test_node_checks_public_wss_and_local_sync_rejects_stale_report(self):
        node = Store(self.store.root.parent / 'node')
        node.init(Settings('node.example.com', 'Node', role='node'))
        atomic_file(node.model / 'peer.json', '{"origin":"https://hub.example.com"}')
        self.store = node
        import json
        status = {'at': time.time(), 'running': True, 'generation': 2, 'desired': 2,
                  'lease_remaining': 10, 'error': None}
        class WebSocket:
            def __init__(self, *args, **kwargs):
                self.asserted = kwargs['context'].verify_mode == ssl.CERT_REQUIRED
            def request(self, method, path, headers):
                self.key = headers['Sec-WebSocket-Key']
            def getresponse(self):
                import base64, hashlib
                accept = base64.b64encode(hashlib.sha1((self.key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                return type('WS', (), {'status': 101, 'getheader': lambda _, k, default=None: {'Upgrade': 'websocket', 'Sec-WebSocket-Accept': accept}.get(k, default)})()
            def close(self):
                pass
        socket_patch = patch('proxy_stack.finish.http.client.HTTPSConnection', WebSocket)
        socket_patch.start()
        self.addCleanup(socket_patch.stop)
        for change, ready in (({}, True), ({'running': False}, False), ({'desired': 3}, False),
                              ({'at': time.time() - 7}, False), ({'lease_remaining': 0}, False),
                              ({'error': 'OSError'}, False)):
            with self.subTest(change=change):
                atomic_file(node.runtime / 'node-status.json', json.dumps(dict(status, **change)))
                code, output = self.run_finish()
                self.assertEqual(code, 0 if ready else 1, output)
                self.assertIn('统一订阅链接请从中心获取', output)
                self.assertNotIn('/sub/', output)
        self.opener.assert_not_called()
        original_snapshot = finish.snapshot
        def blocked_after_network(store):
            if blocked_after_network.calls:
                time.sleep(10)
            blocked_after_network.calls += 1
            return original_snapshot(store)
        blocked_after_network.calls = 0
        with patch.object(finish, 'snapshot', side_effect=blocked_after_network), \
             patch.object(finish, 'ATTEMPT_SECONDS', .3):
            result = finish.check(str(node.root))
        self.assertEqual(result['phase'], 'state')
        self.assertEqual(result['reason'], 'state')
        atomic_file(node.runtime / 'node-status.json', json.dumps(status))
        with patch('proxy_stack.finish.http.client.HTTPSConnection', side_effect=ssl.SSLCertVerificationError('private path')):
            code, output = self.run_finish()
            self.assertEqual(code, 1)
            self.assertIn('HTTPS/WSS 待验证', output)
            self.assertNotIn('private path', output)
            self.assertNotIn('/ws/', output)

    def test_internal_completion_requires_chosen_local_runtime_but_hub_only_can_bootstrap(self):
        self.assertFalse(finish.completion_ready({'role': 'hub', 'ready': True, 'https': True,
                                                  'local_required': True, 'local_ready': False}))
        self.assertTrue(finish.completion_ready({'role': 'hub', 'ready': False, 'https': True,
                                                 'local_required': False}))
        self.assertFalse(finish.completion_ready({'role': 'node', 'ready': False, 'https': True}))
        self.assertTrue(finish.completion_ready({'role': 'node', 'ready': True, 'https': True}))

    def test_cli_wait_is_bounded(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for value in ('-1', '121', '999', 'no'):
                with self.assertRaises(SystemExit):
                    main(['finish', '--wait', value])


if __name__ == '__main__':
    unittest.main()
