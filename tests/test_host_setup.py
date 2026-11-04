"""Isolated host adapter/transaction tests; never contact the real host service."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from proxy_stack import host_setup as setup


# Real systemctl show layout before/after systemctl reload on stock Caddy 2.6.
RELOAD_BEFORE = '{ path=/usr/bin/caddy ; argv[]=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0 }'
RELOAD_AFTER = '{ path=/usr/bin/caddy ; argv[]=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force ; ignore_errors=no ; start_time=[Wed 2026-11-04 06:22:38 UTC] ; stop_time=[Wed 2026-11-04 06:22:38 UTC] ; pid=54219 ; code=exited ; status=0 }'


class HostFixture(setup.Host):
    """Persistent fake stock Caddy 2.6 + systemd/process/listener boundary."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(exist_ok=True)
        self.config = self.root / 'Caddyfile'
        self.binary = self.root / 'caddy'
        self.unit = self.root / 'caddy.service'
        self.events = self.root / 'events.jsonl'
        self.loaded = self.root / 'loaded-config'
        if not self.config.exists():
            self.config.write_text('# comment with { braces }\n{\n email admin@example.com\n}\n'
                                   'example.com {\n reverse_proxy 127.0.0.1:3000\n}\n'
                                   'http://legacy.example.com {\n respond "} # quoted"\n}\n')
            self.config.chmod(0o640)
            self.binary.write_bytes(b'unchanged stock 2.6.2 binary')
            self.unit.write_bytes(b'unchanged stock unit')
        if not self.loaded.exists():
            self.loaded.write_bytes(self.config.read_bytes())
        self.info = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '123',
                     'FragmentPath': str(self.unit), 'User': 'caddy', 'Group': 'caddy',
                     'AmbientCapabilities': 'cap_net_bind_service', 'DropInPaths': '',
                     'Environment': '', 'EnvironmentFiles': '',
                     'ExecStart': '{ path=' + str(self.binary) + ' ; argv[]=' + str(self.binary) + ' run --environ --config ' + str(self.config) + ' ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0 }',
                     'ExecReload': '{ path=' + str(self.binary) + ' ; argv[]=' + str(self.binary) + ' reload --config ' + str(self.config) + ' --force ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0 }'}
        if self.events.exists() and '["systemctl", "reload"]' in self.events.read_text():
            self.info['ExecReload'] = self.info['ExecReload'].replace('start_time=[n/a]', 'start_time=[Wed 2026-11-04 06:22:38 UTC]').replace('stop_time=[n/a]', 'stop_time=[Wed 2026-11-04 06:22:38 UTC]').replace('pid=0', 'pid=54219').replace('code=(null)', 'code=exited')
        self.module_output = 'http.handlers.reverse_proxy\n\n  Standard modules: 1\n  Non-standard modules: 0\n  Unknown modules: 0\n'
        self.listeners = 'LISTEN 0 4096 *:80 *:* users:(("caddy",pid=123,fd=7))\n'
        self.fail_validate = False
        self.fail_reload = False
        self.pause_reload = False
        self.invalid_original = False

    def active(self):
        if self.info['ActiveState'] != 'active':
            raise setup.Refused('admin unavailable while stopped')
        return self.adapt(str(self.binary), self.config, self.loaded.read_text())

    def safe(self, path, writable=False):
        path = Path(path)
        if path.is_symlink():
            raise setup.Refused('symlink')
        return path

    def process(self, pid):
        return ([str(self.binary), 'run', '--environ', '--config', str(self.config)], self.binary, 999)

    def run(self, argv, data=None, cwd=None, timeout=20):
        with self.events.open('a') as out:
            out.write(json.dumps([Path(argv[0]).name] + argv[1:2]) + '\n')
        if argv[:2] == ['systemctl', 'show']:
            return '\n'.join(k + '=' + v for k, v in self.info.items())
        if argv[0] == 'ss':
            return self.listeners
        if argv[-1] == 'version':
            return '2.6.2\n'
        if 'list-modules' in argv:
            return self.module_output
        if argv[:2] == ['systemctl', 'reload']:
            if self.pause_reload or os.environ.get('FAKE_HOST_PAUSE'):
                time.sleep(15)
            if self.fail_reload or os.environ.get('FAKE_HOST_RELOAD_FAIL'):
                self.fail_reload = False
                raise setup.Refused('simulated reload failure')
            self.loaded.write_bytes(self.config.read_bytes())
            self.info['ExecReload'] = self.info['ExecReload'].replace('start_time=[n/a]', 'start_time=[Wed 2026-11-04 06:22:38 UTC]').replace('stop_time=[n/a]', 'stop_time=[Wed 2026-11-04 06:22:38 UTC]').replace('pid=0', 'pid=54219').replace('code=(null)', 'code=exited')
            return ''
        if argv[:2] == ['systemctl', 'is-active']:
            return 'active\n'
        if argv[:2] == ['systemctl', 'start']:
            self.info.update(ActiveState='active', SubState='running', MainPID='123')
            self.loaded.write_bytes(self.config.read_bytes())
            return ''
        if 'adapt' in argv:
            text = Path(argv[argv.index('--config') + 1]).read_text()
            # Native syntax/routing validation also runs in isolated Docker below.
            setup.tokens(text)
            routes = [{'match': [{'host': ['legacy.example.com']}]}]
            if 'BEGIN proxy-stack-http01' in text:
                routes.append({'match': [{'host': ['8.8.8.8']}], 'handle': [{'handler': 'reverse_proxy'}]})
            if self.invalid_original:
                routes.append({'handle': [{'handler': 'static_response'}]})
            return json.dumps({'apps': {'http': {'servers': {'srv0': {'listen': [':80'], 'routes': routes}}}}})
        if 'validate' in argv:
            if self.fail_validate or os.environ.get('FAKE_HOST_VALIDATE_FAIL'):
                raise setup.Refused('invalid candidate')
            return ''
        raise AssertionError(argv)


