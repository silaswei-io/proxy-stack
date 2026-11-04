"""Execute the exported Bash payload with real model/storage and a fake Docker.

No daemon, host ports, upstream downloads or real credentials are used here.
"""
import json
import os
import pty
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.resources import files
from pathlib import Path

from proxy_stack import __version__
from proxy_stack.installer import script


FAKE_DOCKER = r'''
import json, os, sys
from pathlib import Path
def main(argv):
    from proxy_stack.cli import main as cli_main
    return cli_main(argv)
args = sys.argv[1:]
with open(os.environ['FAKE_DOCKER_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\n')
if args[:2] == ['context', 'inspect']:
    print(os.environ.get('FAKE_DOCKER_ENDPOINT', 'unix:///var/run/docker.sock')); sys.exit(0)
if args[:2] == ['info', '--format']:
    print(os.environ.get('FAKE_DOCKER_SECURITY', '[]')); sys.exit(0)
if args == ['info']:
    sys.exit(int(os.environ.get('FAKE_DOCKER_DOWN', '0')))
if args[:3] == ['compose', 'version', '--short']:
    print(os.environ.get('FAKE_COMPOSE_VERSION', '2.24.4')); sys.exit(0)
if args[:3] == ['compose', '--progress', 'quiet']:
    args = ['compose'] + args[3:]
if args[0] == 'pull':
    assert args[1] == 'ghcr.io/silaswei-io/proxy-stack:v1.0.0' or args[1].startswith('caddy:2.11.4@sha256:')
    print('fake Docker pull progress', file=sys.stderr)
    sys.exit(int(os.environ.get('FAKE_PULL_FAIL', '0')))
if args[0] in ('network', 'volume') and args[1] == 'ls':
    sys.exit(0)
if args[0] == 'inspect':
    if 'NetworkSettings.Ports' in args[args.index('--format') + 1]:
        print(os.environ.get('FAKE_BINDINGS', ''))
    else:
        values = dict(line.split('=', 1) for line in Path(os.environ.get('FAKE_OWNED_DIRECTORY', '.'), '.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
        print(values['COMPOSE_PROJECT_NAME'] + '|caddy|' + os.environ.get('FAKE_OWNED_DIRECTORY', str(Path.cwd())) + '|true')
    sys.exit(0)
if args[0] == 'ps':
    sys.exit(0)
if args[0] == 'rm':
    sys.exit(0)
def patch_control():
    if os.environ.get('FAKE_JOIN_HUB'):
        from proxy_stack import control
        from proxy_stack.storage import Store
        hub = control.Hub(Store(os.environ['FAKE_JOIN_HUB']))
        def local_request(origin, action, data, authorization=None):
            assert origin == hub.store.settings(hub.store.state()).origin
            if os.environ.get('FAKE_JOIN_FAIL') and action == 'enroll' or os.environ.get('FAKE_CANCEL_FAIL') and action == 'detach':
                raise OSError('simulated offline hub')
            try:
                return hub.enroll(data) if action == 'enroll' else hub.exchange(action, data, authorization)
            except PermissionError:
                import urllib.error
                raise urllib.error.HTTPError(origin,403,'unauthorized',None,None)
        control.request = local_request
if args[0] == 'run':
    # Explicit boundary: never send the fixture's public IP to a network.
    # Both halves must carry the same nonce and run on separate bridge containers.
    if any(a.startswith('ROUNDTRIP_') for a in args):
        values = dict(args[i+1].split('=', 1) for i, a in enumerate(args) if a == '-e')
        name = args[args.index('--name')+1]
        assert args[args.index('--network')+1] == 'bridge'
        nonce_file = Path(os.environ['FAKE_DOCKER_LOG'] + '.nonce')
        if '-p' in args:
            assert name.startswith('proxy-stack-roundtrip-listener-')
            nonce_file.write_text(values['ROUNDTRIP_NONCE'])
            if os.environ.get('FAKE_ROUNDTRIP_PAUSE'):
                import time
                time.sleep(30)
            sys.exit(0)
        assert name.startswith('proxy-stack-roundtrip-probe-')
        assert nonce_file.read_text() == values['ROUNDTRIP_NONCE']
        assert values['ROUNDTRIP_HOST']
        assert values['ROUNDTRIP_PORT'].isdigit()
        sys.exit(int(os.environ.get('FAKE_ROUNDTRIP_FAIL', '0')))
    if any(arg.startswith('caddy:2.11.4@sha256:') for arg in args):
        sys.exit(int(os.environ.get('FAKE_CANDIDATE_FAIL', '0')))
    if args[-1] == 'check-address':
        sys.exit(0)
    if '-p' in args:
        binding = args[args.index('-p') + 1].rsplit(':', 1)[0]
        busy = binding in os.environ.get('FAKE_BUSY_PORTS', '').split(',')
        if os.environ.get('FAKE_BUSY_ALL_HIGH') and binding.isdigit() and int(binding) >= 20000:
            busy = True
        if os.environ.get('FAKE_RACE_PORT') == binding:
            count = sum(1 for line in Path(os.environ['FAKE_DOCKER_LOG']).read_text().splitlines() if '-p' in json.loads(line) and binding + ':8080' in json.loads(line))
            busy = busy or count >= 2
        sys.exit(1 if busy else 0)
    for i, arg in enumerate(args):
        if arg == '-e':
            key, value = args[i + 1].split('=', 1)
            os.environ[key] = value
    if args[-1] == 'detect-public-ip':
        if os.environ.get('FAKE_IP_FAILURE'):
            sys.exit(1)
        print(os.environ.get('FAKE_PUBLIC_IP', '8.8.8.8')); sys.exit(0)
    if args[-1] == 'join':
        for i, arg in enumerate(args):
            if arg == '-v' and args[i + 1].endswith(':/data'):
                os.environ['PROXY_STACK_DATA'] = args[i + 1][:-len(':/data')]
        patch_control()
    if 'install-state' in args:
        from proxy_stack import install_state
        for i, arg in enumerate(args):
            if arg == '-v' and ':/data' in args[i + 1]:
                os.environ['PROXY_STACK_DATA'] = args[i + 1].split(':/data')[0]
            if arg == '-v' and args[i + 1].endswith(':/transaction'):
                install_state.TRANSACTION = Path(args[i + 1][:-len(':/transaction')])
        patch_control()
        if args[-1] == 'apply' and os.environ.get('FAKE_STATE_APPLY_FAIL'):
            # Simulate a failure after regenerated files were already written.
            main(['install-state', 'apply'])
            sys.exit(1)
        sys.exit(main(['install-state', args[-1]]))
    sys.exit(main([args[-1]]))
assert args[0] == 'compose', args
assert os.environ.get('COMPOSE_FILE') != '/unrelated/compose.yaml', 'inherited Compose configuration'
if args[1] == 'config' and '--images' in args:
    values = dict(line.split('=', 1) for line in Path('.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
    print(os.environ.get('FAKE_TOOL_IMAGE', values.get('TOOL_IMAGE', 'ghcr.io/silaswei-io/proxy-stack:v1.0.0').strip("'")));sys.exit(0)
if args[1] == 'ps' and '--services' in args:
    print(os.environ.get('FAKE_RUNNING', 'hub\nxray\nupdater\ncaddy'));sys.exit(0)
if args[1] == 'run':
    # Parse only the dotenv literals generated by the installer, never source/eval.
    for line in Path('.env').read_text().splitlines():
        if line and not line.startswith('#'):
            key, value = line.split('=', 1)
            os.environ.setdefault(key, value.strip("'"))
    os.environ['PROXY_STACK_DATA'] = str(Path('data').absolute())
    patch_control()
    if args[-1] == 'init' and os.environ.get('FAKE_INIT_FAIL'):
        main(['init'])
        sys.exit(1)
    if 'finish' in args:
        if os.environ.get('FAKE_FINISH_OUTER_TIMEOUT'):
            sys.exit(124)
        from proxy_stack import finish
        from email.message import Message
        import io
        original_check = finish.check
        finish.check = lambda root, wait, https_only=False, **kwargs: original_check(root, 0, https_only, **kwargs)
        class Response(io.BytesIO):
            status = 200
            headers = Message()
            headers['Content-Type'] = 'application/yaml'
            headers['Cache-Control'] = 'no-store'
        class Opener:
            def open(self, req, timeout):
                if os.environ.get('FAKE_HTTPS_READY') != '1':
                    raise OSError('offline')
                from proxy_stack.storage import Store
                store = Store(os.environ['PROXY_STACK_DATA'])
                token = store.state()['users']['owner']['token']
                return Response((store.current() / ('sub/' + token + '/clash.yaml')).read_bytes())
        finish.urllib.request.build_opener = lambda *args: Opener()
        class WebSocket:
            # urllib's HTTPSHandler reads these class attributes even when its
            # opener is stubbed; keep the WSS fixture compatible with Python 3.13.
            debuglevel = 0
            _http_vsn = 11
            def __init__(self, *args, **kwargs):
                pass
            def request(self, method, path, headers):
                if os.environ.get('FAKE_HTTPS_READY') != '1':
                    raise OSError('offline public TLS')
                self.key = headers['Sec-WebSocket-Key']
            def getresponse(self):
                import base64, hashlib
                accept = base64.b64encode(hashlib.sha1((self.key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                return type('WS', (), {'status': 101, 'getheader': lambda _, k, default=None: {'Upgrade': 'websocket', 'Sec-WebSocket-Accept': accept}.get(k, default)})()
            def close(self):
                pass
        finish.http.client.HTTPSConnection = WebSocket
    if 'update-rules' in args:
        from proxy_stack.storage import Store
        from proxy_stack.rules import bootstrap
        update = Store.update
        Store.update = lambda self: update(self, lambda name: bootstrap()['lists'][name])
    sys.exit(main(args[args.index('manager') + 1:]))
if args[1] == '--project-name' and 'logs' in args:
    assert args[-1] == 'caddy' and '--tail' in args
    print('fake scoped Caddy certificate diagnostic')
    sys.exit(0)
if args[1] == 'up':
    if os.environ.get('FAKE_UP_PAUSE'):
        import time
        time.sleep(30)
    if os.environ.get('FAKE_ACK') == '1':
        from proxy_stack.storage import Store
        from proxy_stack.control import Hub
        store = Store(Path('data').absolute())
        state = store.state()
        if state['settings']['role'] == 'hub' and state['settings']['local_node']:
            hub = Hub(store)
            desired = hub.exchange('poll', {'generation': state['nodes']['local']['acked'] if '--no-recreate' in args else 0}, local=True)
            hub.exchange('ack', {'generation': desired['generation']}, local=True)
    if os.environ.get('FAKE_NODE_ACK') == '1':
        import time
        from proxy_stack.storage import Store, atomic_file
        from proxy_stack.control import Hub
        node = Store(Path('data').absolute())
        peer = node.read(node.model / 'peer.json')
        hub = Hub(Store(os.environ['FAKE_JOIN_HUB']))
        authorization = 'Bearer ' + peer['id'] + ':' + peer['token']
        desired = hub.exchange('poll', {'generation': 0}, authorization)
        hub.exchange('ack', {'generation': desired['generation']}, authorization)
        atomic_file(node.runtime / 'node-status.json', json.dumps({'at': time.time(), 'running': True,
            'generation': desired['generation'], 'desired': desired['generation'], 'lease_remaining': 300, 'error': None}))
    print('fake Docker up progress', file=sys.stderr)
    sys.exit(int(os.environ.get('FAKE_UP_FAIL', '0')))
if args[1] == 'ps' and '-q' in args:
    print('a' * 64 if os.environ.get('FAKE_OWNED_CADDY') else 'fake-running-xray')
sys.exit(0)
'''


