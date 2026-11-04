"""Same-v1 reconfiguration preserves identities and restores exact private state."""
import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from proxy_stack import install_state
from proxy_stack.control import Hub, decode_code
from proxy_stack.model import Settings
from proxy_stack.storage import Store, atomic_file


class InstallStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / 'data')
        self.settings = Settings('8.8.8.8', 'Original', mode='existing-caddy', email='operator@example.com',
                                 interval=600, lease_seconds=60)
        self.store.init(self.settings)
        patcher = patch.object(install_state, 'TRANSACTION', self.root / 'backup')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_readonly_check_no_layout_or_lock_and_apply_preserves_identity_then_exact_restore(self):
        before = install_state.tree_hash(self.store.root)
        state = self.store.state()
        changes = {'DOMAIN': '1.1.1.1', 'NODE_NAME': 'Changed', 'PUBLIC_PORT': '52789', 'MODE': 'standalone'}
        with patch.dict(os.environ, changes, clear=True), patch.object(self.store, 'locked', side_effect=AssertionError('preflight mutation')):
            install_state.operate(self.store, 'check')
        self.assertEqual(install_state.tree_hash(self.store.root), before)
        install_state.operate(self.store, 'save')
        with patch.dict(os.environ, changes, clear=True):
            install_state.operate(self.store, 'apply')
        changed = self.store.state()
        self.assertEqual(changed['users'], state['users'])
        self.assertEqual(changed['ws_path'], state['ws_path'])
        self.assertEqual(changed['settings']['port'], 52789)
        self.assertEqual(changed['settings']['email'], self.settings.email)
        self.assertEqual(changed['settings']['interval'], 600)
        self.assertEqual(changed['settings']['lease_seconds'], 60)
        self.assertTrue(changed['settings']['http01_only'])
        self.assertEqual(changed['nodes']['local']['settings'], changed['settings'])
        self.assertFalse(changed['nodes']['local']['ready'])
        self.assertIn('1.1.1.1:52789', (self.store.runtime / 'caddy/Caddyfile').read_text())
        install_state.operate(self.store, 'restore')
        self.assertEqual(install_state.tree_hash(self.store.root), before)
        install_state.operate(self.store, 'restore')
        self.assertEqual(install_state.tree_hash(self.store.root), before)

    def test_backup_tamper_refused_before_deleting_existing_state(self):
        install_state.operate(self.store, 'save')
        path = self.root / 'backup/data/model/state.json'
        path.write_text('{}')
        before = install_state.tree_hash(self.store.root)
        with self.assertRaisesRegex(ValueError, 'integrity'):
            install_state.operate(self.store, 'restore')
        self.assertEqual(install_state.tree_hash(self.store.root), before)

    def test_enrolled_node_and_hub_with_remote_nodes_reject_endpoint_change_before_writes(self):
        hub = Hub(self.store)
        remote = Settings('1.1.1.1', 'Remote', role='node')
        invite = decode_code(hub.code())
        hub.enroll({'secret': invite['secret'], 'settings': asdict(remote), 'ws_path': '/ws/' + 'a' * 48,
                    'peer': {'id': 'b' * 48, 'token': 'c' * 48}})
        before = install_state.tree_hash(self.store.root)
        with patch.dict(os.environ, {'PUBLIC_PORT': '52789'}, clear=True), self.assertRaisesRegex(ValueError, '远程节点'):
            install_state.operate(self.store, 'check')
        self.assertEqual(install_state.tree_hash(self.store.root), before)
        node = Store(self.root / 'node')
        node.init(remote)
        atomic_file(node.model / 'pending-peer.json', json.dumps({'origin': self.settings.origin}))
        with patch.dict(os.environ, {'PUBLIC_PORT': '52789'}, clear=True), self.assertRaisesRegex(ValueError, '已登记节点'):
            install_state.operate(node, 'check')


if __name__ == '__main__':
    unittest.main()
