#!/usr/bin/env python3
"""Native alias HTTPS, upstream TLS, host transaction and container renewal state.

Only systemd/process identity, DNS ownership and Docker orchestration boundaries
are substituted inside a disposable container. Caddy parsing, validation, TLS,
file serving, reload, model publication and rollback execute real code.
"""
import hashlib
import http.client
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
CADDY = 'caddy:2.11.4@sha256:0c994536bddb66445885237f1a5dcc1916bccea922661c76b4e9fc24061f9b52'
TOOL = 'proxy-stack-tool:v1.0.0'


def native(binary):
    sys.path.insert(0, '/tests')
    from test_host_setup import HostFixture
    from proxy_stack import domains, host_setup, render
    from proxy_stack.model import Settings
    from proxy_stack.routes import asset_paths
    from proxy_stack.storage import Store

    class NativeHost(HostFixture):
        def run(self, argv, **kwargs):
            if argv[0] == str(self.binary):
                return host_setup.Host.run(self, argv, **kwargs)
            return super().run(argv, **kwargs)
        active = host_setup.Host.active
        def reload(self):
            self.run([str(self.binary), 'reload', '--config', str(self.config)])

    processes = []
    def launch(config, log, env=None):
        stream = log.open('ab')
        process = subprocess.Popen([binary, 'run', '--config', str(config), '--adapter', 'caddyfile'],
                                   stdout=stream, stderr=stream, env=env)
        stream.close()
        processes.append(process)
        return process

    def wait_file(path):
        until = time.monotonic() + 8
        while not path.exists():
            assert time.monotonic() < until, 'certificate timeout'
            time.sleep(.05)

    def request(host, port, path, context, method='GET'):
        connection = http.client.HTTPSConnection(host, port, context=context, timeout=3)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    with tempfile.TemporaryDirectory(prefix='native-domains-') as temporary:
        root = Path(temporary)
        directory = root / 'installation'
        directory.mkdir()
        (directory / '.env').write_text('DOMAIN=origin.example.com\nPUBLIC_PORT=8443\n')
        store = Store(directory / 'data')
        settings = Settings('origin.example.com', 'Hub', port=8443, local_node=False)
        store.init(settings)
        store.mutate_user('add', 'friend')
        original = store.state()
        # An internal issuer replaces public ACME only in this isolated fixture.
        upstream_data = root / 'upstream-data'
        config = render.caddy(settings, original).replace(
            '{\n    admin off', '{\n    local_certs\n    skip_install_trust\n    storage file_system {\n        root ' + str(upstream_data) + '\n    }\n    admin off', 1)
        config = config.replace('/srv/public/current', str(store.public / 'current'))
        upstream_config = root / 'upstream.Caddyfile'
        # Preload a test wildcard certificate on the host through an exact
        # existing site. This keeps real HTTPS while leaving the host's default
        # ACME policy/subject generation identical to a normal public setup.
        # No host issuer is replaced with a global local_certs catch-all.
        upstream_config.write_text(config + '\n*.example.com:9443 {\n respond "fixture certificate"\n}\n')
        upstream_log = root / 'upstream.log'
        host_log = root / 'host.log'
        alias_log = root / 'alias.log'
        try:
            launch(upstream_config, upstream_log)
            upstream_ca = upstream_data / 'pki/authorities/local/root.crt'
            wait_file(upstream_ca)
            upstream_context = ssl.create_default_context(cafile=str(upstream_ca))
            wildcard = upstream_data / 'certificates/local/wildcard_.example.com/wildcard_.example.com'
            wildcard_cert = str(wildcard) + '.crt'
            wildcard_key = str(wildcard) + '.key'
            wait_file(Path(wildcard_cert))
            wait_file(Path(wildcard_key))
            host = NativeHost(root / 'host')
            shutil.copyfile(binary, host.binary)
            host.binary.chmod(0o700)
            host_data = root / 'host-data'
            host.config.write_text('''{
 admin 127.0.0.1:2019
 auto_https disable_certs
 storage file_system {
 root ''' + str(host_data) + '''
 }
}
http://legacy.example.com:80 {
 file_server {
 hide /private *.secret
 }
}
special.example.com {
 tls ''' + wildcard_cert + ' ' + wildcard_key + ''' {
  issuer acme {
   disable_http_challenge
  }
 }
 respond "special"
}
one.example.com, files.example.com, www.example.com {
 respond "existing HTTPS"
}
''')
            host_original = host.config.read_bytes()
            host_process = launch(host.config, host_log, dict(os.environ, SSL_CERT_FILE=str(upstream_ca)))
            until = time.monotonic() + 8
            while True:
                try:
                    host.active()
                    break
                except host_setup.Refused:
                    assert host_process.poll() is None and time.monotonic() < until, 'host startup'
                    time.sleep(.05)
            host_baseline = host.active()
            original_policies = host_baseline['apps']['tls']['automation']['policies']
            assert len(original_policies) == 2, original_policies
            custom = next(p for p in original_policies if p['subjects'] == ['special.example.com'])
            assert custom['issuers'][0]['module'] == 'acme'
            assert custom['issuers'][0]['challenges']['http']['disabled'] is True
            assert any('one.example.com' in p.get('subjects', []) for p in original_policies)

            def check_policies(added=False):
                expected = json.loads(json.dumps(original_policies))
                if added:
                    default = next(p for p in expected if 'one.example.com' in p.get('subjects', []))
                    default['subjects'].append('subscriptions.example.com')
                actual = host.active()['apps']['tls']['automation']['policies']
                assert actual == expected, (actual, expected)

            class NativeDeployment(domains.Deployment):
                def preflight(self, recovering=False):
                    self.config = {'services': {'caddy': {'ports': []}}}
                    return self.store.state()
                def port_free(self, port):
                    return False
                def confirm(self, origin, wait=120):
                    # Exact same verifier + TLS defaults; bounded retries allow
                    # asynchronous internal issuance. Never disable TLS checks.
                    from proxy_stack.cli import verify
                    ca = upstream_ca
                    wait_file(ca)
                    with patch.dict(os.environ, {'SSL_CERT_FILE': str(ca)}):
                        deadline = time.monotonic() + 8
                        while True:
                            try:
                                verify(self.store, origin)
                                return
                            except Exception:
                                if time.monotonic() >= deadline:
                                    raise
                                time.sleep(.1)
            deployment = NativeDeployment(directory)
            deployment.host = host
            with patch.object(domains.discovery, 'check_address'), patch.object(host_setup.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=999)):
                original_reload = host.reload
                attempts = []
                def failed_reload():
                    attempts.append(True)
                    if len(attempts) == 1:
                        raise host_setup.Refused('injected native reload failure')
                    original_reload()
                with patch.object(host, 'reload', side_effect=failed_reload):
                    try:
                        deployment.operate('add', 'subscriptions.example.com')
                    except host_setup.Refused:
                        pass
                    else:
                        raise AssertionError('host reload failure was not rolled back')
                assert host.config.read_bytes() == host_original
                assert store.state() == original
                check_policies()
                deployment.operate('add', 'subscriptions.example.com')
                check_policies(added=True)
                assert store.state()['nodes'] == original['nodes']
                assert store.state()['users'] == original['users']
                context = ssl.create_default_context(cafile=str(upstream_ca))
                def check_original_https():
                    for name in ('one', 'files', 'www', 'special'):
                        expected = b'special' if name == 'special' else b'existing HTTPS'
                        assert request(name + '.example.com', 443, '/', context)[2] == expected
                check_original_https()
                # Every friend/owner canonical and legacy URL, GET + HEAD,
                # including complete QX resources, must be equal on IP/origin.
                for user in original['users'].values():
                    for clean, legacy, mime in asset_paths(user['token']):
                        expected = (store.current() / legacy).read_bytes()
                        for path in (clean, '/' + legacy):
                            status, headers, body = request('subscriptions.example.com', 443, path, context)
                            assert status == 200 and body == expected
                            assert headers['Cache-Control'] == 'no-store'
                            assert headers['Content-Type'].startswith(mime)
                            assert request('subscriptions.example.com', 443, path, context, 'HEAD')[2] == b''
                            assert request('origin.example.com', 8443, path, upstream_context)[2] == expected
                for path in ('/', '/manifest.json', original['ws_path'], '/control/v1/enroll', '/sub/' + '0'*48 + '/clash.yaml', '/a%2fb', '//x', '/./x', '/../x', '/?x=1'):
                    assert request('subscriptions.example.com', 443, path, context)[0] == 404
                    assert request('subscriptions.example.com', 443, path, context, 'POST')[0] == 404
                # Server restart keeps TLS certificates and selected origin.
                host_process.terminate()
                host_process.wait(timeout=5)
                host_process = launch(host.config, host_log, dict(os.environ, SSL_CERT_FILE=str(upstream_ca)))
                time.sleep(.3)
                deployment.confirm('https://subscriptions.example.com')
                check_policies(added=True)
                # With the upstream CA absent, host Caddy must reject upstream
                # TLS. Its handled error must not log any private request path.
                host_process.terminate()
                host_process.wait(timeout=5)
                host_process = launch(host.config, host_log)
                time.sleep(.3)
                assert request('subscriptions.example.com', 443, asset_paths(original['users']['owner']['token'])[0][0], context)[0] == 404
                host_process.terminate()
                host_process.wait(timeout=5)
                host_process = launch(host.config, host_log, dict(os.environ, SSL_CERT_FILE=str(upstream_ca)))
                time.sleep(.3)
                deployment.confirm('https://subscriptions.example.com')
                deployment.operate('remove')
                assert store.state() == original
                assert host.config.read_bytes() == host_original
                check_policies()
                check_original_https()
                print('PASS: native existing HTTPS hosts/custom TLS issuer policy; only owned alias subject added/removed, policies survive reload rollback and restart')
                # Unknown pre-existing aliases are never silently adopted.
                unknown = host.config.read_text() + '\nsubscriptions.example.com {\n respond "custom"\n}\n'
                host.config.write_text(unknown)
                host.reload()
                try:
                    deployment.operate('add', 'subscriptions.example.com')
                except ValueError:
                    pass
                else:
                    raise AssertionError('unknown alias adopted')
                assert host.config.read_text() == unknown
                host.config.write_bytes(host_original)
                host.reload()
            host_process.terminate()
            host_process.wait(timeout=5)
            # Standalone container site serves assets directly on its :443.
            endpoint = Settings('subscriptions.example.com', 'Alias')
            alias_data = root / 'alias-data'
            alias_config = root / 'alias.Caddyfile'
            alias_config.write_text('''{
 admin off
 local_certs
 skip_install_trust
 storage file_system {
 root ''' + str(alias_data) + '''
 }
 log default {
 exclude http.log.access http.log.error
 }
}
''' + render.subscription_site(endpoint).replace('/srv/public/current', str(store.public / 'current')))
            alias_process = launch(alias_config, alias_log)
            alias_ca = alias_data / 'pki/authorities/local/root.crt'
            wait_file(alias_ca)
            context = ssl.create_default_context(cafile=str(alias_ca))
            path = asset_paths(original['users']['owner']['token'])[0][0]
            until = time.monotonic() + 8
            while True:
                try:
                    assert request(endpoint.domain, 443, path, context)[0] == 200
                    break
                except (OSError, AssertionError):
                    assert time.monotonic() < until
                    time.sleep(.1)
            ca_bytes = alias_ca.read_bytes()
            alias_process.terminate()
            alias_process.wait(timeout=5)
            launch(alias_config, alias_log)
            time.sleep(.3)
            assert request(endpoint.domain, 443, path, context)[0] == 200
            assert alias_ca.read_bytes() == ca_bytes
            # TLS rejection is real: the unrelated system trust must reject it.
            try:
                request(endpoint.domain, 443, path, ssl.create_default_context())
            except ssl.SSLCertVerificationError:
                pass
            else:
                raise AssertionError('untrusted certificate accepted')
            all_logs = host_log.read_text() + upstream_log.read_text() + alias_log.read_text()
            assert all(user['token'] not in all_logs for user in original['users'].values())
            print('PASS: native domain host add/remove/restart; original+friend assets, QX, verified upstream TLS, strict 404, custom alias refusal; container :443 TLS + restart certificate persistence; no private request paths in logs')
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--native':
        native(sys.argv[2])
        return
    subprocess.run([sys.executable, str(ROOT / 'tests/integration/domain_compose.py')], check=True)
    with tempfile.TemporaryDirectory(prefix='domain-native-') as temporary:
        binary = Path(temporary) / 'caddy'
        with binary.open('wb') as stream:
            subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'cat', CADDY, '/usr/bin/caddy'], stdout=stream, check=True, timeout=30)
        binary.chmod(0o700)
        subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop=ALL',
                        '--cap-add=NET_BIND_SERVICE', '--tmpfs', '/tmp:exec', '--tmpfs', '/data', '--tmpfs', '/config',
                        '--add-host', 'origin.example.com:127.0.0.1', '--add-host', 'subscriptions.example.com:127.0.0.1',
                        '--add-host', 'one.example.com:127.0.0.1', '--add-host', 'files.example.com:127.0.0.1',
                        '--add-host', 'www.example.com:127.0.0.1', '--add-host', 'special.example.com:127.0.0.1',
                        '-p', '127.0.0.1::443', '-v', str(binary) + ':/fixture/caddy:ro',
                        '-v', str(ROOT / 'manager/src/proxy_stack') + ':/app/proxy_stack:ro',
                        '-v', str(ROOT / 'tests') + ':/tests:ro', '--entrypoint', 'python', TOOL,
                        '/tests/integration/domains.py', '--native', '/fixture/caddy'], check=True, timeout=100)


if __name__ == '__main__':
    main()
