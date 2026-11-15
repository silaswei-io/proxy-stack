#!/usr/bin/env python3
"""Real Compose lifecycle with a boundary mapping all ports to random loopback.

No public ACME/DNS or host80/443. A normal bridge is required: Docker 29
internal networks retain PortBindings but suppress actual published ports. Only this fixture adds an internal issuer and
trusts its root; production validation and both normal deployment files remain
unmodified. Runtime model, published aliases, overlay and transactions are real.
"""
import json
import re
import http.client
import socket
import ssl
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'manager/src'))
from proxy_stack import domains
from proxy_stack.model import Settings
from proxy_stack.storage import Store

TOOL = 'proxy-stack-tool:v1.0.0'


class IsolatedDeployment(domains.Deployment):
    def __init__(self, directory):
        super().__init__(directory)
        self.inject_failure = False

    def run(self, argv, timeout=30):
        if argv[:2] == ['docker', 'compose']:
            # This sole boundary replaces public binds with loopback ephemeral
            # mappings, retains the real target :443 and uses an internal CA.
            overlay = (self.directory / domains.OVERLAY).exists()
            boundary = {
                'services': {
                    'caddy': {
                        'ports': ['127.0.0.1::2096'] + (['127.0.0.1::443'] if overlay else []),
                        'entrypoint': ['/bin/sh', '-c'],
                        'command': ["sed '/admin off/a\\    local_certs\\n    skip_install_trust' /etc/caddy/Caddyfile > /tmp/Caddyfile; exec caddy run --config /tmp/Caddyfile --adapter caddyfile"],
                        'tmpfs': ['/tmp'],
                        'networks': {'default': {'aliases': ['origin.example.com', 'subscriptions.example.com']}},
                    },
                    'manager': {
                        'environment': {'SSL_CERT_FILE': '/certdata/caddy/pki/authorities/local/root.crt'},
                        'volumes': ['caddy_data:/certdata:ro'],
                    },
                },
                'networks': {'default': {'internal': False}},
            }
            # !override is needed only for the test's isolated port boundary.
            text = json.dumps(boundary)
            (self.directory / 'fixture.yaml').write_text(text.replace('"ports": [', '"ports": !override ['))
            files = ['-f', 'compose.yaml']
            if overlay:
                files += ['-f', domains.OVERLAY]
            argv = ['docker', 'compose', *files, '-f', 'fixture.yaml', *argv[2:]]
        return super().run(argv, timeout)

    def port_free(self, port):
        assert port == 443
        return True  # The test must never try binding real host443.

    def confirm(self, origin, wait=120):
        if self.inject_failure:
            if self.inject_failure == 'stop':
                self.compose('stop', 'caddy')
            self.inject_failure = False
            raise ValueError('injected post-activation failure')
        deadline = time.monotonic() + 20
        while True:
            try:
                result = json.loads(self.compose('run', '--rm', '-T', '--no-deps', 'manager',
                                                 'verify', '--base-url', origin, timeout=20))
                assert result['tls_chain_verified']
                return
            except ValueError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.1)


def installer_config():
    # Execute the exported installer rerun regression in Linux, then ask the
    # REAL Compose parser to resolve its resulting dotenv/base/alias overlay.
    # The unit fixture substitutes daemon ownership, never the Bash payload.
    with tempfile.TemporaryDirectory(prefix='domain-installer-config-') as temporary:
        output = Path(temporary).resolve()
        code = """import sys,shutil
from pathlib import Path
sys.path.insert(0,'/tests')
from test_installer import InstallerTests
case=InstallerTests('test_installer_refresh_retains_subscription_port_overlay_and_state')
case.setUp()
try:
 case.test_installer_refresh_retains_subscription_port_overlay_and_state()
 for name in ('.env','compose.yaml','compose.external.yaml','compose.subscription.yaml'):
  shutil.copyfile(case.destination/name,Path('/output')/name)
finally: case.doCleanups()
"""
        subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python',
                        '-v', str(ROOT / 'tests') + ':/tests:ro', '-v', str(output) + ':/output',
                        '-v', str(ROOT / 'manager/src/proxy_stack') + ':/app/proxy_stack:ro',
                        TOOL, '-c', code], check=True, timeout=45)
        config = json.loads(subprocess.check_output(['docker', 'compose', 'config', '--format', 'json'], cwd=output))
        ports = config['services']['caddy']['ports']
        assert {p['target'] for p in ports} == {80, 34567, 443}
        assert any(p['target'] == 443 and p['published'] == '443' for p in ports)
        print('PASS: exported installer rerun preserves alias state/overlay; real Compose resolves original HTTPS, HTTP-01 and added443')


