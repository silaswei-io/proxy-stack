import copy
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from proxy_stack import domains, render, rules
from proxy_stack.cli import main
from proxy_stack.finish import snapshot
from proxy_stack.model import Settings, subscription_endpoint
from proxy_stack.storage import Store
from proxy_stack.install_state import desired


class Fixture(domains.Deployment):
    def __init__(self, directory):
        super().__init__(directory)
        self.events = []
        self.busy = False
        self.failure = None
        self.on_confirm = None

    def preflight(self, recovering=False):
        self.config = {'services': {'caddy': {'image': 'fixture', 'ports': [{'published': '2096', 'target': 2096}]}}}
        state = self.store.state()
        if self.store.settings(state).role != 'hub':
            raise ValueError('hub only')
        return state

    def port_free(self, port):
        return not self.busy

    def validate_container(self, changes):
        self.events.append('validate')
        if self.failure == 'validate':
            raise ValueError('bad native config')

    def activate(self, backend, stage, recreate=False):
        self.events.append(('activate', backend, recreate))
        if self.failure == 'activate':
            self.failure = None
            raise ValueError('reload failed')

    def confirm(self, origin, wait=120):
        self.events.append(('confirm', origin))
        if self.on_confirm:
            callback, self.on_confirm = self.on_confirm, None
            callback()
        if self.failure == 'confirm':
            self.failure = None
            raise ValueError('TLS failed')


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / 'installation'
        self.root.mkdir()
        (self.root / '.env').write_text('DOMAIN=8.8.8.8\nPUBLIC_PORT=2096\nMODE=standalone\n')
        self.deployment = Fixture(self.root)
        self.store = self.deployment.store
        self.settings = Settings('8.8.8.8', 'Hub', port=2096)
        self.initial = self.store.init(self.settings)
        self.proof = patch.object(domains.discovery, 'check_address')
        self.proof.start()
        self.addCleanup(self.proof.stop)

    def add(self, **kwargs):
        return self.deployment.operate('add', 'subscriptions.example.com', **kwargs)

    def test_origin_nodes_users_and_chain_identities_are_independent(self):
        self.store.mutate_user('add', 'friend')
        before = self.store.state()
        xray = (self.store.runtime / 'xray/config.json').read_bytes()
        self.add()
        after = self.store.state()
        self.assertEqual({k: v for k, v in after.items() if k != 'subscription'}, before)
        self.assertEqual((self.store.runtime / 'xray/config.json').read_bytes(), xray)
        self.assertEqual(self.settings.origin, 'https://8.8.8.8:2096')
        self.assertEqual(subscription_endpoint(after).origin, 'https://subscriptions.example.com')
        self.assertEqual(after['nodes'], before['nodes'])
        self.assertEqual(after['users'], before['users'])

    def test_enrolled_hub_alias_preserves_control_code_routes_and_chain_credentials(self):
        from proxy_stack.control import Hub, decode_code
        from proxy_stack.chains import credential
        hub = Hub(self.store)
        invitation = decode_code(hub.code())
        peer = {'id': 'a'*48, 'token': 'b'*48}
        hub.enroll({'secret': invitation['secret'], 'peer': peer,
                    'settings': dict(self.initial['settings'], domain='exit.example.com', node_name='Exit', role='node'),
                    'ws_path': '/ws/' + 'c'*48})
        hub.chain('add', 'line', 'Two Hop', 'local', peer['id'])
        before = self.store.state()
        route = before['chains']['line']['id']
        identity = credential(before['users']['owner']['uuid'], route)
        self.add()
        after = self.store.state()
        self.assertEqual({k: v for k, v in after.items() if k != 'subscription'}, before)
        self.assertEqual(credential(after['users']['owner']['uuid'], after['chains']['line']['id']), identity)
        self.assertEqual(decode_code(hub.code())['origin'], self.settings.origin)
        with patch.dict(os.environ, {'PROXY_STACK_DATA': str(self.store.root)}), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['links', '--original']), 0)
        self.assertTrue(all(line.startswith(self.settings.origin + '/') for line in output.getvalue().splitlines()))

    def test_friend_links_embedded_rules_qx_and_dns_bootstrap_use_alias(self):
        self.store.mutate_user('add', 'friend')
        self.add()
        state = self.store.state()
        for name, user in state['users'].items():
            root = self.store.current() / 'sub' / user['token']
            clash = json.loads((root / 'clash.yaml').read_text())
            self.assertTrue(all(p['url'].startswith('https://subscriptions.example.com/') for p in clash['rule-providers'].values()))
            self.assertIn('DOMAIN,subscriptions.example.com,DIRECT', clash['rules'])
            self.assertIn('subscriptions.example.com', clash['dns']['fake-ip-filter'])
            self.assertIn('subscriptions.example.com', clash['dns']['nameserver-policy'])
            self.assertIn('https://subscriptions.example.com/', (root / 'qx/full.conf').read_text())
            self.assertIn('host, subscriptions.example.com, direct', (root / 'qx/rules.list').read_text())
            with patch.dict(os.environ, {'PROXY_STACK_DATA': str(self.store.root)}), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['links', '--user', name]), 0)
            self.assertTrue(all(line.startswith('https://subscriptions.example.com/') for line in output.getvalue().splitlines()))
        self.assertTrue(snapshot(self.store)['url'].startswith('https://subscriptions.example.com/'))

    def test_idempotency_conflict_remove_and_original_links(self):
        self.add()
        state = self.store.state()
        self.add()
        self.assertEqual(self.store.state(), state)
        with self.assertRaises(ValueError):
            self.deployment.operate('add', 'other.example.com')
        self.deployment.operate('remove')
        self.deployment.operate('remove')
        self.assertEqual(self.store.state(), self.initial)
        self.assertFalse((self.root / domains.OVERLAY).exists())
        self.assertFalse((self.store.root / domains.ALIAS).exists())
        for provider in json.loads((self.store.current() / ('sub/' + self.initial['users']['owner']['token'] + '/clash.yaml')).read_text())['rule-providers'].values():
            self.assertTrue(provider['url'].startswith(self.settings.origin))

    def test_own_https_port_does_not_duplicate_mapping(self):
        self.add(port=2096)
        self.assertFalse((self.root / domains.OVERLAY).exists())
        self.assertFalse(any(e == ('activate', 'container', True) for e in self.deployment.events))
        self.deployment.operate('remove')

    def test_expert_existing_caddy_mode_retains_original_layout_and_origin(self):
        from dataclasses import replace
        root = self.root / 'expert'
        root.mkdir()
        (root / '.env').write_text('DOMAIN=8.8.8.8\nPUBLIC_PORT=2096\nMODE=existing-caddy\nCOMPOSE_FILE=compose.yaml:compose.external.yaml\n')
        deployment = Fixture(root)
        settings = replace(self.settings, mode='existing-caddy')
        original = deployment.store.init(settings)
        deployment.operate('add', 'subscriptions.example.com')
        self.assertIn('compose.yaml:compose.external.yaml:compose.subscription.yaml', (root / '.env').read_text())
        self.assertIn(':8080 {', (deployment.store.runtime / 'caddy/external.Caddyfile').read_text())
        self.assertEqual(deployment.store.settings(deployment.store.state()), settings)
        deployment.operate('remove')
        self.assertEqual(deployment.store.state(), original)
        self.assertEqual(domains.compose_line((root / '.env').read_text()), 'compose.yaml:compose.external.yaml')

    def test_custom_port_add_remove_and_failure_restore_correct_mapping(self):
        self.add(port=8443)
        self.assertIn('8443:8443', (self.root / domains.OVERLAY).read_text())
        self.deployment.operate('remove')
        self.assertFalse((self.root / domains.OVERLAY).exists())
        self.deployment.failure = 'confirm'
        with self.assertRaises(ValueError):
            self.add(port=8443)
        self.assertFalse((self.root / domains.OVERLAY).exists())
        self.assertEqual(self.store.state(), self.initial)
        with self.assertRaises(ValueError):
            self.add(port=80)

    def real_deployment(self):
        deployment = domains.Deployment(self.root)
        volumes = {}
        for service, names in {'manager': ('model', 'public', 'cache', 'locks', 'runtime'), 'caddy': ('public', 'runtime/caddy')}.items():
            volumes[service] = []
            for name in names:
                target = '/etc/caddy' if name == 'runtime/caddy' else '/srv/public' if service == 'caddy' else '/data/' + name
                volumes[service].append({'type': 'bind', 'source': str(self.store.root / name), 'target': target})
        config = {'services': {service: {'volumes': value} for service, value in volumes.items()}}
        config['services']['manager']['environment'] = {'DOMAIN': '8.8.8.8', 'PUBLIC_PORT': '2096', 'MODE': 'standalone', 'ROLE': 'hub'}
        config['services']['caddy']['image'] = 'fixture'
        calls = []
        def run(argv, timeout=30):
            calls.append(argv)
            if argv[:3] == ['docker', 'context', 'inspect']:
                return 'unix:///var/run/docker.sock'
            if 'config' in argv:
                return json.dumps(config)
            if 'ps' in argv:
                return ''  # Failed recreation left Caddy stopped.
            return ''
        deployment.run = run
        return deployment, calls, config

    def test_real_preflight_recovery_allows_stopped_caddy_with_identity_guards(self):
        self.deployment.failure = 'confirm'
        with patch.object(self.deployment, 'recover', side_effect=RuntimeError('crash')), self.assertRaises(ValueError):
            self.add()
        deployment, calls, config = self.real_deployment()
        with self.assertRaisesRegex(ValueError, '启动'):
            deployment.preflight()
        deployment.operate('recover')
        self.assertEqual(self.store.state(), self.initial)
        self.assertFalse(deployment.stage.exists())
        self.assertTrue(any('up' in call and '--no-deps' in call and call[-1] == 'caddy' for call in calls))
        config['services']['caddy']['volumes'][0]['source'] = '/unrelated/data'
        with self.assertRaisesRegex(ValueError, '挂载'):
            deployment.preflight(recovering=True)

    def test_real_preflight_rejects_unknown_matching_alias_sibling(self):
        deployment, calls, config = self.real_deployment()
        unknown = self.store.runtime / 'caddy/subscription/unknown.Caddyfile'
        unknown.write_text('custom.example.com { respond "custom" }\n')
        with self.assertRaisesRegex(ValueError, '未知订阅'):
            deployment.preflight()
        self.assertEqual(self.store.state(), self.initial)
        self.assertFalse(any('up' in call or 'restart' in call for call in calls))
        self.assertNotIn('subscription*.Caddyfile', render.caddy(self.settings, self.initial))
        self.assertIn('subscription/*.Caddyfile', render.caddy(self.settings, self.initial))

    def test_ipv4_only_port_probe_skips_unavailable_ipv6_but_refuses_busy_port(self):
        deployment = domains.Deployment(self.root)
        deployment.config = {'services': {'caddy': {'image': 'fixture'}}}
        fake_socket = unittest.mock.MagicMock()
        with patch.object(domains.socket, 'socket', side_effect=[fake_socket, OSError(errno.EAFNOSUPPORT, 'no ipv6')]), patch.object(deployment, 'run') as run:
            self.assertTrue(deployment.port_free(443))
            run.assert_called_once()
        busy_socket = unittest.mock.MagicMock()
        busy_socket.bind.side_effect = OSError(errno.EADDRINUSE, 'busy')
        with patch.object(domains.socket, 'socket', return_value=busy_socket), patch.object(deployment, 'run') as run:
            self.assertFalse(deployment.port_free(443))
            run.assert_not_called()

    def test_reject_invalid_hostname_port_and_node_role(self):
        for host in ('1.1.1.1', 'https://a.example.com', 'a.example.com:443', '../etc', 'a.example.com\nimport /etc', 'a..com', '*.example.com', 'a.local', 'localhost', 'a.example.com/abc', 'a.com\x00'):
            with self.subTest(host=host), self.assertRaises(ValueError):
                self.deployment.operate('add', host)
        with self.assertRaises(ValueError):
            self.add(port=0)
        with self.store.locked():
            state = self.store.state()
            state['settings']['role'] = 'node'
            state['settings']['local_node'] = False
            state['users'] = {}
            state['nodes'] = {}
            self.store.commit(state, rules.bootstrap(), private=True)
        with self.assertRaises(ValueError):
            self.add()
        with self.assertRaises(ValueError):
            domains.show(self.store)

    def test_dns_failure_precedes_files_or_service_changes(self):
        with patch.object(domains.discovery, 'check_address', side_effect=ValueError('mismatch')), self.assertRaises(ValueError):
            self.add()
        self.assertEqual(self.deployment.events, [])
        self.assertEqual(self.store.state(), self.initial)
        self.assertFalse(self.deployment.stage.exists())

    def test_native_validation_failure_has_no_persistent_changes(self):
        self.deployment.failure = 'validate'
        with self.assertRaises(ValueError):
            self.add()
        self.assertFalse((self.root / domains.OVERLAY).exists())
        self.assertFalse((self.store.root / domains.ALIAS).exists())
        self.assertEqual(self.store.state(), self.initial)

    def test_activation_and_https_failures_restore_ports_files_and_model(self):
        original_env = (self.root / '.env').read_bytes()
        for failure in ('activate', 'confirm'):
            self.deployment.failure = failure
            with self.assertRaises(ValueError):
                self.add()
            self.assertEqual(self.store.state(), self.initial)
            self.assertEqual((self.root / '.env').read_bytes(), original_env)
            self.assertFalse(self.deployment.stage.exists())
            self.assertFalse((self.root / domains.OVERLAY).exists())
            self.assertFalse((self.store.root / domains.ALIAS).exists())

    def test_concurrent_user_and_topology_mutations_survive_success_and_rollback(self):
        for success, name in ((True, 'alice'), (False, 'bob')):
            def concurrent():
                self.store.mutate_user('add', name)
                with self.store.locked():
                    state = self.store.state()
                    state['chains'] = {}
                    state['nodes']['local']['publish'] = False
                    self.store.commit(state, rules.bootstrap(), private=True)
            self.deployment.on_confirm = concurrent
            if success:
                self.add()
                self.deployment.operate('remove')
            else:
                self.deployment.failure = 'confirm'
                with self.assertRaises(ValueError):
                    self.add()
            self.assertIn(name, self.store.state()['users'])
            self.assertFalse(self.store.state()['nodes']['local']['publish'])

    def test_publication_failure_rolls_back_model_and_assets(self):
        original_switch = self.store.switch
        attempts = []
        def switch(directory):
            attempts.append(directory)
            if len(attempts) == 1:
                raise OSError('disk failure')
            original_switch(directory)
        with patch.object(self.store, 'switch', side_effect=switch), self.assertRaises(OSError):
            self.add()
        self.assertEqual(self.store.state(), self.initial)
        self.assertNotIn('subscriptions.example.com', (self.store.current() / ('sub/' + self.initial['users']['owner']['token'] + '/clash.yaml')).read_text())

    def test_crash_recovery_after_model_publication_preserves_new_friends(self):
        def crash(*args):
            # Simulate SIGKILL after selecting model (normal exceptions roll back).
            state = self.store.state()
            self.assertIn('subscription', state)
            self.store.mutate_user('add', 'latefriend')
            raise SystemExit('simulated crash')
        original_confirm = self.deployment.confirm
        count = []
        def confirm(*args):
            count.append(1)
            if len(count) == 2:
                crash()
            original_confirm(*args)
        with patch.object(self.deployment, 'confirm', side_effect=confirm), patch.object(self.deployment, 'recover', side_effect=SystemExit('killed')):
            with self.assertRaises(SystemExit):
                self.add()
        self.assertTrue(self.deployment.stage.joinpath('journal.json').exists())
        self.deployment.operate('recover')
        self.assertIsNone(self.store.state().get('subscription'))
        self.assertIn('latefriend', self.store.state()['users'])
        self.assertFalse(self.deployment.stage.exists())

    def test_external_file_changes_are_not_clobbered_and_can_be_recovered(self):
        def changed():
            (self.root / domains.OVERLAY).write_text('concurrent custom configuration\n')
        self.deployment.on_confirm = changed
        self.deployment.failure = 'confirm'
        with self.assertRaises(ValueError):
            self.add()
        self.assertEqual((self.root / domains.OVERLAY).read_text(), 'concurrent custom configuration\n')
        self.assertTrue(self.deployment.stage.exists())
        # Explicit operator reconciliation of the conflicting file permits CAS.
        value = json.loads((self.deployment.stage / 'journal.json').read_text())
        (self.root / domains.OVERLAY).write_text(value['files'][str(self.root / domains.OVERLAY)]['after'])
        self.deployment.operate('recover')
        self.assertEqual(self.store.state(), self.initial)

    def test_rollback_preserves_unrelated_dotenv_edits(self):
        def changed():
            path = self.root / '.env'
            path.write_text(path.read_text() + 'UNRELATED=keep\n')
        self.deployment.on_confirm = changed
        self.deployment.failure = 'confirm'
        with self.assertRaises(ValueError):
            self.add()
        self.assertIn('UNRELATED=keep', (self.root / '.env').read_text())
        self.assertNotIn('COMPOSE_FILE=', (self.root / '.env').read_text())
        self.assertFalse(self.deployment.stage.exists())

    def test_unknown_custom_files_and_symlinks_are_refused(self):
        alias = self.store.root / domains.ALIAS
        alias.write_text('custom alias')
        with self.assertRaises(ValueError):
            self.add()
        alias.unlink()
        alias.symlink_to(self.root / '.env')
        with self.assertRaises(ValueError):
            self.add()
        self.assertTrue(alias.is_symlink())

    def test_updater_init_installer_retain_domain_and_alias(self):
        self.add()
        sub = copy.deepcopy(self.store.state()['subscription'])
        alias = (self.store.root / domains.ALIAS).read_bytes()
        self.store.update(lambda name: rules.bootstrap()['lists'][name])
        self.store.init(self.settings)
        self.assertEqual(self.store.state()['subscription'], sub)
        self.assertEqual((self.store.root / domains.ALIAS).read_bytes(), alias)
        with patch.dict(os.environ, {'DOMAIN': '1.1.1.1'}), self.assertRaises(ValueError):
            desired(self.store, self.store.state())

    def test_exported_helper_is_stdlib_only_and_runs_without_site_packages(self):
        path = self.root / 'helper.pyz'
        path.write_bytes(domains.helper_zip())
        result = subprocess.run([sys.executable, '-S', str(path), '--directory', str(self.root), 'show'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['original'], self.settings.origin)
        with zipfile.ZipFile(io.BytesIO(domains.helper_zip())) as archive:
            self.assertIn('proxy_stack/domains.py', archive.namelist())
            self.assertFalse(any('state.json' in name or '.env' in name for name in archive.namelist()))

    def test_alias_is_only_public_assets_and_upstream_tls_is_verified(self):
        endpoint = Settings('subscriptions.example.com', 'Alias')
        for upstream in (None, self.settings):
            config = render.subscription_site(endpoint, upstream)
            self.assertNotIn('/control/', config)
            self.assertNotIn('/ws/', config)
            self.assertIn('method GET HEAD', config)
            self.assertIn('respond "Not found" 404', config)
            self.assertNotIn('tls_insecure_skip_verify', config)
            if upstream:
                self.assertIn('reverse_proxy https://8.8.8.8:2096', config)
                self.assertIn('tls_server_name 8.8.8.8', config)
                self.assertIn('handle_errors', config)