@unittest.skipUnless(sys.platform == 'linux', 'temporary containers run Linux')
class NonceDeadlineTests(unittest.TestCase):
    def code(self, seconds):
        return next(code for code in re.findall(
            r"--entrypoint python \"\$tool_image\" -c '\n(.*?)\n'", script(), re.S)
                    if 'signal.alarm(' + str(seconds) + ')' in code)

    def test_extracted_listener_expires_despite_trickled_headers(self):
        started = time.monotonic()
        process = subprocess.Popen([sys.executable, '-c', self.code(30)],
                                   env=dict(os.environ, ROUNDTRIP_NONCE='a' * 64),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        connection = None
        try:
            while time.monotonic() - started < 3:
                try:
                    connection = socket.create_connection(('127.0.0.1', 8080), timeout=.5)
                    break
                except ConnectionRefusedError:
                    if process.poll() is not None:
                        self.fail('listener exited before accepting a connection')
                    time.sleep(.02)
            self.assertIsNotNone(connection)
            connection.sendall(b'GET / HTTP/1.1\r\nX-Trickle: ')
            # Every byte arrives within the two-second socket idle cap.
            while process.poll() is None and time.monotonic() - started < 33:
                try:
                    connection.sendall(b'a')
                except OSError:
                    break
                time.sleep(.2)
            self.assertEqual(process.wait(timeout=2), -signal.SIGALRM)
            self.assertLess(time.monotonic() - started, 33)
        finally:
            if connection:
                connection.close()
            if process.poll() is None:
                process.kill()
            process.wait()

    def test_extracted_probe_expires_during_blocked_dns(self):
        started = time.monotonic()
        result = subprocess.run([sys.executable, '-c',
                                 'import socket, time\nsocket.getaddrinfo = lambda *a, **k: time.sleep(60)\n' + self.code(12)],
                                env=dict(os.environ, ROUNDTRIP_HOST='nonce.invalid', ROUNDTRIP_PORT='8080',
                                         ROUNDTRIP_NONCE='a' * 64), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, -signal.SIGALRM)
        self.assertLess(time.monotonic() - started, 15)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="proxy-stack-ux-")
        self.addCleanup(self.temporary.cleanup)
        # macOS /var is itself a symlink; use the physical disposable path.
        self.root = Path(self.temporary.name).resolve()
        self.destination = self.root / "installation with spaces"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "docker"
        fake.write_text("#!" + sys.executable + "\n" + FAKE_DOCKER)
        fake.chmod(0o700)
        hostname = self.bin / "hostname"
        hostname.write_text('#!/bin/sh\nprintf "%s\\n" "${FAKE_HOSTNAME-test-host_01}"\n')
        hostname.chmod(0o700)
        self.log = self.root / "docker.jsonl"
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        FAKE_DOCKER_LOG=str(self.log), FAKE_HTTPS_READY='1', FAKE_ACK='1')
        self.payload = script()

    def install(self, *args, inputs=True, env=None, timeout=45):
        command = ["bash", "-s", "--", "--directory", str(self.destination), "--non-interactive"]
        if inputs:
            command += ["--domain", "Proxy.Example.com", "--name", "测试 VPS", "--port", "34567"]
        return subprocess.run(command + list(args), input=self.payload, text=True, capture_output=True,
                              env=env or self.env, start_new_session=True, timeout=timeout)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_export_is_only_bash_and_templates_match_payload(self):
        exported = subprocess.check_output([sys.executable, "-m", "proxy_stack.cli", "installer"], text=True)
        self.assertEqual(exported, self.payload)
        self.assertTrue(exported.startswith("#!/usr/bin/env bash\n"))
        subprocess.run(["bash", "-n"], input=exported, text=True, check=True)
        self.assert_ok(self.install("--prepare-only"))
        templates = files("proxy_stack").joinpath("templates")
        for name in ("compose.yaml", "compose.external.yaml", ".env.example", "proxyctl"):
            self.assertEqual((self.destination / name).read_text(), templates.joinpath(name).read_text())
        self.assertIn("ghcr.io/silaswei-io/proxy-stack:" + __version__, (self.destination / ".env").read_text())
        self.assertNotIn("EXPORT_TEMPLATES", exported)

    def test_fresh_install_finishes_after_up_without_unverified_links(self):
        result = self.install('--role', 'hub-only', env=dict(self.env, DOMAIN='unrelated.example.com', NODE_NAME='Unrelated',
                                       COMPOSE_FILE='/unrelated/compose.yaml', COMPOSE_PROJECT_NAME='unrelated'))
        self.assert_ok(result)
        self.assertNotIn("/sub/", result.stdout + result.stderr)
        self.assertIn("HTTPS 已验证", result.stdout)
        self.assertIn("等待就绪节点回执", result.stdout)
        self.assertIn("minimal-bootstrap", result.stdout)
        self.assertIn("proxyctl' menu", result.stdout)
        state = json.loads((self.destination / "data/model/state.json").read_text())
        self.assertEqual(state["settings"]["node_name"], "测试 VPS")
        project = next(line for line in (self.destination / '.env').read_text().splitlines()
                       if line.startswith('COMPOSE_PROJECT_NAME='))
        self.assertRegex(project, r'^COMPOSE_PROJECT_NAME=proxy-stack-\d+-\d+-\d+$')
        self.assertEqual((self.destination / ".env").stat().st_mode & 0o777, 0o600)
        calls = self.calls()
        finish = next(i for i, call in enumerate(calls) if "finish" in call)
        up = calls.index(["compose", "up", "-d", "--force-recreate"])
        self.assertGreater(finish, up)
        self.assertFalse(any("links" in c for c in calls))
        self.assertIn("--wait", calls[finish])
        self.assertFalse(any("status" in c or "verify" in c for c in calls))
        # The secret links are only stdout, never command arguments/ordinary logs.
        self.assertNotIn(state["users"]["owner"]["token"], self.log.read_text())
        self.assertNotIn(state["users"]["owner"]["token"], result.stderr)

    def test_completion_outer_timeout_uses_phase_neutral_diagnosis(self):
        result = self.install(env=dict(self.env, FAKE_FINISH_OUTER_TIMEOUT='1'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('120 秒上限已到；根据上方进度分类检查本机状态', result.stderr)
        self.assertNotIn('120 秒上限已到；检查所选 HTTPS 端口的云防火墙', result.stderr)

    def test_hub_only_pending_nodes_is_quiet_and_private_logs_retain_docker_errors(self):
        result = self.install('--role', 'hub-only')
        self.assert_ok(result)
        self.assertIn('HTTPS 已验证', result.stdout)
        self.assertNotIn('/sub/', result.stdout + result.stderr)
        self.assertNotIn('fake Docker', result.stdout + result.stderr)
        log_path = Path(result.stdout.split('安装日志/恢复记录：')[1].strip()) / 'operator.log'
        # Transaction journals intentionally retain recovery evidence.
        self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)
        self.assertIn('fake Docker pull progress', log_path.read_text())
        self.assertIn('fake Docker up progress', log_path.read_text())
        owner = json.loads((self.destination / 'data/model/state.json').read_text())['users']['owner']
        for secret in owner.values():
            self.assertNotIn(secret, log_path.read_text() + self.log.read_text() + result.stderr)
        self.assertEqual(self.calls().count(['pull', 'ghcr.io/silaswei-io/proxy-stack:v1.0.0']), 1)
        self.assertFalse(any('links' in c or 'verify' in c for c in self.calls()))

    def test_failed_pull_retains_useful_private_log_and_no_install(self):
        result = self.install(env=dict(self.env, FAKE_PULL_FAIL='1'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('诊断日志：', result.stderr)
        self.assertNotIn('fake Docker', result.stdout + result.stderr)
        log_path = Path(result.stderr.split('诊断日志：')[1].splitlines()[0])
        # Transaction journals intentionally retain recovery evidence.
        self.assertIn('fake Docker pull progress', log_path.read_text())
        self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.destination.exists())

    def test_retry_finish_in_menu_and_managed_installer_preserves_identity(self):
        from proxy_stack.control import Hub
        from proxy_stack.storage import Store
        self.assert_ok(self.install('--prepare-only'))
        store = Store(self.destination / 'data')
        hub = Hub(store)
        generation = hub.exchange('poll', {'generation': 0}, local=True)['generation']
        hub.exchange('ack', {'generation': generation}, local=True)
        before = {str(p.relative_to(self.destination)): p.read_bytes()
                  for p in self.destination.rglob('*') if p.is_file()}
        self.env['FAKE_HTTPS_READY'] = '1'
        self.log.write_text('')
        result = self.install()
        self.assert_ok(result)
        self.assertIn('订阅就绪', result.stdout)
        self.assertEqual(result.stdout.count('https://'), 1)
        self.assertRegex(result.stdout, r'https://[^\s]+/[0-9a-f]{48}/clash')
        after = {str(p.relative_to(self.destination)): p.read_bytes()
                 for p in self.destination.rglob('*') if p.is_file()}
        self.assertEqual({k:v for k,v in before.items() if k != 'data/model/heartbeats.json'},
                         {k:v for k,v in after.items() if k != 'data/model/heartbeats.json'})
        self.assertFalse(any('init' in c or c[0] == 'pull' and c[1] == 'ghcr.io/silaswei-io/proxy-stack:v1.0.0' for c in self.calls()))
        code, output = self.terminal([str(self.destination / 'proxyctl'), 'menu'], '4\n0\n')
        self.assertEqual(code, 0, output)
        self.assertIn('订阅就绪', output)
        self.assertIn('服务与完成检查', output)
        self.assertNotIn('代理管理（', output)

    def test_repeat_explicit_settings_refresh_templates_and_preserve_credentials(self):
        self.assert_ok(self.install('--prepare-only'))
        (self.destination / '.env').write_text((self.destination / '.env').read_text() + '# keep my config\n')
        original = json.loads((self.destination / 'data/model/state.json').read_text())
        # Simulate the still-published same-v1 wrapper. Only the authorized
        # installer transaction refreshes templates; init remains immutable.
        (self.destination / 'proxyctl').write_text((self.destination / 'proxyctl').read_text() + '# older same-v1 wrapper\n')
        self.log.write_text('')
        result = self.install('--domain', 'different.example.com', '--name', 'Different', '--port', '45678')
        self.assert_ok(result)
        updated = json.loads((self.destination / 'data/model/state.json').read_text())
        self.assertEqual(updated['settings']['domain'], 'different.example.com')
        self.assertEqual(updated['settings']['node_name'], 'Different')
        self.assertEqual(updated['settings']['port'], 45678)
        self.assertEqual(updated['users'], original['users'])
        self.assertEqual(updated['ws_path'], original['ws_path'])
        self.assertIn('# keep my config', (self.destination / '.env').read_text())
        self.assertEqual((self.destination / 'proxyctl').read_text(), files('proxy_stack').joinpath('templates/proxyctl').read_text())
        self.assertFalse(any('init' in c for c in self.calls()))
        self.assertTrue(any('--force-recreate' in c for c in self.calls()))
        self.assertRegex(result.stdout, r':45678/[0-9a-f]{48}/clash')

    def test_reconfigure_rolls_back_exact_files_state_and_prior_services(self):
        self.assert_ok(self.install('--prepare-only'))
        original = {str(p.relative_to(self.destination)): p.read_bytes()
                    for p in self.destination.rglob('*') if p.is_file()}
        for changes in ({'FAKE_STATE_APPLY_FAIL': '1'}, {'FAKE_UP_FAIL': '1'}, {'FAKE_HTTPS_READY': '0'}):
            with self.subTest(changes=changes):
                self.log.write_text('')
                result = self.install('--port', '45678', env=dict(self.env, **changes))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('已回滚', result.stderr)
                self.assertNotIn('/sub/', result.stdout + result.stderr)
                self.assertEqual(original, {str(p.relative_to(self.destination)): p.read_bytes()
                                            for p in self.destination.rglob('*') if p.is_file()})
                self.assertFalse(Path(str(self.destination) + '.install-lock').exists())
                restored = [c for c in self.calls() if c[:2] == ['compose', '--project-name'] and 'up' in c]
                self.assertEqual(len(restored), 1)
                self.assertEqual(restored[0][-4:], ['hub', 'xray', 'updater', 'caddy'])

    def test_unchanged_repeat_https_failure_leaves_prior_containers_and_identity(self):
        self.assert_ok(self.install('--prepare-only'))
        before = json.loads((self.destination / 'data/model/state.json').read_text())
        dotenv = (self.destination / '.env').read_bytes()
        self.log.write_text('')
        result = self.install(env=dict(self.env, FAKE_HTTPS_READY='0'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('已回滚', result.stderr)
        self.assertTrue(self.destination.exists())
        after = json.loads((self.destination / 'data/model/state.json').read_text())
        self.assertEqual(after['users'], before['users'])
        self.assertEqual(after['ws_path'], before['ws_path'])
        self.assertEqual((self.destination / '.env').read_bytes(), dotenv)
        self.assertFalse(any('down' in c or 'stop' in c for c in self.calls()))
        self.assertTrue(any('--no-recreate' in c for c in self.calls()))

    def test_standalone_https_failure_gates_commit_and_retains_private_log(self):
        result = self.install(env=dict(self.env, FAKE_HTTPS_READY='0'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('HTTPS / 节点运行同步待完成', result.stderr)
        self.assertIn('已回滚', result.stderr)
        self.assertFalse(self.destination.exists())
        self.assertNotIn('/sub/', result.stdout + result.stderr)
        stage = Path(result.stderr.split('备份/日志：')[1].strip())
        self.assertEqual((stage / 'phase').read_text().strip(), 'rolled-back')
        self.assertTrue((stage / 'failed-install/data/model/state.json').exists())
        call = next(c for c in self.calls() if 'finish' in c)
        self.assertIn('120', call)
        self.assertIn('--https-only', call)
        self.assertIn('--silent', call)
        self.assertNotIn('fake Docker', result.stdout + result.stderr)

    def test_pending_same_v1_external_install_retries_with_high_port_and_preserved_identity(self):
        self.assert_ok(self.install('--mode', 'existing-caddy', '--prepare-only'))
        before = json.loads((self.destination / 'data/model/state.json').read_text())
        # Original same-v1 template has none of the new orchestration commands.
        old = self.destination / 'compose.yaml'
        old.write_text(old.read_text().replace('HTTP01_ONLY: ${HTTP01_ONLY:-false}', 'HTTP01_ONLY: false'))
        host = self.fake_host()
        original_host = host.config.read_bytes()
        result = self.install('--manage-host-caddy', '--port', '45678',
                              env=dict(self.env, FAKE_BUSY_PORTS='80,443', FAKE_ACK='1'))
        self.assert_ok(result)
        after = json.loads((self.destination / 'data/model/state.json').read_text())
        self.assertEqual(after['users'], before['users'])
        self.assertEqual(after['ws_path'], before['ws_path'])
        self.assertEqual(after['settings']['mode'], 'standalone')
        self.assertEqual(after['settings']['port'], 45678)
        self.assertTrue(after['settings']['http01_only'])
        self.assertNotIn('COMPOSE_FILE=', '\n'.join(line for line in (self.destination / '.env').read_text().splitlines() if not line.startswith('#')))
        self.assertRegex(result.stdout, r':45678/[0-9a-f]{48}/clash')
        self.assertTrue(host.config.read_bytes().startswith(original_host))


    def join_hub(self):
        from proxy_stack.storage import Store
        from proxy_stack.control import Hub
        from proxy_stack.model import Settings
        hub = Store(self.root / 'hub-data')
        hub.init(Settings('hub.example.com', 'Hub', local_node=False))
        code = Hub(hub).code()
        code_file = self.root / 'join-code'
        code_file.write_text(code + '\n')
        self.env['FAKE_JOIN_HUB'] = str(hub.root)
        return hub, code, code_file

    def test_hub_only_role_and_node_join_headless(self):
        from proxy_stack.control import Hub
        hub, code, code_file = self.join_hub()
        result = self.install('--role', 'node', '--join-code-file', str(code_file), '--prepare-only')
        self.assert_ok(result)
        state = json.loads((self.destination / 'data/model/state.json').read_text())
        self.assertEqual(state['settings']['role'], 'node')
        self.assertEqual(state['users'], {})
        peer = json.loads((self.destination / 'data/model/peer.json').read_text())
        self.assertEqual(peer['origin'], 'https://hub.example.com')
        self.assertEqual(Hub(hub).status()[0]['status'], 'pending')
        for secret in (code, peer['token']):
            self.assertNotIn(secret, self.log.read_text())
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertNotIn('owner 私密订阅', result.stdout)
        self.assertIn('未检查公网就绪', result.stdout)
        repeated = self.install('--prepare-only')
        self.assert_ok(repeated)
        self.assertEqual(json.loads((self.destination / 'data/model/peer.json').read_text()), peer)

    def test_failed_join_rolls_back_fresh_install_and_preserves_private_backup(self):
        hub, code, code_file = self.join_hub()
        failed = self.install('--role', 'node', '--join-code-file', str(code_file), '--prepare-only',
                              env=dict(self.env, FAKE_JOIN_FAIL='1'))
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('已回滚', failed.stderr)
        self.assertFalse(self.destination.exists())
        transaction = Path(failed.stderr.split('备份/日志：')[1].strip())
        self.assertTrue((transaction / 'failed-install/data/model/state.json').exists())
        self.assertEqual((transaction / 'phase').read_text().strip(), 'rolled-back')
        self.assertTrue(any(c[:2] == ['compose', '--project-name'] and 'down' in c for c in self.calls()))
        self.assertNotIn(code, failed.stdout + failed.stderr + (transaction / 'operator.log').read_text())

    def test_node_ready_checks_public_wss_and_failed_public_tls_revokes_ready_enrollment(self):
        from proxy_stack.control import Hub
        hub, code, code_file = self.join_hub()
        result = self.install('--role', 'node', '--join-code-file', str(code_file),
                              env=dict(self.env, FAKE_NODE_ACK='1', FAKE_HTTPS_READY='0'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('已回滚', result.stderr)
        self.assertEqual(Hub(hub).status()[0]['status'], 'removed')
        self.assertFalse(self.destination.exists())
        self.assertNotIn('/sub/', result.stdout + result.stderr)
        invitation = Hub(hub).code()
        code_file.write_text(invitation + '\n')
        result = self.install('--role', 'node', '--join-code-file', str(code_file),
                              env=dict(self.env, FAKE_NODE_ACK='1'))
        self.assert_ok(result)
        self.assertIn('节点就绪：公共 HTTPS/WSS、本机运行及同步已确认', result.stdout)
        self.assertNotIn('/sub/', result.stdout)

    def test_offline_enrollment_rollback_keeps_scoped_identity_and_normal_retry_recovers(self):
        from proxy_stack.control import Hub
        hub, code, code_file = self.join_hub()
        result = self.install('--role', 'node', '--join-code-file', str(code_file),
                              env=dict(self.env, FAKE_HTTPS_READY='0', FAKE_CANCEL_FAIL='1'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--recover', result.stderr)
        lock = Path(str(self.destination) + '.install-lock')
        self.assertTrue(lock.exists())
        stage = Path((lock / 'owner').read_text().strip())
        self.assertEqual((stage / 'phase').read_text().strip(), 'recovery-required')
        self.assertTrue((self.destination / 'data/model/peer.json').exists())
        self.assertEqual(Hub(hub).status()[0]['status'], 'pending')
        result = self.install('--check', inputs=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(lock.exists())
        # The normal rerun discovers recovery automatically, revokes the scoped
        # enrollment, and then performs the requested fresh dry-run preflight.
        result = self.install(inputs=False)
        self.assert_ok(result)
        self.assertIn('上次事务已恢复', result.stdout)
        self.assertEqual(Hub(hub).status()[0]['status'], 'removed')
        self.assertFalse(lock.exists())
        self.assertTrue((stage / 'failed-install/data/model/enrollment-rollback.json').exists())
        self.assertNotIn(code, result.stdout + result.stderr + (stage / 'operator.log').read_text())

    def test_interactive_roles_use_tty_and_hub_only_has_no_local_node(self):
        installer_file = self.root / 'installer.sh'
        installer_file.write_text(self.payload)
        code, output = self.terminal(['bash', '-c', 'cat "$1" | bash -s -- --directory "$2" --port 34567 --prepare-only',
                                     'installer-test', str(installer_file), str(self.destination)],
                                    '2\nproxy.example.com\nHub Only\n')
        self.assertEqual(code, 0, output)
        self.assertIn('仅订阅中心', output)
        self.assertIn('1) 中心 + 本机节点（默认）\r\n  2)', output)
        self.assertIn('选择 [1]:', output)
        state = json.loads((self.destination / 'data/model/state.json').read_text())
        self.assertFalse(state['settings']['local_node'])
        self.assertEqual(state['nodes'], {})

    def test_v1_pull_and_explicit_image_update_preserve_actual_files(self):
        self.assert_ok(self.install('--prepare-only'))
        self.assertEqual(self.calls().count(['pull', 'ghcr.io/silaswei-io/proxy-stack:v1.0.0']), 1)
        before = {str(p.relative_to(self.destination)): p.read_bytes() for p in self.destination.rglob('*') if p.is_file()}
        self.log.write_text('')
        self.assert_ok(self.install('--prepare-only', inputs=False))
        self.assertFalse(any(call[0] == 'pull' for call in self.calls()))
        result = subprocess.run([str(self.destination / 'proxyctl'), 'upgrade'], env=dict(self.env, FAKE_ACK='0'),
                                text=True, capture_output=True, timeout=10)
        self.assert_ok(result)
        self.assertEqual(self.calls().count(['pull', 'ghcr.io/silaswei-io/proxy-stack:v1.0.0']), 1)
        self.assertIn(['compose','up','-d','--no-deps','--no-build','--pull','never','--force-recreate','hub','xray','updater','caddy'],self.calls())
        self.assertEqual(before, {str(p.relative_to(self.destination)):p.read_bytes() for p in self.destination.rglob('*') if p.is_file()})

    def legacy_image(self):
        dotenv = self.destination / '.env'
        dotenv.write_text(dotenv.read_text().replace('silaswei-io/proxy-stack', 'silaswei/proxy-stack'))
        compose = self.destination / 'compose.yaml'
        compose.write_text(compose.read_text().replace('silaswei-io/proxy-stack', 'silaswei/proxy-stack'))

    def test_same_v1_installer_migrates_old_official_namespace_and_keeps_identity(self):
        self.assert_ok(self.install('--prepare-only'))
        self.legacy_image()
        before = json.loads((self.destination / 'data/model/state.json').read_text())
        self.log.write_text('')
        self.assert_ok(self.install())
        self.assertIn('TOOL_IMAGE=ghcr.io/silaswei-io/proxy-stack:v1.0.0', (self.destination / '.env').read_text())
        after = json.loads((self.destination / 'data/model/state.json').read_text())
        for key in ('users', 'ws_path', 'settings'):
            self.assertEqual(before[key], after[key])
        self.assertIn(['pull', 'ghcr.io/silaswei-io/proxy-stack:v1.0.0'], self.calls())

    def test_upgrade_migrates_only_official_image_default_preserving_private_files(self):
        self.assert_ok(self.install('--prepare-only'))
        self.legacy_image()
        before = {str(p.relative_to(self.destination)): p.read_bytes() for p in self.destination.rglob('*') if p.is_file()}
        self.log.write_text('')
        result = subprocess.run([str(self.destination / 'proxyctl'), 'upgrade'], env=dict(self.env, FAKE_ACK='0'),
                                text=True, capture_output=True, timeout=10)
        self.assert_ok(result)
        after = {str(p.relative_to(self.destination)): p.read_bytes() for p in self.destination.rglob('*') if p.is_file()}
        self.assertEqual(after.pop('.env'), before.pop('.env').replace(b'silaswei/proxy-stack', b'silaswei-io/proxy-stack'))
        self.assertEqual(before, after)
        self.assertIn(['pull', 'ghcr.io/silaswei-io/proxy-stack:v1.0.0'], self.calls())

    def test_custom_and_wrong_version_images_rejected_before_mutation(self):
        self.assert_ok(self.install('--prepare-only'))
        before = {str(p.relative_to(self.destination)): p.read_bytes() for p in self.destination.rglob('*') if p.is_file()}
        for image in ('example/custom:v1.0.0', 'ghcr.io/silaswei/proxy-stack:v1.0.1',
                      'ghcr.io/silaswei-io/proxy-stack:latest'):
            self.log.write_text('')
            env = dict(self.env, FAKE_TOOL_IMAGE=image)
            result = subprocess.run([str(self.destination / 'proxyctl'), 'upgrade'], env=env,
                                    text=True, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotEqual(self.install(env=env).returncode, 0)
            self.assertFalse(any(call[0] == 'pull' or 'up' in call for call in self.calls()))
            self.assertEqual(before, {str(p.relative_to(self.destination)): p.read_bytes() for p in self.destination.rglob('*') if p.is_file()})
        dotenv = self.destination / '.env'
        dotenv.write_text(dotenv.read_text().replace('ghcr.io/silaswei-io/proxy-stack:v1.0.0', 'example/custom:v1.0.0'))
        self.assertNotEqual(self.install().returncode, 0)

    def test_upgrade_unresolvable_namespace_change_restores_dotenv(self):
        self.assert_ok(self.install('--prepare-only'))
        self.legacy_image()
        before = (self.destination / '.env').read_bytes()
        self.log.write_text('')
        result = subprocess.run([str(self.destination / 'proxyctl'), 'upgrade'],
                                env=dict(self.env, FAKE_TOOL_IMAGE='ghcr.io/silaswei/proxy-stack:v1.0.0'),
                                text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, (self.destination / '.env').read_bytes())
        self.assertFalse(any('up' in call for call in self.calls()))

    def test_refuses_nonempty_unmanaged_and_symlink_paths(self):
        self.destination.mkdir()
        important = self.destination / ".env"
        important.write_text("preserve me")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未受管", result.stderr)
        self.assertEqual(important.read_text(), "preserve me")
        important.unlink()
        self.destination.rmdir()
        target = self.root / "target"
        target.mkdir()
        self.destination.symlink_to(target, target_is_directory=True)
        self.assertNotEqual(self.install().returncode, 0)
        self.destination.unlink()
        parent = self.root / "alias"
        parent.symlink_to(target, target_is_directory=True)
        self.assertNotEqual(self.install("--directory", str(parent / "child")).returncode, 0)
        self.assertEqual(list(target.iterdir()), [])

    def test_rejects_symlink_managed_files_and_invalid_marker(self):
        self.assert_ok(self.install("--prepare-only"))
        marker = self.destination / ".proxy-stack-managed"
        marker.write_text("not our installer")
        self.assertNotEqual(self.install().returncode, 0)
        marker.write_text("proxy-stack-installer-v1\n")
        config = self.destination / ".env"
        config.rename(self.destination / "saved.env")
        config.symlink_to("saved.env")
        self.assertNotEqual(self.install().returncode, 0)

    def test_invalid_inputs_fail_without_writes(self):
        for args in (("--domain", "https://bad.example.com"), ("--name", "a,b"),
                     ("--name", "$(touch /tmp/never-execute)"), ("--name", "VPS"),
                     ("--domain", "bad.example.com\nEVIL=x"), ("--mode", "bad"), ("--domain",)):
            with self.subTest(args=args):
                self.assertNotEqual(self.install(*args).returncode, 0)
                self.assertFalse(self.destination.exists())
        self.assertNotEqual(self.install(inputs=False, env=dict(self.env, FAKE_IP_FAILURE="1")).returncode, 0)
        no_tty = subprocess.run(["bash", "-s", "--", "--directory", str(self.destination)], input=self.payload,
                                text=True, capture_output=True, env=self.env, start_new_session=True, timeout=5)
        self.assertNotEqual(no_tty.returncode, 0)
        self.assertIn("无终端", no_tty.stderr)

    def test_omitted_headless_inputs_use_public_ip_and_host_hostname(self):
        result = self.install("--prepare-only", inputs=False)
        self.assert_ok(result)
        state = json.loads((self.destination / "data/model/state.json").read_text())
        self.assertEqual(state["settings"]["domain"], "8.8.8.8")
        self.assertEqual(state["settings"]["node_name"], "test-host_01")
        self.assertIn("未检查公网就绪", result.stdout)
        self.assertFalse(any("finish" in c or "verify" in c or "up" in c for c in self.calls()))
        self.assertNotIn("A/AAAA", result.stdout)
        self.log.write_text("")
        self.assert_ok(self.install(inputs=False, env=dict(self.env, FAKE_IP_FAILURE="1")))
        self.assertFalse(any("detect-public-ip" in c or "default-node-name" in c for c in self.calls()))

    def test_explicit_address_skips_discovery_even_offline(self):
        result = self.install("--domain", "2606:4700:4700::1111", "--prepare-only",
                              inputs=False, env=dict(self.env, FAKE_IP_FAILURE="1", FAKE_HOSTNAME="DIRECT"))
        self.assert_ok(result)
        state = json.loads((self.destination / "data/model/state.json").read_text())
        self.assertEqual(state["settings"]["node_name"], "Server-DIRECT")
        self.assertNotIn("/sub/", result.stdout)
        self.assertIn("未检查公网就绪", result.stdout)
        self.assertFalse(any("detect-public-ip" in c for c in self.calls()))

    def test_hostname_sanitized_without_executing_and_explicit_name_strict(self):
        marker = self.root / "never-execute"
        hostile = "$(touch " + str(marker) + "),\n[mitm]" + "x" * 200
        result = self.install("--domain", "proxy.example.com", "--prepare-only", inputs=False,
                              env=dict(self.env, FAKE_HOSTNAME=hostile))
        self.assert_ok(result)
        self.assertFalse(marker.exists())
        state = json.loads((self.destination / "data/model/state.json").read_text())
        name = state["settings"]["node_name"]
        self.assertLessEqual(len(name), 80)
        self.assertNotIn("$", name)
        self.assertNotIn("\n", name)

    def test_enter_twice_accepts_displayed_defaults(self):
        installer_file = self.root / "installer.sh"
        installer_file.write_text(self.payload)
        code, output = self.terminal(["bash", "-c", 'cat "$1" | bash -s -- --directory "$2" --port 34567 --prepare-only',
                                     "installer-test", str(installer_file), str(self.destination)], "1\n\n\n")
        self.assertEqual(code, 0, output)
        self.assertIn("回车使用 8.8.8.8", output)
        self.assertIn("回车使用 test-host_01", output)
        state = json.loads((self.destination / "data/model/state.json").read_text())
        self.assertEqual(state["settings"]["domain"], "8.8.8.8")
        self.assertEqual(state["settings"]["node_name"], "test-host_01")

    def test_probe_failure_allows_explicit_terminal_override(self):
        self.env["FAKE_IP_FAILURE"] = "1"
        installer_file = self.root / "installer.sh"
        installer_file.write_text(self.payload)
        code, output = self.terminal(["bash", "-c", 'cat "$1" | bash -s -- --directory "$2" --port 34567 --prepare-only',
                                     "installer-test", str(installer_file), str(self.destination)],
                                    "1\nproxy.example.com\n\n")
        self.assertEqual(code, 0, output)
        self.assertIn("探测失败，请显式输入", output)

    def test_docker_and_compose_requirements_fail_helpfully(self):
        for changes, message in (({"FAKE_DOCKER_DOWN": "1"}, "Docker 不可用"),
                                 ({"FAKE_COMPOSE_VERSION": "2.24.3"}, "Compose >= 2.24.4"),
                                 ({"FAKE_COMPOSE_VERSION": "unknown"}, "Compose 版本")):
            result = self.install(env=dict(self.env, **changes))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(message, result.stderr)
            self.assertFalse(self.destination.exists())

    def fake_host(self):
        from test_host_setup import HostFixture
        host_root = self.root / 'host'
        host = HostFixture(host_root)
        wrapper = self.bin / 'python3'
        wrapper.write_text('#!' + sys.executable + '\nimport sys,importlib.util\nsys.path.insert(0, ' + repr(str(Path(__file__).parent)) + ')\n'
                           'from test_host_setup import fixture_main\n'
                           'spec=importlib.util.spec_from_file_location("transaction_helper",sys.argv[1])\n'
                           'helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)\n'
                           'sys.exit(fixture_main(' + repr(str(host_root)) + ', sys.argv[2:],helper))\n')
        wrapper.chmod(0o700)
        return host

    def test_occupied_80_host_plan_opt_in_apply_and_https_rollback(self):
        host = self.fake_host()
        original = host.config.read_bytes()
        binary = host.binary.read_bytes()
        env = dict(self.env, FAKE_BUSY_PORTS='80', FAKE_HTTPS_READY='1', FAKE_ACK='1')
        result = self.install('--check', env=env)
        self.assert_ok(result)
        self.assertIn('HTTP-01 转发', result.stdout)
        self.assertFalse(self.destination.exists())
        self.assertEqual(host.config.read_bytes(), original)
        result = self.install(env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--manage-host-caddy', result.stderr)
        self.assertFalse(self.destination.exists())
        result = self.install('--manage-host-caddy', env=dict(env, FAKE_HTTPS_READY='0'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('已回滚', result.stderr)
        self.assertFalse(self.destination.exists())
        self.assertEqual(host.config.read_bytes(), original)
        self.assertEqual(host.binary.read_bytes(), binary)
        self.assertNotIn('/sub/', result.stdout + result.stderr)
        transaction = Path(result.stderr.split('备份/日志：')[1].strip())
        self.assertEqual((transaction / 'phase').read_text().strip(), 'rolled-back')
        self.assertEqual(json.loads((transaction / 'host/journal.json').read_text())['phase'], 'rolled-back')
        self.assertTrue(any(c[:2] == ['compose', '--project-name'] and 'down' in c for c in self.calls()))
        result = self.install('--manage-host-caddy', env=env)
        self.assert_ok(result)
        self.assertIn('订阅就绪', result.stdout)
        self.assertRegex(result.stdout, r':34567/[0-9a-f]{48}/clash')
        self.assertNotIn('Quantumult X 完整配置', result.stdout)
        state = (self.destination / 'data/model/state.json').read_bytes()
        dotenv = (self.destination / '.env').read_bytes()
        self.assertTrue(host.config.read_bytes().startswith(original))
        self.assertEqual(host.binary.read_bytes(), binary)
        # Rerun keeps the chosen port, identity and exact managed challenge block.
        configured = host.config.read_bytes()
        self.assert_ok(self.install('--manage-host-caddy', '--port', '45678', env=env))
        self.assertEqual(host.config.read_bytes(), configured)
        changed = json.loads((self.destination / 'data/model/state.json').read_text())
        previous = json.loads(state)
        self.assertEqual(changed['settings']['port'], 45678)
        self.assertEqual(changed['users'], previous['users'])
        self.assertEqual(changed['ws_path'], previous['ws_path'])
        self.assertIn(b'PUBLIC_PORT=45678', (self.destination / '.env').read_bytes())
        self.assertEqual(host.config.read_bytes(), configured)

    def test_unknown_80_and_host_candidate_failure_precede_install_writes(self):
        env = dict(self.env, FAKE_BUSY_PORTS='80')
        result = self.install('--manage-host-caddy', env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.destination.exists())
        host = self.fake_host()
        original = host.config.read_bytes()
        result = self.install('--manage-host-caddy', env=dict(env, FAKE_HOST_VALIDATE_FAIL='1'))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.destination.exists())
        self.assertEqual(host.config.read_bytes(), original)

    def legacy_recovery(self):
        host = self.fake_host()
        result = self.install('--manage-host-caddy',
                              env=dict(self.env, FAKE_BUSY_PORTS='80', FAKE_HOST_RELOAD_FAIL='1'))
        self.assertNotEqual(result.returncode, 0)
        lock = Path(str(self.destination) + '.install-lock')
        stage = Path((lock / 'owner').read_text().strip())
        self.assertEqual((stage / 'phase').read_text().strip(), 'recovery-required')
        legacy = (Path(__file__).parent / 'fixtures/host_setup-611082f.py').read_bytes()
        import hashlib
        self.assertEqual(hashlib.sha256(legacy).hexdigest(), '4e58b55786b068d5f664d6354b9b9a1219514f449a81d71c64aacbdfb31cbaee')
        (stage / 'host_setup.py').write_bytes(legacy)
        journal = stage / 'host/journal.json'
        value = json.loads(journal.read_text())
        value['service']['service'] = dict(host.info)  # original raw systemctl strings
        journal.write_text(json.dumps(value))
        return host, stage, lock, legacy

    def test_current_recover_migrates_exact_legacy_helper_retains_bytes_and_checks_drift(self):
        host, stage, lock, legacy = self.legacy_recovery()
        # Reproduce the original failure using the actual old embedded helper.
        old = subprocess.run([str(self.bin / 'python3'), str(stage / 'host_setup.py'),
                              'rollback', '--stage', str(stage / 'host')],
                             env=self.env, text=True, capture_output=True, timeout=10)
        self.assertNotEqual(old.returncode, 0)
        self.assertIn('外部 unit 变更', old.stderr)
        original_unit = host.unit.read_bytes()
        host.unit.write_bytes(original_unit + b'# external drift\n')
        result = self.install('--recover', inputs=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(lock.exists())
        self.assertTrue((stage / 'host/original').exists())
        self.assertEqual((stage / 'host_setup.611082f.original.py').read_bytes(), legacy)
        self.assertEqual((stage / 'host_setup.py').read_bytes(), files('proxy_stack').joinpath('host_setup.py').read_bytes())
        host.unit.write_bytes(original_unit)
        self.assert_ok(self.install('--recover', inputs=False))
        self.assertFalse(lock.exists())
        self.assertFalse(self.destination.exists())
        self.assertEqual((stage / 'phase').read_text().strip(), 'rolled-back')
        self.assertEqual(json.loads((stage / 'host/journal.json').read_text())['phase'], 'rolled-back')
        self.assertEqual((stage / 'host_setup.611082f.original.py').read_bytes(), legacy)

    def test_recovery_refuses_unknown_symlink_helper_and_conflicting_backup(self):
        host, stage, lock, legacy = self.legacy_recovery()
        helper = stage / 'host_setup.py'
        helper.write_bytes(legacy + b'\nraise RuntimeError("untrusted helper executed")\n')
        result = self.install('--recover', inputs=False)
        self.assertIn('未知恢复 helper', result.stderr)
        self.assertEqual(helper.read_bytes(), legacy + b'\nraise RuntimeError("untrusted helper executed")\n')
        helper.unlink()
        helper.symlink_to(stage / 'install/host_setup.py')
        result = self.install('--recover', inputs=False)
        self.assertIn('helper 不安全', result.stderr)
        helper.unlink()
        helper.write_bytes(legacy)
        helper.chmod(0o600)
        backup = stage / 'host_setup.611082f.original.py'
        backup.write_bytes(b'unknown backup')
        result = self.install('--recover', inputs=False)
        self.assertIn('备份冲突', result.stderr)
        self.assertEqual(helper.read_bytes(), legacy)
        self.assertEqual(backup.read_bytes(), b'unknown backup')
        self.assertTrue(lock.exists())

    def test_only_443_busy_needs_no_host_integration_and_explicit_port_conflicts_fail(self):
        result = self.install('--check', env=dict(self.env, FAKE_BUSY_PORTS='443'))
        self.assert_ok(result)
        self.assertIn('HTTPS proxy.example.com:34567', result.stdout)
        self.assertFalse(self.destination.exists())
        result = self.install('--port', '443', env=dict(self.env, FAKE_BUSY_PORTS='443'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('443 已占用', result.stderr)
        self.assertFalse(self.destination.exists())
        for value in ('abc', '0', '65536', '-1', '80'):
            self.assertNotEqual(self.install('--port', value).returncode, 0)
            self.assertFalse(self.destination.exists())

    def test_explicit_expert_prepare_retains_snippet_and_never_starts_or_probes_https(self):
        result = self.install('--mode', 'existing-caddy', '--prepare-only')
        self.assert_ok(result)
        self.assertIn('TLS/global', result.stdout)
        self.assertNotIn('/sub/', result.stdout)
        self.assertIn('COMPOSE_FILE=compose.yaml:compose.external.yaml', (self.destination / '.env').read_text())
        self.assertIn('reverse_proxy 127.0.0.1:18090', (self.destination / 'data/runtime/host-caddy.snippet').read_text())
        self.assertFalse(any('up' in c or 'finish' in c or 'verify' in c for c in self.calls()))

    def test_check_and_candidate_failure_have_no_install_writes(self):
        result = self.install('--check')
        self.assert_ok(result)
        self.assertIn('未激活服务', result.stdout)
        self.assertFalse(self.destination.exists())
        self.assertFalse(any('init' in c or 'up' in c for c in self.calls()))
        result = self.install(env=dict(self.env, FAKE_CANDIDATE_FAIL='1'))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.destination.exists())

    def test_public_roundtrip_failure_is_early_cleans_both_and_preserves_original(self):
        host = self.fake_host()
        original = host.config.read_bytes()
        for args in (('--port', '34567'), ()):
            self.log.write_text('')
            result = self.install('--check', *args, inputs=bool(args),
                                  env=dict(self.env, FAKE_ROUNDTRIP_FAIL='1', FAKE_BUSY_PORTS='80'))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('公共地址回环失败', result.stderr)
            self.assertIn('NAT', result.stderr)
            self.assertFalse(self.destination.exists())
            self.assertFalse(Path(str(self.destination) + '.install-lock').exists())
            self.assertEqual(host.config.read_bytes(), original)
            self.assertFalse(host.events.exists())
            calls = self.calls()
            checks = [c for c in calls if any(a.startswith('ROUNDTRIP_HOST=') for a in c)]
            self.assertEqual(len(checks), 1)  # never scan for another reachable port
            names = [c[c.index('--name') + 1] for c in calls if c[0] == 'run' and any(a.startswith('ROUNDTRIP_NONCE=') for a in c)]
            self.assertEqual(len(names), 2)
            self.assertTrue(any(c[:2] == ['rm', '-f'] and all(n in c for n in names) for c in calls))
            self.assertFalse(any('init' in c or 'up' in c or 'preview-caddy' in c for c in calls))
        self.assert_ok(self.install('--prepare-only'))
        before = (self.destination / 'data/model/state.json').read_bytes()
        result = self.install('--port', '45678', env=dict(self.env, FAKE_ROUNDTRIP_FAIL='1'))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.destination / 'data/model/state.json').read_bytes(), before)

    def test_roundtrip_interruption_cleans_listener_and_probe_before_mutation(self):
        installer = self.root / 'installer.sh'
        installer.write_text(self.payload)
        process = subprocess.Popen(['bash', str(installer), '--directory', str(self.destination),
                                    '--non-interactive', '--domain', '8.8.8.8', '--port', '34567'],
                                   env=dict(self.env, FAKE_ROUNDTRIP_PAUSE='1'), start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if self.log.exists() and any(any(a.startswith('ROUNDTRIP_NONCE=') for a in c) for c in self.calls()):
                    break
                time.sleep(.03)
            else: self.fail('listener did not start')
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=15)
            self.assertNotEqual(process.returncode, 0)
            self.assertFalse(self.destination.exists())
            self.assertTrue(any(c[:2] == ['rm', '-f'] and any('roundtrip-listener-' in a for a in c)
                                and any('roundtrip-probe-' in a for a in c) for c in self.calls()))
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
    def test_port_recheck_and_bounded_random_failure_leave_no_install(self):
        result = self.install(env=dict(self.env, FAKE_RACE_PORT='34567'))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.destination.exists())
        self.assertIn('预检后被占用', result.stderr)
        self.log.write_text('')
        # Exhausting all 24 probes launches 48 fake Docker processes. Allow
        # slower CI runners to finish the bounded search and assert its result.
        result = self.install(inputs=False, env=dict(self.env, FAKE_BUSY_ALL_HIGH='1'), timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('24 次', result.stderr)
        self.assertFalse(self.destination.exists())
        self.assertEqual(sum('-p' in c for c in self.calls()), 24)

    def test_random_port_default_is_available_and_persisted(self):
        result = self.install('--prepare-only', inputs=False)
        self.assert_ok(result)
        state = json.loads((self.destination / 'data/model/state.json').read_text())
        self.assertGreaterEqual(state['settings']['port'], 20000)
        self.assertLess(state['settings']['port'], 60000)
        self.assertTrue(state['settings']['http01_only'])
        before = (self.destination / '.env').read_bytes()
        self.assert_ok(self.install('--prepare-only', inputs=False))
        self.assertEqual((self.destination / '.env').read_bytes(), before)
        self.assert_ok(self.install('--port', '12345', inputs=False))
        changed = json.loads((self.destination / 'data/model/state.json').read_text())
        self.assertEqual(changed['settings']['port'], 12345)
        self.assertEqual(changed['users'], state['users'])
        self.assertEqual(changed['ws_path'], state['ws_path'])

    def test_init_failure_cleans_own_network_before_up_and_preserves_private_backup(self):
        result = self.install(env=dict(self.env, FAKE_INIT_FAIL='1'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('已回滚', result.stderr)
        self.assertFalse(self.destination.exists())
        stage = Path(result.stderr.split('备份/日志：')[1].strip())
        self.assertTrue((stage / 'resources-starting').exists())
        self.assertTrue((stage / 'env.sha256').exists())
        self.assertTrue((stage / 'failed-install/data/model/state.json').exists())
        cleanup = [c for c in self.calls() if c[:2] == ['compose', '--project-name'] and 'down' in c]
        self.assertEqual(len(cleanup), 1)
        project = next(line.split('=', 1)[1] for line in (stage / 'failed-install/.env').read_text().splitlines()
                       if line.startswith('COMPOSE_PROJECT_NAME='))
        self.assertEqual(cleanup[0][2], project)
        self.assertIn('--volumes', cleanup[0])
        self.assertFalse(any('up' in c for c in self.calls()))

    def test_local_runtime_failure_never_commits_or_prints_private_links(self):
        result = self.install(env=dict(self.env, FAKE_ACK='0'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('节点运行同步待完成', result.stderr)
        self.assertFalse(self.destination.exists())
        self.assertNotIn('/sub/', result.stdout + result.stderr)
        self.assertIn('失败（node）', result.stdout)
        stage = Path(result.stderr.split('备份/日志：')[1].strip())
        diagnostics = stage / 'caddy-diagnostics.log'
        self.assertEqual(diagnostics.stat().st_mode & 0o777, 0o600)
        self.assertIn('fake scoped Caddy certificate diagnostic', diagnostics.read_text())
        self.assertNotIn('fake scoped', result.stdout + result.stderr)
        calls = self.calls()
        self.assertLess(next(i for i, c in enumerate(calls) if 'logs' in c),
                        next(i for i, c in enumerate(calls) if 'down' in c))

    def test_rootless_remote_and_nonlinux_fail_before_install_writes(self):
        for changes in ({'FAKE_DOCKER_ENDPOINT': 'ssh://elsewhere'}, {'FAKE_DOCKER_SECURITY': '["name=rootless"]'}):
            with self.subTest(changes=changes):
                result = self.install('--check', env=dict(self.env, **changes))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.destination.exists())
                self.assertFalse(any(c[0] == 'run' for c in self.calls()))
        uname = self.bin / 'uname'
        uname.write_text('#!/bin/sh\necho Darwin\n')
        uname.chmod(0o700)
        result = self.install('--check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('仅支持 Linux', result.stderr)
        self.assertFalse(self.destination.exists())

    def test_owns_binding_normalizes_ipv6_brackets_on_unchanged_repeat(self):
        self.assert_ok(self.install('--prepare-only', '--domain', '2606:4700:4700::1111'))
        result = self.install('--check', inputs=False,
                              env=dict(self.env, FAKE_OWNED_CADDY='1', FAKE_OWNED_DIRECTORY=str(self.destination),
                                       FAKE_BUSY_PORTS='34567,80,[::]:80',
                                       FAKE_BINDINGS='34567/tcp :: 34567\n80/tcp :: 80'))
        self.assert_ok(result)
        self.assertIn('HTTP-01 [::]:80', result.stdout)
        self.assertNotIn('ROUNDTRIP_NONCE=', self.log.read_text())

    def test_owned_binding_skips_listener_but_completion_is_still_required(self):
        self.assert_ok(self.install('--prepare-only'))
        before = (self.destination / 'data/model/state.json').read_bytes()
        self.log.write_text('')
        result = self.install(inputs=False, env=dict(self.env, FAKE_OWNED_CADDY='1',
                              FAKE_OWNED_DIRECTORY=str(self.destination), FAKE_BUSY_PORTS='34567,80',
                              FAKE_BINDINGS='34567/tcp 0.0.0.0 34567\n80/tcp 0.0.0.0 80', FAKE_HTTPS_READY='0'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('跳过临时监听', result.stdout)
        self.assertIn('失败（connect）', result.stdout)
        self.assertNotIn('ROUNDTRIP_NONCE=', self.log.read_text())
        self.assertTrue(any('finish' in c for c in self.calls()))
        after = json.loads((self.destination / 'data/model/state.json').read_text())
        for key in ('users', 'ws_path', 'settings'):
            self.assertEqual(after[key], json.loads(before)[key])

    def test_killed_installer_normal_rerun_and_recover_discover_journal(self):
        installer = self.root / 'installer.sh'
        installer.write_text(self.payload)
        process = subprocess.Popen(['bash', str(installer), '--directory', str(self.destination),
                                    '--non-interactive', '--domain', '8.8.8.8', '--name', 'Crash', '--port', '34567'],
                                   env=dict(self.env, FAKE_UP_PAUSE='1'), start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        lock = Path(str(self.destination) + '.install-lock')
        stage = None
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if (lock / 'owner').exists():
                    stage = Path((lock / 'owner').read_text().strip())
                    if (stage / 'resources-starting').exists():
                        break
                time.sleep(.03)
            else:
                self.fail('installer did not reach activation')
            active = self.install('--recover', inputs=False)
            self.assertNotEqual(active.returncode, 0)
            self.assertIn('仍在运行', active.stderr)
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
            checked = self.install('--check', inputs=False)
            self.assertNotEqual(checked.returncode, 0)
            self.assertIn('--recover', checked.stderr)
            self.assertTrue(self.destination.exists())
            self.assertNotEqual((stage / 'phase').read_text().strip(), 'rolled-back')
            recovered = self.install('--recover', inputs=False)
            self.assert_ok(recovered)
            self.assertFalse(self.destination.exists())
            self.assertFalse(lock.exists())
            self.assertEqual((stage / 'phase').read_text().strip(), 'rolled-back')
            self.assert_ok(self.install('--recover', inputs=False))
            self.assert_ok(self.install())
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)

    def terminal(self, command, input_text):
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.root)
            os.execvpe(command[0], command, self.env)
        output = b""
        try:
            staged = list(input_text) if not isinstance(input_text, str) else []
            scan_from = 0
            if isinstance(input_text, str):
                os.write(fd, input_text.encode())
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if select.select([fd], [], [], 0.1)[0]:
                    try:
                        data = os.read(fd, 65536)
                        if not data:
                            break
                        output += data
                        if staged and staged[0][0].encode() in output[scan_from:]:
                            time.sleep(0.05)
                            os.write(fd, staged.pop(0)[1].encode())
                            scan_from = len(output)
                    except OSError:
                        break
                exited, status = os.waitpid(pid, os.WNOHANG)
                if exited:
                    return os.waitstatus_to_exitcode(status), output.decode()
            else:
                os.kill(pid, 9)
                self.fail("terminal command hung")
            _, status = os.waitpid(pid, 0)
            return os.waitstatus_to_exitcode(status), output.decode()
        finally:
            os.close(fd)

    def test_join_wizard_and_retry_menu_read_terminal_with_piped_stdin(self):
        hub, invitation, code_file = self.join_hub()
        installer_file = self.root / 'installer.sh'
        installer_file.write_text(self.payload)
        command = ['bash','-c','cat "$1" | bash -s -- --directory "$2" --port 34567 --prepare-only',
                   'installer-test',str(installer_file),str(self.destination)]
        code, output = self.terminal(command,[('此服务器：','3\n'),('代理域名','node.example.com\n'),
                                             ('节点名称','Joined Node\n'),('粘贴限时加入码',invitation+'\n')])
        self.assertEqual(code,0,output)
        self.assertNotIn(invitation,output)
        self.assertNotIn(invitation,self.log.read_text())
        peer_file = self.destination / 'data/model/peer.json'
        self.assertTrue(peer_file.exists())
        # Detach using real hub state in this fake-Docker UI test, then exercise
        # the menu with stdin still being a pipe (as on repeated installation).
        from proxy_stack.control import Hub
        peer = json.loads(peer_file.read_text())
        Hub(hub).remove(peer['id'])
        peer_file.unlink()
        invitation = Hub(hub).code()
        code, output = self.terminal(['bash','-c','printf "unused pipe" | "$1" menu','menu-test',
                                     str(self.destination/'proxyctl')],
                                    [('选择:','11\n'),('粘贴限时加入码',invitation+'\n'),('选择:','0\n')])
        self.assertEqual(code,0,output)
        self.assertNotIn(invitation,output)
        self.assertTrue(peer_file.exists())

    def test_menu_operations_use_existing_validation_and_restart(self):
        self.assert_ok(self.install("--prepare-only"))
        owner = json.loads((self.destination / "data/model/state.json").read_text())["users"]["owner"]
        code, output = self.terminal([str(self.destination / "proxyctl"), "menu"],
                                     "1\n2\nalice\n2\nBad Name\n5\n6\n3\nalice\ny\n0\n")
        self.assertEqual(code, 0, output)
        self.assertIn("Mihomo / Clash Meta", output)
        self.assertIn("invalid username value", output)
        # No live update in this fake: update-rules is patched below to bootstrap.
        state = json.loads((self.destination / "data/model/state.json").read_text())
        self.assertEqual(state["users"], {"owner": owner})
        self.assertEqual(self.calls().count(["compose", "restart", "xray"]), 0)
        self.assertIn("pending/synced/offline", output)
        self.assertTrue(any("status" in c for c in self.calls()))
        self.assertTrue(any("update-rules" in c for c in self.calls()))
        headless = subprocess.run([str(self.destination / "proxyctl"), "menu"], env=self.env,
                                  start_new_session=True, text=True, capture_output=True, timeout=5)
        self.assertNotEqual(headless.returncode, 0)
        self.assertIn("交互终端", headless.stderr)

    def test_two_prompts_read_tty_when_script_arrives_via_pipe(self):
        installer_file = self.root / "installer.sh"
        installer_file.write_text(self.payload)
        # The bash stdin is a pipe while the answers arrive on its controlling TTY.
        code, output = self.terminal(["bash", "-c", 'cat "$1" | bash -s -- --directory "$2" --port 34567 --prepare-only',
                                      "installer-test", str(installer_file), str(self.destination)],
                                     "1\nproxy.example.com\n我的 节点\n")
        self.assertEqual(code, 0, output)
        self.assertIn("代理域名", output)
        self.assertIn("节点名称", output)
        state = json.loads((self.destination / "data/model/state.json").read_text())
        self.assertEqual(state["settings"]["node_name"], "我的 节点")


if __name__ == "__main__":
    unittest.main()
