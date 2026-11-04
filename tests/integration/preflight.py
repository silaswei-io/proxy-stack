#!/usr/bin/env python3
"""Native Caddy/Compose acceptance with disposable Docker port bindings."""
import importlib.util
import json
import os
import subprocess
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CADDY = 'caddy:2.11.4@sha256:0c994536bddb66445885237f1a5dcc1916bccea922661c76b4e9fc24061f9b52'
TOOL = 'proxy-stack-tool:v1.0.0'
spec = importlib.util.spec_from_file_location('host_setup', ROOT / 'manager/src/proxy_stack/host_setup.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def run(*args, env=None, timeout=30):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                            env=env, cwd=ROOT)
    if result.returncode:
        raise RuntimeError('native preflight validation failed: ' + args[0] + '\n' + result.stderr.decode()[-2000:])
    return result.stdout.decode()


with tempfile.TemporaryDirectory(prefix='proxy-stack-native-plan-') as temporary:
    root = Path(temporary).resolve()
    original = ('# original bytes and braces in comments { }\n{\n email admin@example.com\n}\n'
                'import sites/*.caddy\n'
                'http://legacy.example.com {\n'
                ' handle /.well-known/acme-challenge/* {\n reverse_proxy 127.0.0.1:9000\n }\n'
                ' handle {\n respond "original } # response"\n }\n}\n')
    (root / 'sites').mkdir()
    (root / 'sites/domain.caddy').write_text('one.example.com {\n reverse_proxy 127.0.0.1:3000\n}\n'
                                          'two.example.com {\n reverse_proxy 127.0.0.1:4000\n}\n')
    (root / 'original').write_text(original)
    candidate = helper.merge(original, helper.challenge_site('8.8.8.8', 40881, 'proxy-stack-native'), 'proxy-stack-native')
    assert candidate.startswith(original)
    (root / 'candidate').write_text(candidate)
    def caddy(filename, verb='adapt'):
        return run('docker', 'run', '--rm', '--network', 'none', '--read-only',
                   '--cap-add=NET_BIND_SERVICE', '--cap-drop=ALL', '--tmpfs', '/data', '--tmpfs', '/config', '--tmpfs', '/tmp',
                   '-v', str(root) + ':/work:ro', '--workdir', '/work', CADDY,
                   'caddy', verb, '--config', '/work/' + filename, '--adapter', 'caddyfile')
    before = json.loads(caddy('original'))
    after = json.loads(caddy('candidate'))
    caddy('candidate', 'validate')
    # Preserve every original native route; only the new IP/80 route is added.
    def routes(value):
        return [route for server in value['apps']['http']['servers'].values() for route in server['routes']]
    helper.preserve_routes(before, after)
    own = [r for r in routes(after) if r.get('match') == [{'host': ['8.8.8.8']}]]
    assert len(own) == 1
    private_route = json.dumps(own)
    assert '/.well-known/acme-challenge/*' in private_route
    assert '127.0.0.1:40881' in private_route
    assert '/sub/' not in private_route and '/ws/' not in private_route
    for domain in ('8.8.8.8', 'proxy.example.com', '2606:4700:4700::1111'):
        text = run('docker', 'run', '--rm', '--network', 'none', '-e', 'DOMAIN=' + domain,
                   '-e', 'NODE_NAME=Native', '-e', 'PUBLIC_PORT=34567', '-e', 'HTTP01_ONLY=true',
                   TOOL, 'preview-caddy')
        (root / 'stack').write_text(text)
        adapted = json.loads(caddy('stack'))
        caddy('stack', 'validate')
        policy = adapted['apps']['tls']['automation']['policies'][0]
        assert policy['subjects'] == [domain]
        issuer = policy['issuers'][0]
        assert issuer['ca'] == 'https://acme-v02.api.letsencrypt.org/directory'
        assert issuer['challenges']['tls-alpn']['disabled'] is True
        assert (issuer.get('profile') == 'shortlived') == (':' in domain or domain == '8.8.8.8')
        assert any(':34567' in s['listen'] for s in adapted['apps']['http']['servers'].values())
        assert set(adapted['logging']['logs']['default']['exclude']) >= {'http.log.access', 'http.log.error'}
    # Every generated role/layout must pass the actual pinned Caddy parser.
    for role, local_node in (('hub', 'true'), ('hub', 'false'), ('node', 'false')):
        for mode in ('standalone', 'existing-caddy'):
            text = run('docker', 'run', '--rm', '--network', 'none', '-e', 'DOMAIN=proxy.example.com',
                       '-e', 'ROLE=' + role, '-e', 'LOCAL_NODE=' + local_node, '-e', 'MODE=' + mode,
                       TOOL, 'preview-caddy')
            (root / 'layout').write_text(text)
            caddy('layout', 'validate')
    env = dict(os.environ, DOMAIN='8.8.8.8', PUBLIC_PORT='34567', HTTP_BIND='127.0.0.1', HTTP_PORT='40881', HTTP01_ONLY='true')
    for key in ('COMPOSE_FILE', 'COMPOSE_PROJECT_NAME', 'COMPOSE_PROFILES', 'COMPOSE_ENV_FILES'):
        env.pop(key, None)
    compose = json.loads(run('docker', 'compose', 'config', '--format', 'json', env=env))
    ports = compose['services']['caddy']['ports']
    assert any(p['target'] == 80 and p['published'] == '40881' and p['host_ip'] == '127.0.0.1' for p in ports)
    assert any(p['target'] == 34567 and p['published'] == '34567' for p in ports)
    assert not any(p['target'] in (8090, 2019, 8080) for p in ports)
    # Exercise the *exported*, unmodified Bash payload with real Docker and
    # native pinned Caddy. The adapter substitutes only the locally built image,
    # cached pulls, and temporary probe bindings (ephemeral Docker host ports).
    # It does not fake Compose, validation, model code or command results.
    isolated = root / 'exported-check'
    isolated.mkdir()
    binary_dir = isolated / 'bin'
    binary_dir.mkdir()
    # Host-shell boundary only: this acceptance tests Linux installer code with
    # real Docker/Caddy, not Desktop's unreliable collision detection.
    if sys.platform != 'linux':
        uname = binary_dir / 'uname'
        uname.write_text('#!/bin/sh\necho Linux\n')
        uname.chmod(0o700)
    real_docker = shutil.which('docker')
    adapter = binary_dir / 'docker'
    boundary = isolated / 'roundtrip-boundary.json'
    gateway = ('host.docker.internal' if sys.platform != 'linux' else
               run('docker', 'network', 'inspect', 'bridge', '--format', '{{(index .IPAM.Config 0).Gateway}}').strip())
    adapter.write_text('#!' + sys.executable + '\n' +
        'import json,os,subprocess,sys\nfrom pathlib import Path\nargs=sys.argv[1:]\n' +
        'args=[' + repr(TOOL) + ' if a=="ghcr.io/silaswei-io/proxy-stack:v1.0.0" else a for a in args]\n' +
        'if args[:1]==["pull"]: args=["image","inspect",args[1]]\n' +
        'boundary=Path(' + repr(str(boundary)) + ')\n' +
        'docker=' + repr(real_docker) + '\n' +
        'if args[:1]==["run"] and any(a.startswith("ROUNDTRIP_NONCE=") for a in args):\n' +
        '    if "-p" in args:\n' +
        '        args[args.index("-p")+1]="0.0.0.0::8080"\n' +
        '        subprocess.run([docker]+args,check=True,stdout=subprocess.DEVNULL)\n' +
        '        name=args[args.index("--name")+1]\n' +
        '        ports=json.loads(subprocess.check_output([docker,"inspect","--format","{{json .NetworkSettings.Ports}}",name]))\n' +
        '        boundary.write_text(json.dumps({"port":ports["8080/tcp"][0]["HostPort"],"listener":name}))\n' +
        '        sys.exit(0)\n' +
        '    # Explicit nonce fixture boundary: never route to the synthetic 8.8.8.8.\n' +
        '    state=json.loads(boundary.read_text());state["probe"]=args[args.index("--name")+1];boundary.write_text(json.dumps(state))\n' +
        '    args=["ROUNDTRIP_HOST="+' + repr(gateway) + ' if a.startswith("ROUNDTRIP_HOST=") else "ROUNDTRIP_PORT="+state["port"] if a.startswith("ROUNDTRIP_PORT=") else "ROUNDTRIP_NONCE="+"0"*64 if a.startswith("ROUNDTRIP_NONCE=") and os.environ.get("NATIVE_BAD_NONCE") else a for a in args]\n' +
        'elif args[:1]==["run"] and "-p" in args: args[args.index("-p")+1]="127.0.0.1::8080"\n' +
        'os.execv(' + repr(real_docker) + ', ["docker"]+args)\n')
    adapter.chmod(0o700)
    if not shutil.which('timeout'):
        deadline = binary_dir / 'timeout'
        deadline.write_text('#!' + sys.executable + '\nimport subprocess,sys\n' +
            'try: sys.exit(subprocess.run(sys.argv[2:],timeout=float(sys.argv[1])).returncode)\n' +
            'except subprocess.TimeoutExpired: sys.exit(124)\n')
        deadline.chmod(0o700)
    payload = isolated / 'installer.sh'
    payload.write_text(run('docker', 'run', '--rm', '--network', 'none', TOOL, 'installer'))
    check_env = dict(os.environ, PATH=str(binary_dir) + os.pathsep + os.environ['PATH'], TMPDIR=str(isolated))
    output = run('bash', str(payload), '--check', '--non-interactive', '--domain', '8.8.8.8',
                 '--name', 'Native Check', '--port', '34567', '--directory', str(isolated / 'installation'),
                 env=check_env, timeout=180)
    assert '预检通过' in output and 'HTTPS 8.8.8.8:34567' in output
    assert not (isolated / 'installation').exists()
    assert not (isolated / 'installation.install-lock').exists()
    def roundtrip_clean():
        state = json.loads(boundary.read_text())
        for name in (state['listener'], state['probe']):
            assert not run('docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$').strip()
    roundtrip_clean()
    failed = subprocess.run(['bash', str(payload), '--check', '--non-interactive', '--domain', '8.8.8.8',
                             '--name', 'Native Check', '--port', '34567', '--directory', str(isolated / 'installation')],
                            env=dict(check_env, NATIVE_BAD_NONCE='1'), text=True, capture_output=True, timeout=60)
    assert failed.returncode and '公共地址回环失败' in failed.stderr
    assert not (isolated / 'installation').exists()
    assert not (isolated / 'installation.install-lock').exists()
    roundtrip_clean()
    print('PASS: actual Docker published-port nonce roundtrip from separate bridge container; wrong nonce fails early; both containers removed')

    # Debian's actual 2.6.2 binary exercises no-v version, bare-module report,
    # file adaptation, relative imports, old native syntax and temporary cleanup.
    run('docker', 'build', '-t', 'proxy-stack-native-caddy26:v1.0.0', '-f',
        'tests/integration/stock-caddy.Dockerfile', '.', timeout=240)
    code = r'''import json
from pathlib import Path
from proxy_stack.host_setup import Host, stock_modules, challenge_site, merge, preserve_routes
host=Host()
binary='/usr/bin/caddy'
version=host.run([binary,'version']).strip()
assert version.lstrip('v').startswith('2.6.2'), version
assert stock_modules(host.run([binary,'list-modules','--packages','--versions']))
config=Path('/work/original')
original=config.read_text()
before=host.adapt(binary,config,original)
candidate=merge(original,challenge_site('8.8.8.8',40881,'proxy-stack-native'),'proxy-stack-native')
after=host.adapt(binary,config,candidate)
preserve_routes(before,after)
assert config.read_text()==original
assert not list(config.parent.glob('.proxy-stack-plan-*'))
Path('/work/native26.json').write_text(json.dumps(after))
host.validate(binary,'/work/native26.json')
try: host.adapt(binary,config,'this is { invalid')
except Exception: pass
else: raise AssertionError('bad candidate accepted')
assert not list(config.parent.glob('.proxy-stack-plan-*'))
print('PASS: actual Debian stock '+version+'; native relative imports, candidate validation and cleanup')
'''
    print(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop=ALL',
              '--cap-add=NET_BIND_SERVICE', '--tmpfs', '/data', '--tmpfs', '/config', '--tmpfs', '/tmp',
              '-v', str(root) + ':/work', 'proxy-stack-native-caddy26:v1.0.0', '-c', code).strip())
print('PASS: native routes/imports preserved; HTTP-01 -> internal :80; high-port HTTPS; TLS-ALPN disabled; IP shortlived/default_sni; private logs; Compose; exported installer --check with explicit isolated nonce boundary')
