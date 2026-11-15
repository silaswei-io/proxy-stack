"""Feature contracts, with synthetic identities only and no live service access."""
import contextlib
import copy
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from proxy_stack import chains, render, rules
from proxy_stack.cli import main
from proxy_stack.control import Hub, decode_code
from proxy_stack.model import Settings
from proxy_stack.runtime import validate_desired
from proxy_stack.storage import Store


class ChainTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'data')
        self.store.init(Settings('hub.example.com', 'Seoul', lease_seconds=10))
        self.hub = Hub(self.store)
        code = decode_code(self.hub.code())
        self.peer = {'id': 'a' * 48, 'token': 'b' * 48}
        self.hub.enroll({'secret': code['secret'], 'peer': self.peer,
                         'settings': dict(self.store.state()['settings'], domain='usa.example.com', node_name='USA', role='node'),
                         'ws_path': '/ws/' + 'c' * 48})
        self.exit = self.peer['id']
        self.auth = 'Bearer ' + self.exit + ':' + self.peer['token']
        self.sync()

    def poll(self, ident):
        return self.hub.exchange('poll', {'generation': self.store.state()['nodes'][ident]['acked']}, self.auth if ident != 'local' else None, local=ident == 'local')

    def sync(self):
        for ident in ('local', self.exit):
            desired = self.poll(ident)
            self.hub.exchange('ack', {'generation': desired['generation']}, self.auth if ident != 'local' else None, local=ident == 'local')

    def add(self, ident='usa', name='USA via Seoul', entry='local', exit=None):
        self.hub.chain('add', ident, name, entry, exit or self.exit)

    def proxies(self, name='owner'):
        token = self.store.state()['users'][name]['token']
        return json.loads((self.store.current() / 'sub' / token / 'clash.yaml').read_text())['proxies']

    def test_old_state_and_exact_exit_schema_and_scoped_entry(self):
        old = self.store.state()
        self.assertNotIn('chains', old)
        files = render.public_files(self.store.settings(old), old, rules.bootstrap())
        old['chains'] = {}
        self.assertTrue(files == render.public_files(self.store.settings(old), old, rules.bootstrap()))
        exit_before = self.poll(self.exit)
        self.add()
        desired = self.poll('local')
        self.assertTrue(exit_before == self.poll(self.exit))
        self.assertEqual(set(exit_before), {'generation', 'lease_seconds', 'users', 'ws_path'})
        self.assertEqual(set(desired['chains'][0]), {'id', 'exit'})
        encoded = json.dumps(desired)
        state = self.store.state()
        self.assertFalse(any(secret in encoded for secret in (self.peer['token'], self.exit, state['users']['owner']['token'], 'USA via Seoul')))
        validate_desired(desired, state['ws_path'])
        self.assertEqual([p['name'] for p in self.proxies()], ['USA'])
        self.sync()
        self.assertEqual([p['name'] for p in self.proxies()], ['Seoul', 'USA', 'USA via Seoul'])
        self.hub.publish(self.exit, False)
        self.assertEqual([p['name'] for p in self.proxies()], ['Seoul', 'USA via Seoul'])
        self.assertTrue(exit_before == self.poll(self.exit))
        self.hub.publish(self.exit, True)
        self.assertEqual(len(self.proxies()), 3)

    def test_per_user_outbound_identity_revoke_and_recreation(self):
        self.store.mutate_user('add', 'alice')
        self.add()
        self.sync()
        state = self.store.state()
        route_id = state['chains']['usa']['id']
        old_credential = chains.credential(state['users']['alice']['uuid'], route_id)
        desired = self.poll('local')
        config = json.loads(render.xray({'users': state['users'], 'ws_path': state['ws_path'], 'chains': desired['chains']}))
        for name, user in state['users'].items():
            derived = chains.credential(user['uuid'], route_id)
            out = next(o for o in config['outbounds'] if o.get('tag') == derived)
            self.assertTrue(out['settings']['vnext'][0]['users'][0]['id'] == user['uuid'])
            self.assertTrue(self.proxies(name)[-1]['uuid'] == derived)
            self.assertTrue(config['routing']['rules'][-1]['outboundTag'] == 'reject')
        alice_token = state['users']['alice']['token']
        self.store.mutate_user('revoke', 'alice')
        self.assertFalse((self.store.current() / 'sub' / alice_token).exists())
        self.assertTrue(old_credential not in render.xray({'users': self.store.state()['users'], 'ws_path': state['ws_path'], 'chains': self.poll('local')['chains']}))
        self.hub.chain('remove', 'usa')
        self.assertNotIn('chains', self.poll('local'))
        self.add()
        self.assertFalse(self.store.state()['chains']['usa']['id'] == route_id)

    def test_offline_pending_detach_remove_and_fresh_ack(self):
        self.add()
        self.sync()
        with patch('proxy_stack.control.time.time', return_value=time.time() + 11):
            self.store.update(lambda n: rules.bootstrap()['lists'][n])
            self.assertEqual(self.proxies(), [])
            self.sync()  # Same-generation renewal restores publication without a reaper.
            self.assertEqual(len(self.proxies()), 3)
        with patch('proxy_stack.control.time.time', return_value=time.time() + 22):
            self.hub.reap()
            self.assertEqual(self.proxies(), [])
            self.assertEqual(self.hub.chain('list')[0]['status'], 'unavailable')
        self.sync()
        self.store.mutate_user('add', 'alice')
        self.assertEqual(self.proxies(), [])
        self.sync()
        for action in ('detach', 'remove'):
            # A removed exit drops credentials from the entry desired and makes it pending.
            if action == 'detach':
                self.hub.exchange('detach', {'generation': 0}, self.auth)
            else:
                self.hub.remove('local')
            self.assertEqual(self.proxies(), [])
        self.assertFalse(self.store.state()['nodes']['local']['ready'])

    def test_unreaped_leases_after_last_chain_removal_and_updater(self):
        # Genuinely live ordinary installations keep identical no-chain assets.
        before = self.proxies()
        self.store.update(lambda n: rules.bootstrap()['lists'][n])
        self.assertTrue(before == self.proxies())
        self.add()
        self.sync()
        with patch('proxy_stack.storage.time.time', return_value=time.time() + 11):
            self.hub.chain('remove', 'usa')
            state = self.store.state()
            self.assertEqual(state['chains'], {})
            self.assertFalse(state['nodes']['local']['ready'])
            self.assertTrue(state['nodes'][self.exit]['ready'])
            self.assertEqual(self.proxies(), [])
            self.store.update(lambda n: rules.bootstrap()['lists'][n])
            self.assertEqual(self.proxies(), [])
        self.sync()
        self.assertEqual([p['name'] for p in self.proxies()], ['Seoul', 'USA'])
        # Old installations may omit the optional chains key entirely.
        state = self.store.state()
        state.pop('chains')
        self.hub.save(state)
        # The same lease enforcement applies to their ordinary updater publications.
        with patch('proxy_stack.storage.time.time', return_value=time.time() + 11):
            self.store.update(lambda n: rules.bootstrap()['lists'][n])
            self.assertEqual(self.proxies(), [])

    def test_validation_and_atomic_rollback(self):
        before, current = self.store.state(), self.store.current()
        for kwargs in ({'entry': self.exit}, {'entry': 'absent'}, {'name': 'bad,name'}, {'name': 'a' * 81}, {'name': ' pad '}, {'name': 'VPS'}, {'name': 'USA'}, {'ident': '../x'}):
            with self.assertRaises(ValueError):
                self.add(**kwargs)
            self.assertTrue(before == self.store.state())
            self.assertEqual(current, self.store.current())
        with patch.object(self.store, 'switch', side_effect=OSError('test')):
            with self.assertRaises(OSError):
                self.add()
        self.assertTrue(before == self.store.state())
        self.assertEqual(current, self.store.current())
        self.add()
        for kwargs in ({}, {'ident': 'duplicate', 'name': 'Another'}):
            with self.assertRaises(ValueError):
                self.add(**kwargs)
        # A valid full topology must reject the next mutation atomically.
        state = self.store.state()
        state['chains'] = {}
        for i in range(chains.MAX_CHAINS + 1):
            node_id = format(i + 1, '048x')
            node = copy.deepcopy(state['nodes'][self.exit])
            node['settings'].update(domain='n' + str(i) + '.example.com', node_name='Node ' + str(i))
            state['nodes'][node_id] = node
            if i < chains.MAX_CHAINS:
                state['chains']['line' + str(i)] = {'id': format(i + 100, '048x'), 'name': 'Line ' + str(i),
                                                  'entry': 'local', 'exit': node_id}
        chains.validate(state)
        self.store.commit(state, rules.bootstrap(), private=True)
        current = self.store.current()
        with self.assertRaises(ValueError):
            self.add(ident='overflow', name='Overflow', exit=node_id)
        self.assertTrue(state == self.store.state())
        self.assertEqual(current, self.store.current())
        desired = self.poll('local')
        for endpoint in ('127.0.0.1', '10.0.0.1', 'https://usa.example.com', '[2606:4700::1111]'):
            bad = json.loads(json.dumps(desired))
            bad['chains'][0]['exit']['domain'] = endpoint
            with self.assertRaises(ValueError):
                validate_desired(bad, desired['ws_path'])

    def test_unrelated_entry_routes_and_old_exit_are_isolated(self):
        code = decode_code(self.hub.code())
        other = {'id': 'd' * 48, 'token': 'e' * 48}
        self.hub.enroll({'secret': code['secret'], 'peer': other,
                         'settings': dict(self.store.state()['settings'], domain='other.example.com', node_name='Other', role='node'),
                         'ws_path': '/ws/' + 'f' * 48})
        self.add()
        self.add(ident='other', name='Other via Hub', entry=other['id'], exit='local')
        local = self.poll('local')
        self.assertEqual(len(local['chains']), 1)
        self.assertTrue('other.example.com' not in json.dumps(local))
        self.assertEqual(set(self.poll(self.exit)), {'generation', 'lease_seconds', 'ws_path', 'users'})
        self.hub.remove(self.exit)
        self.assertNotIn('chains', self.poll('local'))
        self.assertFalse(self.store.state()['nodes']['local']['ready'])

    def test_endpoint_rendering_and_strict_extension(self):
        self.add()
        desired = self.poll('local')
        for address, host in (('usa.example.com', 'usa.example.com'), ('8.8.8.8', '8.8.8.8'), ('2606:4700::1111', '[2606:4700::1111]')):
            desired['chains'][0]['exit']['domain'] = address
            validate_desired(desired, desired['ws_path'])
            cfg = json.loads(render.xray({'users': self.store.state()['users'], 'ws_path': desired['ws_path'], 'chains': desired['chains']}))
            stream = cfg['outbounds'][1]['streamSettings']
            self.assertEqual(stream['wsSettings']['headers']['Host'], host)
            self.assertFalse(stream['tlsSettings']['allowInsecure'])
        for extra in ([], [{}], [dict(desired['chains'][0], token='no')]):
            with self.assertRaises(ValueError):
                validate_desired(dict(desired, chains=extra), desired['ws_path'])

    def test_cli_and_hub_only_local_rejected(self):
        with patch.dict(os.environ, PROXY_STACK_DATA=str(self.store.root)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['chain', 'add', 'usa', '--name', 'USA via Seoul', '--entry', 'local', '--exit', self.exit]), 0)
            self.assertEqual(main(['chain', 'list']), 0)
            self.assertEqual(main(['node', 'publish', self.exit, 'off']), 0)
            self.assertEqual(main(['chain', 'remove', 'usa']), 0)
        self.hub.remove('local')
        with self.assertRaises(ValueError):
            self.add()
        other = Store(self.store.root.parent / 'hub-only')
        other.init(Settings('only.example.com', 'Hub', local_node=False))
        with self.assertRaises(ValueError):
            Hub(other).chain('add', 'usa', 'USA', 'local', self.exit)