def fixture_main(root, argv, helper=None):
    host = HostFixture(root)
    with patch('proxy_stack.host_setup.platform.system', return_value='Linux'), \
         patch('proxy_stack.host_setup.os.geteuid', return_value=0), \
         patch('proxy_stack.host_setup.pwd.getpwnam', return_value=SimpleNamespace(pw_uid=999)):
        return (helper or setup).main(argv, host=host)


class HostSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.host = HostFixture(self.root / 'host')
        self.stage = self.root / 'transaction'
        self.old = self.host.config.read_bytes()
        self.binary = self.host.binary.read_bytes()
        self.unit = self.host.unit.read_bytes()
        for patcher in (patch('proxy_stack.host_setup.platform.system', return_value='Linux'),
                        patch('proxy_stack.host_setup.os.geteuid', return_value=0),
                        patch('proxy_stack.host_setup.pwd.getpwnam', return_value=SimpleNamespace(pw_uid=999))):
            patcher.start()
        self.addCleanup(patch.stopall)

    def plan(self):
        return setup.plan(self.host, self.stage, '8.8.8.8', 23456, 'proxy-stack-fixture')

    def unchanged(self):
        self.assertEqual(self.host.config.read_bytes(), self.old)
        self.assertEqual(self.host.binary.read_bytes(), self.binary)
        self.assertEqual(self.host.unit.read_bytes(), self.unit)
        self.assertEqual(self.host.config.stat().st_mode & 0o777, 0o640)

    def test_stock_26_plan_preserves_raw_blocks_and_never_upgrades_binary(self):
        value = self.plan()
        self.unchanged()
        self.assertEqual(value['service']['version'], '2.6.2')
        self.assertTrue((self.stage / 'candidate').read_bytes().startswith(self.old))
        candidate = (self.stage / 'candidate').read_text()
        self.assertEqual(candidate.count('email admin@example.com'), 1)
        self.assertIn('http://8.8.8.8:80', candidate)
        self.assertIn('path /.well-known/acme-challenge/*', candidate)
        self.assertIn('reverse_proxy 127.0.0.1:23456', candidate)
        self.assertNotIn('/sub/', candidate)
        self.assertNotIn('default_sni', candidate)
        self.assertEqual((self.stage / 'original').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('reload', self.host.events.read_text())

    def test_real_systemctl_reload_metadata_and_legacy_raw_journal_recover(self):
        self.assertEqual(setup.command_identity(RELOAD_BEFORE), setup.command_identity(RELOAD_AFTER))
        value = self.plan()
        # Old journals saved the full strings, with no identity schema marker.
        value['service']['service'] = dict(self.host.info)
        setup.save(self.stage / 'journal.json', value)
        self.host.info['ExecStart'] = self.host.info['ExecStart'].replace('pid=0', 'pid=123')
        setup.recheck(self.host, value)
        setup.apply(self.host, self.stage)
        self.assertIn('pid=54219', self.host.info['ExecReload'])
        setup.rollback(self.host, self.stage)
        self.unchanged()

    def test_stable_command_identity_rejects_actual_changes_and_multiple_commands(self):
        for changed in (RELOAD_AFTER.replace('--force', '--adapter json'),
                        RELOAD_AFTER.replace('path=/usr/bin/caddy', 'path=/usr/local/bin/caddy'),
                        RELOAD_AFTER.replace('ignore_errors=no', 'ignore_errors=yes')):
            self.assertNotEqual(setup.command_identity(RELOAD_BEFORE), setup.command_identity(changed))
        with self.assertRaises(setup.Refused):
            setup.command_identity(RELOAD_BEFORE + ' ' + RELOAD_AFTER)

    def test_rollback_refuses_command_unit_binary_import_and_active_config_drift(self):
        imported = self.host.root / 'extra.conf'
        imported.write_text('# imported original\n')
        self.host.config.write_bytes(self.old + b'import extra.conf\n')
        self.host.loaded.write_bytes(self.host.config.read_bytes())
        self.plan()
        setup.apply(self.host, self.stage)
        candidate = self.host.config.read_bytes()
        for key, change in (('ExecReload', '--force --adapter json'), ('User', 'root'),
                            ('Group', 'root'), ('AmbientCapabilities', ''), ('DropInPaths', '/other')):
            before = self.host.info[key]
            self.host.info[key] = before.replace('--force', change) if key == 'ExecReload' else change
            with self.subTest(key=key), self.assertRaises(setup.Refused):
                setup.rollback(self.host, self.stage)
            self.host.info[key] = before
            self.assertEqual(self.host.config.read_bytes(), candidate)
        for path in (self.host.unit, self.host.binary, imported):
            before = path.read_bytes()
            path.write_bytes(before + b'# external change\n')
            with self.subTest(path=path.name), self.assertRaises(setup.Refused):
                setup.rollback(self.host, self.stage)
            path.write_bytes(before)
        with patch.object(self.host, 'active', return_value={'external': 'admin API change'}), self.assertRaises(setup.Refused):
            setup.rollback(self.host, self.stage)
        setup.rollback(self.host, self.stage)

    def test_rollback_stopped_service_starts_before_admin_baseline_verification(self):
        for active, sub in (('inactive', 'dead'), ('failed', 'failed')):
            with self.subTest(state=active):
                self.plan()
                setup.apply(self.host, self.stage)
                self.host.info.update(ActiveState=active, SubState=sub, MainPID='0')
                with self.assertRaises(setup.Refused):
                    self.host.active()
                setup.rollback(self.host, self.stage)
                self.unchanged()
                self.assertEqual(self.host.loaded.read_bytes(), self.old)
                self.assertIn('["systemctl", "start"]', self.host.events.read_text())
                self.assertEqual(json.loads((self.stage / 'journal.json').read_text())['phase'], 'rolled-back')

    def test_rollback_refuses_transitioning_unknown_or_inconsistent_service_states(self):
        self.plan()
        setup.apply(self.host, self.stage)
        candidate = self.host.config.read_bytes()
        for active, sub, pid in (('activating', 'start', '0'), ('deactivating', 'stop', '123'),
                                 ('reloading', 'reload', '123'), ('unknown', 'dead', '0'),
                                 ('inactive', 'dead', '123'), ('active', 'exited', '0'),
                                 ('failed', 'running', '123')):
            with self.subTest(state=(active, sub, pid)):
                self.host.info.update(ActiveState=active, SubState=sub, MainPID=pid)
                with self.assertRaises(setup.Refused), patch.object(self.host, 'active') as admin:
                    setup.rollback(self.host, self.stage)
                admin.assert_not_called()
                self.assertEqual(self.host.config.read_bytes(), candidate)
        self.assertNotIn('["systemctl", "start"]', self.host.events.read_text())

    def test_unknown_listener_custom_module_user_and_unit_fail_without_changes(self):
        changes = [(self.host, 'listeners', self.host.listeners.replace('pid=123', 'pid=456')),
                   (self.host, 'module_output', 'dns.providers.custom v1 github.com/example/plugin'),
                   (self.host, 'invalid_original', True)]
        for target, key, value in changes:
            original = getattr(target, key)
            with self.subTest(key=key):
                setattr(target, key, value)
                with self.assertRaises(setup.Refused):
                    self.plan()
                self.unchanged()
                setattr(target, key, original)
        for key, value in (('User', 'root'), ('DropInPaths', '/custom'), ('EnvironmentFiles', '/custom')):
            original = self.host.info[key]
            self.host.info[key] = value
            with self.assertRaises(setup.Refused):
                self.plan()
            self.host.info[key] = original
            self.unchanged()
        with patch('proxy_stack.host_setup.os.geteuid', return_value=1000), self.assertRaises(setup.Refused):
            self.plan()

    def test_debian_bare_module_report_and_optional_version_prefix(self):
        for version in ('2.6.2', 'v2.6.2'):
            original_run = self.host.run
            def run(argv, **kwargs):
                return version if argv[-1] == 'version' else original_run(argv, **kwargs)
            with patch.object(self.host, 'run', side_effect=run):
                self.assertEqual(self.host.service()['version'], version)
        stock = '\n'.join('stock.module' + str(i) for i in range(98))
        report = stock + '\nStandard modules: 98\nNon-standard modules: 0\nUnknown modules: 0\n'
        self.assertTrue(setup.stock_modules(report))
        for output in (stock, report.replace('Unknown modules: 0', 'Unknown modules: 1'),
                       report.replace('Non-standard modules: 0', 'Non-standard modules: 1'),
                       report.replace('Standard modules: 98', 'Standard modules: 99'),
                       'dns.providers.custom v1 github.com/example/plugin'):
            self.assertFalse(setup.stock_modules(output))

    def test_candidate_failure_before_any_live_write(self):
        self.host.fail_validate = True
        with self.assertRaises(setup.Refused):
            self.plan()
        self.unchanged()
        self.assertFalse((self.stage / 'journal.json').exists())

    def test_race_recheck_refuses_changed_file_process_and_import_graph(self):
        self.plan()
        self.host.config.write_bytes(self.old + b'# external edit\n')
        with self.assertRaises(setup.Refused):
            setup.apply(self.host, self.stage)
        self.assertTrue(self.host.config.read_bytes().endswith(b'# external edit\n'))
        self.host.config.write_bytes(self.old)
        self.host.info['MainPID'] = '999'
        with self.assertRaises(setup.Refused):
            setup.apply(self.host, self.stage)
        self.assertNotIn('reload', self.host.events.read_text())

    def test_apply_and_repeat_are_idempotent_then_rollback_restores_exact_config(self):
        self.plan()
        setup.apply(self.host, self.stage)
        candidate = self.host.config.read_bytes()
        self.assertNotEqual(candidate, self.old)
        second = self.root / 'second'
        value = setup.plan(self.host, second, '8.8.8.8', 23456, 'proxy-stack-fixture')
        self.assertEqual(value['original'], value['candidate'])
        setup.apply(self.host, second)
        self.assertEqual(self.host.config.read_bytes(), candidate)
        setup.rollback(self.host, self.stage)
        self.unchanged()
        self.assertEqual(json.loads((self.stage / 'journal.json').read_text())['phase'], 'rolled-back')

    def test_failure_after_atomic_replace_and_reload_is_recoverable(self):
        self.plan()
        self.host.fail_reload = True
        with self.assertRaises(setup.Refused):
            setup.apply(self.host, self.stage)
        self.assertNotEqual(self.host.config.read_bytes(), self.old)
        self.assertEqual(json.loads((self.stage / 'journal.json').read_text())['phase'], 'reloading')
        setup.rollback(self.host, self.stage)
        self.unchanged()

    def test_crash_after_replacement_recovers_from_persisted_journal(self):
        self.plan()
        code = 'import sys;sys.path.insert(0,sys.argv[1]);from test_host_setup import fixture_main;sys.exit(fixture_main(sys.argv[2],sys.argv[3:]))'
        process = subprocess.Popen([sys.executable, '-c', code, str(Path(__file__).parent), str(self.host.root),
                                    'apply', '--stage', str(self.stage)],
                                   env=dict(os.environ, FAKE_HOST_PAUSE='1'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            until = time.monotonic() + 5
            while time.monotonic() < until:
                if json.loads((self.stage / 'journal.json').read_text())['phase'] == 'reloading':
                    break
                time.sleep(.02)
            else:
                self.fail('apply did not persist replacement phase')
            process.kill()
            process.wait(timeout=2)
            setup.rollback(self.host, self.stage)
            self.unchanged()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)

    def test_rollback_does_not_overwrite_concurrent_external_change(self):
        self.plan()
        setup.apply(self.host, self.stage)
        external = self.host.config.read_bytes() + b'# someone else changed it\n'
        self.host.config.write_bytes(external)
        with self.assertRaises(setup.Refused):
            setup.rollback(self.host, self.stage)
        self.assertEqual(self.host.config.read_bytes(), external)

    def test_imports_fingerprints_quoted_braces_and_unsafe_paths(self):
        imported = self.host.root / 'extra.conf'
        imported.write_text('other.example.com {\n respond `quoted { # }`\n}\n')
        self.host.config.write_bytes(self.old + b'import extra.conf\n')
        self.host.loaded.write_bytes(self.host.config.read_bytes())
        value = self.plan()
        self.assertIn(str(imported), value['dependencies']['files'])
        imported.write_text('# external import mutation\n')
        with self.assertRaises(setup.Refused):
            setup.apply(self.host, self.stage)
        for text in ('{\n', 'x {\n respond "unterminated\n}', 'import {$DYNAMIC}\n'):
            with self.assertRaises(setup.Refused):
                setup.tokens(text)


if __name__ == '__main__':
    unittest.main()