def main():
    installer_config()
    with tempfile.TemporaryDirectory(prefix='domain-compose-') as temporary:
        directory = Path(temporary).resolve()
        for name in ('compose.yaml', 'compose.external.yaml'):
            shutil.copyfile(ROOT / name, directory / name)
        project = 'domain-test-' + uuid.uuid4().hex[:12]
        (directory / '.env').write_text('DOMAIN=origin.example.com\nNODE_NAME=Hub\nPUBLIC_PORT=2096\n'
                                      'HTTP_BIND=127.0.0.1\nHTTP_PORT=40881\nMODE=standalone\nLOCAL_NODE=false\nROLE=hub\n'
                                      'TOOL_IMAGE=' + TOOL + '\nCOMPOSE_PROJECT_NAME=' + project + '\n')
        deployment = IsolatedDeployment(directory)
        store = deployment.store
        settings = Settings('origin.example.com', 'Hub', port=2096, local_node=False)
        store.init(settings)
        store.mutate_user('add', 'friend')
        original = store.state()
        try:
            deployment.compose('up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'caddy', timeout=60)
            deployment.confirm(settings.origin)
            with patch.object(domains.discovery, 'check_address'):
                deployment.operate('add', 'subscriptions.example.com')
                assert store.state()['users'] == original['users']
                assert store.state()['nodes'] == original['nodes']
                assert store.state()['generation'] == original['generation']
                assert (directory / domains.OVERLAY).exists()
                # Resolve the exact production files separately from the
                # fixture's ephemeral port mapping (configuration only).
                effective = json.loads(subprocess.check_output(['docker', 'compose', 'config', '--format', 'json'], cwd=directory))
                production_ports = effective['services']['caddy']['ports']
                assert {p['target'] for p in production_ports} == {80, 2096, 443}
                assert any(p['target'] == 80 and p['published'] == '40881' and p['host_ip'] == '127.0.0.1' for p in production_ports)
                config = json.loads(deployment.compose('config', '--format', 'json'))
                ports = config['services']['caddy']['ports']
                assert {p['target'] for p in ports} == {443, 2096}
                assert all(p['host_ip'] == '127.0.0.1' and not p.get('published') for p in ports)
                ident = deployment.compose('ps', '-q', 'caddy').strip()
                bindings = json.loads(deployment.run(['docker', 'inspect', '--format', '{{json .NetworkSettings.Ports}}', ident]))
                assert bindings['443/tcp'] and all(int(p['HostPort']) > 1024 and p['HostIp'] == '127.0.0.1' for values in bindings.values() if values for p in values), bindings
                # Prove the Docker host mapping actually serves trusted HTTPS,
                # independently of the manager's internal network connection.
                ca = directory / 'fixture-root.crt'
                deployment.run(['docker', 'cp', ident + ':/data/caddy/pki/authorities/local/root.crt', str(ca)])
                context = ssl.create_default_context(cafile=str(ca))
                port = int(bindings['443/tcp'][0]['HostPort'])
                path = '/' + original['users']['owner']['token'] + '/clash'
                connection = http.client.HTTPSConnection('subscriptions.example.com', context=context, timeout=5)
                def connect():
                    connection.sock = context.wrap_socket(socket.create_connection(('127.0.0.1', port), timeout=5),
                                                          server_hostname='subscriptions.example.com')
                connection.connect = connect
                try:
                    connection.request('GET', path)
                    response = connection.getresponse()
                    expected = store.current() / ('sub/' + original['users']['owner']['token'] + '/clash.yaml')
                    assert response.status == 200 and response.read() == expected.read_bytes()
                finally:
                    connection.close()
                deployment.compose('restart', 'caddy', timeout=60)
                deployment.confirm('https://subscriptions.example.com')
                # Regeneration/refresh retains both the preferred domain and
                # independently mounted alias import through subsequent restart.
                store.init(settings)
                deployment.compose('restart', 'caddy', timeout=60)
                deployment.confirm('https://subscriptions.example.com')
                logs = deployment.compose('logs', 'caddy')
                assert all(user['token'] not in logs for user in original['users'].values())
                deployment.operate('remove')
                assert store.state() == original
                assert not (directory / domains.OVERLAY).exists()
                assert not (store.root / domains.ALIAS).exists()
                deployment.inject_failure = True
                try:
                    deployment.operate('add', 'subscriptions.example.com')
                except ValueError:
                    pass
                else:
                    raise AssertionError('failure injection did not roll back')
                deployment.confirm(settings.origin)
                assert store.state() == original
                assert not (directory / domains.OVERLAY).exists()
                assert not deployment.stage.exists()
                deployment.inject_failure = 'stop'
                with patch.object(deployment, 'recover', side_effect=RuntimeError('simulated process death')):
                    try:
                        deployment.operate('add', 'subscriptions.example.com')
                    except ValueError:
                        pass
                    else:
                        raise AssertionError('interrupted recreation was not injected')
                assert not deployment.compose('ps', '--status', 'running', '-q', 'caddy').strip()
                deployment.operate('recover')
                deployment.confirm(settings.origin)
                assert store.state() == original
                restored = json.loads(subprocess.check_output(['docker', 'compose', 'config', '--format', 'json'], cwd=directory))
                assert {p['target'] for p in restored['services']['caddy']['ports']} == {80, 2096}
            print('PASS: real Docker Compose add/recreate, target443 on random loopback, trusted TLS verify, restart/init persistence, remove, injected failure rollback; original origin and identities preserved')
        except BaseException:
            print(re.sub(r'\b[0-9a-f]{48}\b', '[redacted]', deployment.compose('logs', 'caddy')), file=sys.stderr)
            raise
        finally:
            deployment.compose('down', '-v', '--remove-orphans', timeout=60)
            # rootful Linux Docker may have created root-owned generated files.
            subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python',
                            '-v', str(directory) + ':/cleanup', TOOL, '-c',
                            "import shutil; shutil.rmtree('/cleanup/data', ignore_errors=True)"], check=True, timeout=30)


if __name__ == '__main__':
    main()
