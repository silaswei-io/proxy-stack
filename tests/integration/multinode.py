"""Real V1 control plane, supervised nodes, trusted HTTPS and Mihomo egress."""
import http.client
import json
import shutil
import socket
import ssl
import struct
import time
from pathlib import Path

from run import TOOL, CADDY, MIHOMO, docker, tunnel, recv_exact


def until(predicate, message, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(message)


def multinode_integration(root, network, containers):
    directory = root / 'multinode'
    directory.mkdir()
    domains = ['hub.example.com', 'node1.example.com', 'node2.example.com']
    names = ['Subscription Hub', 'Node One', 'Node Two']
    data = [directory / ('data' + str(i)) for i in range(3)]
    for path in data:
        path.mkdir()
    prefix = network + '-mn-'
    api, gateways, runtimes = prefix + 'api', [], []

    def manager(index, *args, input_text=None, code=None):
        command = ['run', '--rm', '--read-only', '--cap-drop=ALL', '--network', network,
                   '-v', f'{data[index]}:/data', '-e', 'DOMAIN=' + domains[index], '-e', 'NODE_NAME=' + names[index],
                   '-e', 'PUBLIC_PORT=8443', '-e', 'ROLE=' + ('hub' if index == 0 else 'node'),
                   '-e', 'LOCAL_NODE=false' if index == 0 else 'LOCAL_NODE=true', '-e', 'LEASE_SECONDS=10']
        if index:
            command += ['-v', f'{directory / "cert0"}:/trust:ro', '-e', 'SSL_CERT_FILE=/trust/caddy/pki/authorities/local/root.crt']
        if code is not None:
            return docker(*command, '--entrypoint', 'python', TOOL, '-c', code)
        if input_text is not None:
            # Enrollment secrets travel on stdin. No raw secret in command argv.
            import subprocess
            result = subprocess.run(['docker', *command, '-i', TOOL, *args], input=input_text, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
            if result.returncode:
                raise AssertionError('test enrollment failed (' + str(result.returncode) + ')')
            return result.stdout
        return docker(*command, TOOL, *args)

    for i in range(3):
        manager(i, 'init')
    original = json.loads((data[0] / 'model/state.json').read_text())
    owner = original['users']['owner']
    token = owner['token']
    central_link = 'https://hub.example.com:8443/sub/' + token + '/clash.yaml'
    assert json.loads((data[0] / 'public/current/sub' / token / 'clash.yaml').read_text())['proxies'] == []
    assert 'REJECT' in (data[0] / 'public/current/sub' / token / 'clash.yaml').read_text()

    containers.append(api)
    docker('run', '-d', '--name', api, '--network', network, '--network-alias', 'hub', '--read-only', '--cap-drop=ALL',
           '-v', f'{data[0]}:/data', TOOL, 'hub-api')
    for i in range(3):
        certs = directory / ('cert' + str(i))
        certs.mkdir()
        if i:
            shutil.copytree(directory / 'cert0/caddy/pki', certs / 'caddy/pki')
        production = (data[i] / 'runtime/caddy/Caddyfile').read_text()
        # Keep exact production routes; replace only ACME with a disposable CA,
        # and the internal runtime upstream with this node's test container name.
        text = production.replace('{\n', '{\n    skip_install_trust\n', 1)
        text = text.replace(domains[i] + ':8443 {', domains[i] + ':8443 {\n    tls internal')
        text = text.replace('hub:8090', api + ':8090')
        text = text.replace('xray:10000', prefix + 'runtime' + str(i) + ':10000')
        caddyfile = directory / ('Caddyfile' + str(i))
        caddyfile.write_text(text)
        docker('run', '--rm', '--network', 'none', '--read-only', '--cap-drop=ALL',
               '--cap-add=NET_BIND_SERVICE', '-v', f'{caddyfile}:/etc/caddy/Caddyfile:ro', '--tmpfs', '/data', '--tmpfs', '/config',
               CADDY, 'caddy', 'validate', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile')
        gateway = prefix + 'gateway' + str(i)
        gateways.append(gateway)
        containers.append(gateway)
        docker('run', '-d', '--name', gateway, '--network', network, '--network-alias', domains[i],
               '--read-only', '--cap-drop=ALL', '--cap-add=NET_BIND_SERVICE', '-p', '127.0.0.1::8443', '-v', f'{caddyfile}:/etc/caddy/Caddyfile:ro',
               '-v', f'{data[i] / "public"}:/srv/public:ro', '-v', f'{certs}:/data', '--tmpfs', '/config', CADDY)
        until(lambda: (certs / 'caddy/pki/authorities/local/root.crt').exists(), 'test CA not generated')
    ca = directory / 'cert0/caddy/pki/authorities/local/root.crt'
    context = ssl.create_default_context(cafile=str(ca))
    ports = [int(json.loads(docker('inspect', name))[0]['NetworkSettings']['Ports']['8443/tcp'][0]['HostPort']) for name in gateways]
    assert all(p > 1024 for p in ports) and len(set(ports)) == 3

    def https(index, path, method='GET', body=None, headers=None):
        host = domains[index]
        with socket.create_connection(('127.0.0.1', ports[index]), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=host) as stream:
                encoded = (json.dumps(body).encode() if body is not None else b'')
                extra = ''.join(k + ': ' + v + '\r\n' for k, v in (headers or {}).items())
                stream.sendall((f'{method} {path} HTTP/1.1\r\nHost: {host}:8443\r\nConnection: close\r\n' + extra +
                                f'Content-Type: application/json\r\nContent-Length: {len(encoded)}\r\n\r\n').encode() + encoded)
                response = http.client.HTTPResponse(stream)
                response.begin()
                return response.status, response.read()

    until(lambda: https(0, '/sub/' + token + '/clash.yaml')[0] == 200, 'hub HTTPS not ready')
    assert https(0, '/control/v1/poll', 'POST', {'generation': 0})[0] == 403
    assert https(0, '/control/v1/ack', 'POST', {'generation': 1})[0] == 403
    assert https(0, original['ws_path'])[0] == 404  # subscription-only hub has no tunnel
    assert https(1, '/control/v1/poll', 'POST', {'generation': 0})[0] == 404
    for i in (1, 2):
        code = manager(0, 'node', 'code').strip()
        manager(i, 'node', 'join', input_text=code + '\n')
        # A scoped peer file contains no subscription bearer or full hub state.
        peer = json.loads((data[i] / 'model/peer.json').read_text())
        assert set(peer) == {'id', 'token', 'origin'}
        assert token not in json.dumps(peer)
        # Prove replay is rejected over actual verified TLS.
        import base64
        invitation = json.loads(base64.urlsafe_b64decode(code + '=' * (-len(code) % 4)))
        node = json.loads((data[i] / 'model/state.json').read_text())
        assert https(0, '/control/v1/enroll', 'POST', {'secret': invitation['secret'],
                     'settings': node['settings'], 'ws_path': node['ws_path'], 'peer': {k:peer[k] for k in ('id','token')}})[0] == 403

    origin_name = prefix + 'origin'
    containers.append(origin_name)
    origin_code = '''from http.server import BaseHTTPRequestHandler,HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.end_headers();self.wfile.write(('proxy-stack-egress-ok '+self.client_address[0]).encode())
    def log_message(self,*args): pass
HTTPServer(('0.0.0.0',8000),H).serve_forever()
'''
    docker('run', '-d', '--name', origin_name, '--network', network, '--read-only', '--cap-drop=ALL',
           '--entrypoint', 'python', TOOL, '-c', origin_code)
    origin_ip = json.loads(docker('inspect', origin_name))[0]['NetworkSettings']['Networks'][network]['IPAddress']
    # Only this test wrapper adds the one private origin to Xray's egress allow
    # list. It executes the pinned, bundled binary with otherwise production code.
    wrapper = directory / 'xray-test'
    wrapper.write_text('''#!/usr/local/bin/python
import json,sys,subprocess
from pathlib import Path
args=sys.argv[1:]
p=Path(args[args.index('-config')+1])
c=json.loads(p.read_text())
c['outbounds'][0]['settings']={'finalRules':[{'action':'allow','network':'tcp','port':'8000','ip':[''' + repr(origin_ip) + ''']}]}
p.write_text(json.dumps(c))
import os
os.execv('/usr/local/bin/xray',['xray']+args)
''')
    wrapper.chmod(0o755)
    def start_runtime(i, failing=False):
        name = prefix + 'runtime' + str(i)
        runtime_code = ("import signal,threading; from proxy_stack.storage import Store; from proxy_stack.runtime import Runtime,Supervisor; "
                        "s=Store('/data'); stop=threading.Event(); signal.signal(signal.SIGTERM,lambda *_:stop.set()); "
                        "Runtime(s,Supervisor(s.runtime/'xray'," + repr('/bin/false' if failing else '/test/xray-test') + ")).run(stop)")
        docker('run', '-d', '--name', name, '--network', network, '--read-only', '--cap-drop=ALL',
               '-v', f'{data[i]}:/data', '-v', f'{directory}:/test:ro',
               '-e', 'SSL_CERT_FILE=/test/cert0/caddy/pki/authorities/local/root.crt',
               '--entrypoint', 'python', TOOL, '-c', runtime_code)
        return name
    for i in (1, 2):
        runtime = prefix + 'runtime' + str(i)
        containers.append(runtime)
        runtimes.append(start_runtime(i, failing=i == 2))
    def statuses():
        return json.loads(manager(0, 'node', 'list'))
    until(lambda: any(n['status'] == 'synced' for n in statuses()), 'first supervised node not acknowledged')
    state = json.loads((data[0] / 'model/state.json').read_text())
    second_id = json.loads((data[2] / 'model/peer.json').read_text())['id']
    assert state['nodes'][second_id]['acked'] == 0 and not state['nodes'][second_id]['ready']
    config = json.loads(https(0, '/sub/' + token + '/clash.yaml')[1])
    assert [p['name'] for p in config['proxies']] == ['Node One']
    docker('rm', '-f', runtimes[1])
    start_runtime(2)
    until(lambda: all(n['status'] == 'synced' for n in statuses()), 'both supervised nodes not synchronized')
    config = json.loads(https(0, '/sub/' + token + '/clash.yaml')[1])
    assert [p['server'] for p in config['proxies']] == domains[1:]
    assert all(provider['url'].startswith(central_link.rsplit('/', 1)[0] + '/') for provider in config['rule-providers'].values())
    assert https(0, '/sub/' + token + '/qx/nodes.txt')[1].count(b'vless=') == 2
    assert b'static = VPS, Node One, Node Two' in https(0, '/sub/' + token + '/qx/full.conf')[1]
    for i in (1, 2):
        runtime_status = json.loads((data[i] / 'runtime/node-status.json').read_text())
        assert runtime_status['running'] and runtime_status['generation'] == 1
        docker('run', '--rm', '--read-only', '--cap-drop=ALL', '--network', 'none', '--entrypoint', '/usr/local/bin/xray',
               '-v', f'{data[i] / "runtime/xray"}:/config:ro', TOOL, 'run', '-test', '-config', '/config/active.json')
    print('PASS: HTTPS one-use enrollment, scoped credentials, failed-start withheld, 2 actual supervised Xray nodes acknowledged', flush=True)

    mihomo_dir = directory / 'mihomo'
    mihomo_dir.mkdir()
    owner_dir = data[0] / 'public/current/sub' / token
    shutil.copytree(owner_dir / 'rules', mihomo_dir / 'rules')
    # Test traffic is private origin traffic: explicitly send it through the
    # selected group. Production domestic/LAN rules remain checked separately.
    config.update({'mixed-port': 7890, 'allow-lan': True, 'external-controller': '0.0.0.0:9090',
                   'dns': {'enable': False}, 'rules': ['MATCH,VPS']})
    (mihomo_dir / 'config.yaml').write_text(json.dumps(config))
    client = prefix + 'mihomo'
    containers.append(client)
    docker('run', '-d', '--name', client, '--network', network, '--read-only', '--cap-drop=ALL',
           '-p', '127.0.0.1::7890', '-p', '127.0.0.1::9090', '-e', 'SSL_CERT_FILE=/trust/root.crt',
           '-v', f'{ca}:/trust/root.crt:ro', '-v', f'{mihomo_dir}:/root/.config/mihomo', MIHOMO,
           '-d', '/root/.config/mihomo', '-f', '/root/.config/mihomo/config.yaml')
    client_ports = json.loads(docker('inspect', client))[0]['NetworkSettings']['Ports']
    socks_port = int(client_ports['7890/tcp'][0]['HostPort'])
    control_port = int(client_ports['9090/tcp'][0]['HostPort'])
    def select(name):
        conn = http.client.HTTPConnection('127.0.0.1', control_port, timeout=5)
        try:
            conn.request('PUT', '/proxies/VPS', json.dumps({'name': name}), {'Content-Type': 'application/json'})
            result = conn.getresponse()
            result.read()
            return result.status == 204
        except OSError:
            return False
        finally:
            conn.close()
    def mihomo_egress(i):
        until(lambda: select(names[i]), 'Mihomo controller did not select node')
        with socket.create_connection(('127.0.0.1', socks_port), timeout=10) as stream:
            stream.sendall(b'\x05\x01\x00')
            assert recv_exact(stream, 2) == b'\x05\x00'
            stream.sendall(b'\x05\x01\x00\x01' + socket.inet_aton(origin_ip) + struct.pack('!H', 8000))
            reply = recv_exact(stream, 4)
            assert reply[:2] == b'\x05\x00', reply
            assert reply[3] == 1, reply
            recv_exact(stream, 6)
            stream.sendall(b'GET / HTTP/1.1\r\nHost: origin\r\nConnection: close\r\n\r\n')
            response = b''
            while True:
                chunk = stream.recv(4096)
                if not chunk:
                    break
                response += chunk
        runtime_ip = json.loads(docker('inspect', runtimes[i - 1]))[0]['NetworkSettings']['Networks'][network]['IPAddress']
        assert ('proxy-stack-egress-ok ' + runtime_ip).encode() in response, response
    for i in (1, 2):
        mihomo_egress(i)
    print('PASS: actual Mihomo -> verified TLS/WSS/VLESS -> each node -> origin, source IP proves node egress', flush=True)

    manager(0, 'user', 'add', 'alice')
    manager(0, 'user', 'add', 'bob')
    until(lambda: all(n['status'] == 'synced' and n['acknowledged'] == 3 for n in statuses()), 'friend credentials not applied on each node')
    state = json.loads((data[0] / 'model/state.json').read_text())
    alice, bob = state['users']['alice'], state['users']['bob']
    for i in (1, 2):
        node = json.loads((data[i] / 'model/state.json').read_text())
        for user in (owner, alice, bob):
            tunnel(ports[i], context, node, user, origin_ip, server_hostname=domains[i])
    bob_links = manager(0, 'links', '--user', 'bob')
    docker('stop', api)
    manager(0, 'user', 'revoke', 'alice')
    assert https(0, '/sub/' + alice['token'] + '/clash.yaml')[0] == 404
    assert any(n['status'] == 'pending' for n in statuses())
    until(lambda: all(n['status'] == 'offline' for n in statuses()), 'offline state not reported', timeout=20)
    until(lambda: all(not json.loads((data[i] / 'runtime/node-status.json').read_text())['running'] for i in (1, 2)),
          'offline leases did not stop actual Xray children', timeout=15)
    # The watchdog must also work while a poll blocks. Simulate an unresponsive
    # HTTPS hub by pausing its API, rather than replacing protocol behavior.
    docker('start', api)
    until(lambda: all(n['status'] == 'synced' and n['acknowledged'] == 4 for n in statuses()), 'nodes did not recover desired revocation')
    for i in (1, 2):
        node = json.loads((data[i] / 'model/state.json').read_text())
        for user in (owner, bob):
            tunnel(ports[i], context, node, user, origin_ip, server_hostname=domains[i])
        try:
            tunnel(ports[i], context, node, alice, origin_ip, server_hostname=domains[i])
        except (EOFError, ConnectionResetError, ssl.SSLError):
            pass
        else:
            raise AssertionError('revoked friend still accepted by node')
        mihomo_egress(i)
    assert manager(0, 'links', '--user', 'bob') == bob_links
    assert central_link in manager(0, 'links')
    print('PASS: remote add/revoke on both nodes, pending/offline/lease enforcement, stable central URLs, owner and other friend unaffected', flush=True)

    docker('pause', api)
    try:
        # Poll has a 10s I/O timeout and ack could also block. The independent
        # lease watchdog must kill within the 10s lease regardless of either.
        started = time.monotonic()
        heartbeats = json.loads((data[0] / 'model/heartbeats.json').read_text())
        # Hub timestamps are slightly later than each runtime's conservative
        # monotonic lease start. Allow only Docker exec/scheduling overhead.
        remaining = max(heartbeats.values()) + 10 - time.time()
        bound = max(.5, remaining) + .9
        until(lambda: all(docker('exec', runtime, 'python', '-c',
                       "import socket; s=socket.socket(); s.settimeout(.1); print(s.connect_ex(('127.0.0.1',10000)))") != '0'
                         for runtime in runtimes), 'blocking poll overran Xray lease', timeout=bound)
        assert time.monotonic() - started <= bound + .2
    finally:
        docker('unpause', api)
    until(lambda: all(n['status'] == 'synced' for n in statuses()), 'recovery after blocked control request failed')
    manager(0, 'node', 'remove', second_id)
    peer = json.loads((data[2] / 'model/peer.json').read_text())
    auth = {'Authorization': 'Bearer ' + peer['id'] + ':' + peer['token']}
    assert https(0, '/control/v1/poll', 'POST', {'generation': 4}, auth)[0] == 403
    until(lambda: not json.loads((data[2] / 'runtime/node-status.json').read_text())['running'], 'removed node still running')
    config = json.loads(https(0, '/sub/' + token + '/clash.yaml')[1])
    assert [p['name'] for p in config['proxies']] == ['Node One']
    assert state['users']['owner'] == owner
    for runtime in runtimes:
        logs = docker('logs', runtime)
        assert not any(u['uuid'] in logs for u in (owner, alice, bob))
    print('PASS: watchdog stops during blocked HTTPS request, removed peer auth invalid immediately, no raw credentials in runtime logs', flush=True)
