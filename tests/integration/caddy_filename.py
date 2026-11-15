#!/usr/bin/env python3
"""Pinned native file_server/installer regression, isolated from all host services."""
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
CADDY = 'caddy:2.11.4@sha256:0c994536bddb66445885237f1a5dcc1916bccea922661c76b4e9fc24061f9b52'


def native(binary):
    sys.path.insert(0, str(ROOT / 'tests'))
    from test_host_setup import HostFixture
    from proxy_stack import host_setup as setup

    class NativeHost(HostFixture):
        # Only the systemd/process/ownership boundary is simulated. Adaptation,
        # validation, admin state, reload and file transactions use native Caddy.
        def run(self, argv, **kwargs):
            if argv[0] == str(self.binary):
                return setup.Host.run(self, argv, **kwargs)
            return super().run(argv, **kwargs)

        active = setup.Host.active

        def reload(self):
            self.run([str(self.binary), 'reload', '--config', str(self.config)])

    def handlers(value):
        if isinstance(value, dict):
            if value.get('handler') == 'file_server':
                yield value
            for child in value.values():
                yield from handlers(child)
        elif isinstance(value, list):
            for child in value:
                yield from handlers(child)

    def refused(operation):
        try:
            operation()
        except setup.Refused:
            return
        raise AssertionError('real file_server hide drift accepted')

    with tempfile.TemporaryDirectory(prefix='native-caddy-filename-') as temporary:
        root = Path(temporary)
        host = NativeHost(root / 'host')
        shutil.copyfile(binary, host.binary)
        host.binary.chmod(0o700)
        (host.root / 'sites').mkdir()
        (host.root / 'sites/files.caddy').write_text(
            'http://files.example.com {\n file_server {\n hide /private/imported *.key\n }\n}\n')
        original = ('{\n admin 127.0.0.1:2019\n auto_https off\n}\n'
                    'import sites/*.caddy\nhttp://legacy.example.com {\n'
                    ' handle /first/* {\n file_server {\n hide /private *.secret .proxy-stack-plan-user.caddy\n }\n }\n'
                    ' handle {\n file_server {\n hide ' + str(host.config) + ' /kept\n }\n }\n}\n')
        host.config.write_text(original)
        def raw_adapt():
            return json.loads(host.run([str(host.binary), 'adapt', '--adapter', 'caddyfile',
                                        '--config', str(host.config)], cwd=str(host.root)))
        with (root / 'caddy.log').open('wb') as log:
            process = subprocess.Popen([str(host.binary), 'run', '--config', str(host.config)],
                                       cwd=host.root, stdout=log, stderr=log)
            try:
                until = time.monotonic() + 5
                while True:
                    try:
                        active = host.active()
                        break
                    except setup.Refused:
                        if process.poll() is not None or time.monotonic() >= until:
                            raise AssertionError('disposable native Caddy did not start') from None
                        time.sleep(.05)
                baseline = raw_adapt()
                assert active == baseline
                assert len(list(handlers(baseline))) == 3
                assert host.adapt(str(host.binary), host.config, original) == baseline
                assert host.config.read_text() == original
                stage = root / 'transaction'
                with patch.object(setup.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=999)):
                    value = setup.plan(host, stage, '8.8.8.8', 40881, 'proxy-stack-native-files')
                    candidate = json.loads((stage / 'candidate.json').read_text())
                    setup.preserve_routes(baseline, candidate)
                    assert [h['hide'] for h in handlers(candidate)] == [h['hide'] for h in handlers(baseline)]
                    setup.recheck(host, value)
                    # Real admin changes to a user's hide list remain drift at
                    # plan time, apply recheck, route preservation and rollback.
                    drift = copy.deepcopy(baseline)
                    next(handlers(drift))['hide'].append('/real-user-change')
                    drift_path = root / 'drift.json'
                    drift_path.write_text(json.dumps(drift))
                    host.run([str(host.binary), 'reload', '--config', str(drift_path)])
                    refused(lambda: setup.plan(host, root / 'drift-plan', '8.8.8.8', 40881, 'proxy-stack-native-files'))
                    refused(lambda: setup.apply(host, stage))
                    refused(lambda: setup.preserve_routes(baseline, drift))
                    assert host.config.read_text() == original
                    host.reload()
                    setup.apply(host, stage)
                    assert host.active() == candidate == raw_adapt()
                    assert host.adapt(str(host.binary), host.config, host.config.read_text()) == candidate
                    drift = copy.deepcopy(candidate)
                    next(handlers(drift))['hide'].append('/real-user-change')
                    drift_path.write_text(json.dumps(drift))
                    host.run([str(host.binary), 'reload', '--config', str(drift_path)])
                    refused(lambda: setup.rollback(host, stage))
                    host.reload()
                    setup.rollback(host, stage)
                    assert host.config.read_text() == original
                    assert host.active() == baseline == raw_adapt()
                assert not list(host.root.glob('.proxy-stack-plan-*'))
            finally:
                process.terminate()
                process.wait(timeout=10)
    print('PASS: pinned Caddy 2.11.4 file_server; exact filename canonicalization, user hides/imports, native validation/apply/recheck/rollback, real drift refusal')


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--native':
        native(sys.argv[2])
        return
    # Extract the binary from the pinned image into a disposable fixture. Run
    # it with Python in a network-isolated container; publish no host ports.
    with tempfile.TemporaryDirectory(prefix='proxy-stack-caddy-filename-') as temporary:
        binary = Path(temporary) / 'caddy'
        with binary.open('wb') as out:
            subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'cat',
                            CADDY, '/usr/bin/caddy'], stdout=out, check=True, timeout=60)
        binary.chmod(0o700)
        subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop=ALL',
                        '--cap-add=NET_BIND_SERVICE', '--tmpfs', '/tmp:exec', '--tmpfs', '/data', '--tmpfs', '/config',
                        '-v', str(binary) + ':/fixture/caddy:ro',
                        '-v', str(ROOT / 'manager/src/proxy_stack') + ':/app/proxy_stack:ro',
                        '-v', str(ROOT / 'tests') + ':/tests:ro', '--entrypoint', 'python',
                        'proxy-stack-tool:v1.0.0', '/tests/integration/caddy_filename.py',
                        '--native', '/fixture/caddy'], check=True, timeout=60)


if __name__ == '__main__':
    main()
