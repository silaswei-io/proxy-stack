#!/usr/bin/env python3
"""Disposable Docker integration; only loopback ephemeral high ports are published.

No GitHub access or real deployment inputs are needed. Existing containers are not
touched. The tool image must first be built as proxy-stack-tool:v1.0.0.
On Linux with rootful Docker, run with sudo: both the bind-mount ownership and
host Python's access to generated root-owned 0700 directories require root.
"""
import base64
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "manager/src"))
TOOL = os.environ.get("TEST_TOOL_IMAGE", "proxy-stack-tool:v1.0.0")
XRAY = os.environ.get("TEST_XRAY_IMAGE", "ghcr.io/xtls/xray-core:26.7.11@sha256:a1644183accdb0b5be967093fe34be756fd5de15fe2ee0206e842ae17350967f")
CADDY = os.environ.get("TEST_CADDY_IMAGE", "caddy:2.11.4@sha256:0c994536bddb66445885237f1a5dcc1916bccea922661c76b4e9fc24061f9b52")
MIHOMO = os.environ.get("TEST_MIHOMO_IMAGE", "metacubex/mihomo:v1.19.15@sha256:da91cd45f1c96fe964982d7bcf3e7252d122022c65694365f3cd36c76e2d2ebd")


def docker(*args, check=True):
    result = subprocess.run(["docker", *map(str, args)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError("Docker command failed: " + result.stderr + result.stdout)
    return (result.stdout + (result.stderr if args and args[0] == "logs" else "")).strip()


def recv_exact(stream, count):
    result = b""
    while len(result) < count:
        chunk = stream.recv(count - len(result))
        if not chunk:
            raise EOFError("tunnel closed")
        result += chunk
    return result


def tunnel(port, context, state, user, origin_ip, server_hostname="localhost"):
    host = "[" + server_hostname + "]" if ":" in server_hostname else server_hostname
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=server_hostname) as stream:
            key = base64.b64encode(os.urandom(16)).decode()
            request = (f"GET {state['ws_path']} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                       f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
            stream.sendall(request.encode())
            headers = b""
            while not headers.endswith(b"\r\n\r\n"):
                headers += recv_exact(stream, 1)
                if len(headers) > 16384:
                    raise ValueError("oversized websocket headers")
            assert headers.startswith(b"HTTP/1.1 101"), headers
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            assert expected in headers
            payload = (b"\x00" + uuid.UUID(user["uuid"]).bytes + b"\x00\x01" + struct.pack("!H", 8000)
                       + b"\x01" + socket.inet_aton(origin_ip)
                       + b"GET / HTTP/1.1\r\nHost: origin\r\nConnection: close\r\n\r\n")
            mask = os.urandom(4)
            length = bytes([0x80 | len(payload)]) if len(payload) < 126 else b"\xfe" + struct.pack("!H", len(payload))
            stream.sendall(b"\x82" + length + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
            result = b""
            while b"proxy-stack-egress-ok" not in result:
                first, second = recv_exact(stream, 2)
                if first & 15 == 8:
                    raise EOFError("websocket closed")
                size = second & 127
                if size == 126:
                    size = struct.unpack("!H", recv_exact(stream, 2))[0]
                elif size == 127:
                    size = struct.unpack("!Q", recv_exact(stream, 8))[0]
                if size > 1048576 or second & 128:
                    raise ValueError("unexpected websocket frame")
                result += recv_exact(stream, size)
            assert result.startswith(b"\x00\x00HTTP/1."), result[:40]


def ip_integration(root, data, identifier, containers, state, origin_ip):
    """Adapt production ACME unchanged, then use a TEST CA only for local TLS.

    Connect to loopback while verifying each public IP SAN. Python's TLS client
    omits SNI for IP literals; default_sni must select the certificate even though
    Caddy's container LocalAddr is a different private address.
    """
    for number, address in enumerate(("8.8.8.8", "2606:4700:4700::1111")):
        directory = root / ("ip" + str(number))
        directory.mkdir()
        docker("run", "--rm", "--network", "none", "--entrypoint", "python",
               "-v", f"{data}:/source:ro", "-v", f"{directory}:/ip", "-e", "TEST_IP=" + address, TOOL, "-c", """
import json, os
from pathlib import Path
from proxy_stack.model import Settings
from proxy_stack import render, rules
settings = Settings(os.environ['TEST_IP'], 'IP Integration', port=8443)
state = json.loads(Path('/source/model/state.json').read_text())
from dataclasses import asdict
state['nodes']['local']['settings'] = asdict(settings)
for name, text in render.public_files(settings, state, rules.bootstrap()).items():
    path = Path('/ip/public/current', name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
Path('/ip/Caddyfile').write_text(render.caddy(settings, state))
Path('/ip/external.Caddyfile').write_text(render.caddy(settings, state, external=True))
Path('/ip/host-caddy.snippet').write_text(render.snippet(settings))
""")
        for filename in ("Caddyfile", "host-caddy.snippet"):
            adapted = json.loads(docker("run", "--rm", "--network", "none", "-v", f"{directory}:/etc/caddy:ro", CADDY,
                                        "caddy", "adapt", "--config", "/etc/caddy/" + filename, "--adapter", "caddyfile"))
            policies = adapted["apps"]["tls"]["automation"]["policies"]
            assert len(policies) == 1 and policies[0]["subjects"] == [address]
            assert policies[0]["issuers"] == [{"module": "acme", "ca": "https://acme-v02.api.letsencrypt.org/directory", "profile": "shortlived"}]
            assert '"internal"' not in json.dumps(adapted)
            for server in adapted["apps"]["http"]["servers"].values():
                assert all(p["default_sni"] == address for p in server["tls_connection_policies"])
            docker("run", "--rm", "--network", "none", "-v", f"{directory}:/etc/caddy:ro", CADDY,
                   "caddy", "validate", "--config", "/etc/caddy/" + filename, "--adapter", "caddyfile")
            if filename == "Caddyfile":
                test_config = adapted
        external = json.loads(docker("run", "--rm", "--network", "none", "-v", f"{directory}:/etc/caddy:ro", CADDY,
                                     "caddy", "adapt", "--config", "/etc/caddy/external.Caddyfile", "--adapter", "caddyfile"))
        assert "tls" not in external["apps"]
        assert list(external["apps"]["http"]["servers"].values())[0]["listen"] == [":8080"]
        owner_dir = directory / "public/current/sub" / state["users"]["owner"]["token"]
        mihomo = directory / "mihomo"
        shutil.copytree(owner_dir / "rules", mihomo / "rules")
        shutil.copyfile(owner_dir / "clash.yaml", mihomo / "config.yaml")
        docker("run", "--rm", "--network", "none", "-v", f"{mihomo}:/root/.config/mihomo", MIHOMO,
               "-t", "-d", "/root/.config/mihomo", "-f", "/root/.config/mihomo/config.yaml")
        # Only this disposable runtime config uses an internal CA. The production
        # render above is inspected AND validated without any network access.
        test_config["apps"]["tls"]["automation"]["policies"][0]["issuers"] = [{"module": "internal"}]
        test_config["apps"]["pki"] = {"certificate_authorities": {"local": {"install_trust": False}}}
        test_config["logging"] = {"logs": {"default": {"level": "DEBUG"}}}
        config = directory / "test.json"
        config.write_text(json.dumps(test_config))
        cert_data = directory / "cert-data"
        cert_data.mkdir()
        name = identifier + "-ip" + str(number)
        containers.append(name)
        docker("run", "-d", "--name", name, "--network", identifier, "--read-only", "--cap-drop=ALL",
               "--cap-add=NET_BIND_SERVICE", "-p", "127.0.0.1::8443", "-v", f"{config}:/etc/caddy/test.json:ro",
               "-v", f"{directory / 'public'}:/srv/public:ro", "-v", f"{cert_data}:/data", "--tmpfs", "/config",
               CADDY, "caddy", "run", "--config", "/etc/caddy/test.json")
        ca = cert_data / "caddy/pki/authorities/local/root.crt"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            ports = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]
            if ca.exists() and ports.get("8443/tcp"):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("IP TLS test gateway not ready")
        port = int(ports["8443/tcp"][0]["HostPort"])
        assert port > 1024
        context = ssl.create_default_context(cafile=str(ca))
        # The CA file exists before async certificate issuance/cache loading ends.
        # Wait for a verified handshake, never mistake CA creation for readiness.
        deadline = time.monotonic() + 20
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
                    with context.wrap_socket(raw, server_hostname=address):
                        break
            except ssl.SSLCertVerificationError:
                raise
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        host = "[" + address + "]" if ":" in address else address
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname=address) as stream:
                assert any(kind == "IP Address" and ipaddress.ip_address(value) == ipaddress.ip_address(address)
                           for kind, value in stream.getpeercert()["subjectAltName"])
                stream.sendall(("GET /sub/" + state["users"]["owner"]["token"] + "/clash.yaml HTTP/1.1\r\nHost: "
                                + host + ":8443\r\nConnection: close\r\n\r\n").encode())
                response = http.client.HTTPResponse(stream)
                response.begin()
                assert response.status == 200
                assert json.loads(response.read())["proxies"][0]["server"] == address
        for bad_context, hostname in ((context, "1.1.1.1"), (ssl.create_default_context(), address)):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
                    with bad_context.wrap_socket(raw, server_hostname=hostname):
                        raise AssertionError("wrong SAN or untrusted chain accepted")
            except ssl.SSLCertVerificationError:
                pass
        tunnel(port, context, state, state["users"]["owner"], origin_ip, server_hostname=address)
        hellos = [json.loads(line)["data"]["client_hello"] for line in docker("logs", name).splitlines()
                  if '"name":"tls_get_certificate"' in line]
        assert hellos and all(hello["ServerName"] == "" for hello in hellos)
        print("PASS: " + address + " native ACME/no local issuer, Mihomo, IP SAN chain, no-SNI HTTPS/VLESS; wrong SAN/untrusted CA rejected")


def main():
    if sys.platform.startswith("linux") and os.geteuid() != 0:
        raise SystemExit(
            "Linux integration requires root with a rootful Docker daemon.\n"
            "Run: sudo python3 tests/integration/run.py\n"
            "Temporary bind mounts must be root-owned for the manager with cap_drop ALL, "
            "and host Python must read generated root-owned 0700 model directories. "
            "Production capability isolation is unchanged."
        )
    # Fast native installer/stock-host adapter acceptance is mandatory in CI and
    # release integration; it uses only private fixtures and loopback probes.
    subprocess.run([sys.executable, str(REPO / 'tests/integration/preflight.py')], check=True)
    # Fail if the public payload has drifted from the advanced Compose/wrapper.
    docker("run", "--rm", "--network", "none", "--entrypoint", "python", "-v", f"{REPO}:/source:ro", TOOL,
           "-c", "from pathlib import Path; from importlib.resources import files; "
           "t=files('proxy_stack').joinpath('templates'); "
           "assert all(Path('/source', n).read_bytes()==t.joinpath(n).read_bytes() "
           "for n in ('compose.yaml','compose.external.yaml','.env.example','proxyctl'))")
    for role, local in (("hub", "true"), ("hub", "false"), ("node", "true")):
        environment = dict(os.environ, DOMAIN="proxy.example.com", MODE="standalone", ROLE=role, LOCAL_NODE=local)
        for external in (False, True):
            command = ["docker", "compose", "--env-file", ".env.example", "--profile", "tools", "-f", "compose.yaml"]
            if external:
                command += ["-f", "compose.external.yaml"]
            layout = json.loads(subprocess.check_output(command + ["config", "--format", "json"], cwd=REPO, env=environment))
            services = layout["services"]
            assert services["manager"]["image"] == services["updater"]["image"]
            assert services["manager"]["tmpfs"] == ["/tmp:size=64m,mode=1777"]
            assert not services["xray"].get("ports") and not services["hub"].get("ports")
            assert services['xray']['command'] == ['runtime']
            assert services['hub']['command'] == ['hub-api']
            assert services['xray']['image'] == services['hub']['image'] == services['manager']['image']
            assert services['xray']['environment']['ROLE'] == role
            assert services['xray']['environment']['LOCAL_NODE'] == local
            for service in services.values():
                assert service.get("cap_drop") == ["ALL"]
                assert not service.get("privileged") and service.get("network_mode") != "host"
                assert not any("docker.sock" in mount.get("source", "") for mount in service.get("volumes", []))
            mounts = {mount["target"]: mount for mount in services["updater"]["volumes"]}
            assert mounts["/data/model"]["read_only"]
            assert set(mounts) == {"/data/model", "/data/public", "/data/cache", "/data/locks"}
            if external:
                ports = services["caddy"]["ports"]
                assert len(ports) == 1 and ports[0]["host_ip"] == "127.0.0.1" and ports[0]["published"] == "18090" and ports[0]["target"] == 8080
    print("PASS: both Compose layouts, all v1.0.0 roles and container isolation invariants")
    identifier = "proxy-stack-test-" + uuid.uuid4().hex[:12]
    containers = []
    with tempfile.TemporaryDirectory(prefix="proxy-stack-test-") as temporary:
        root = Path(temporary)
        data = root / "data"
        data.mkdir()
        network_created = False
        try:
            docker("network", "create", identifier)
            network_created = True
            def manager(*args):
                return docker("run", "--rm", "--read-only", "--cap-drop=ALL", "--network", identifier,
                              "-v", f"{data}:/data", "-e", "DOMAIN=proxy.example.com", "-e", "NODE_NAME=Integration VPS", TOOL, *args)

            manager("init")
            original = (data / "model/state.json").read_bytes()
            manager("init")
            assert (data / "model/state.json").read_bytes() == original
            manager("user", "add", "alice")
            manager("user", "add", "bob")
            state = json.loads((data / "model/state.json").read_text())
            mihomo = root / "mihomo"
            owner_dir = (data / "public/current/sub" / state["users"]["owner"]["token"])
            shutil.copytree(owner_dir / "rules", mihomo / "rules")
            shutil.copyfile(owner_dir / "clash.yaml", mihomo / "config.yaml")
            docker("run", "--rm", "--network", "none", "-v", f"{mihomo}:/root/.config/mihomo", MIHOMO,
                   "-t", "-d", "/root/.config/mihomo", "-f", "/root/.config/mihomo/config.yaml")
            # Native syntax validation also verifies the pinned Xray's non-root user
            # can read its narrowly mounted configuration.
            docker("run", "--rm", "--read-only", "--cap-drop=ALL", "-v", f"{data / 'runtime/xray'}:/etc/xray:ro",
                   XRAY, "run", "-test", "-config", "/etc/xray/config.json")
            for config in ("Caddyfile", "external.Caddyfile"):
                docker("run", "--rm", "-v", f"{data / 'runtime/caddy'}:/etc/caddy:ro", CADDY,
                       "caddy", "validate", "--config", "/etc/caddy/" + config, "--adapter", "caddyfile")
            print("PASS: idempotent init; Mihomo, Xray and both Caddy configurations validated natively")

            external = (data / "runtime/caddy/external.Caddyfile").read_text()
            site_start = external.index(":8080 {")
            tls_site = external[site_start:].replace(":8080 {", "https://localhost:8443 {\n    tls internal", 1)
            test_config = root / "Caddyfile"
            test_config.write_text((external + "\n" + tls_site).replace("{\n", "{\n    skip_install_trust\n", 1))
            cert_data = root / "cert-data"
            cert_data.mkdir()
            origin = identifier + "-origin"
            containers.append(origin)
            origin_code = ("from http.server import BaseHTTPRequestHandler,HTTPServer; "
                           "H=type('H',(BaseHTTPRequestHandler,),{'do_GET':lambda s:(s.send_response(200),s.end_headers(),s.wfile.write(b'proxy-stack-egress-ok'))}); "
                           "HTTPServer(('0.0.0.0',8000),H).serve_forever()")
            docker("run", "-d", "--name", origin, "--network", identifier, "--network-alias", "origin",
                   "--read-only", "--cap-drop=ALL", "--entrypoint", "python", TOOL, "-c", origin_code)
            origin_ip = json.loads(docker("inspect", origin))[0]["NetworkSettings"]["Networks"][identifier]["IPAddress"]
            docker("run", "--rm", "--network", identifier, "--entrypoint", "python", TOOL,
                   "-c", "import urllib.request; assert b'proxy-stack-egress-ok' in urllib.request.urlopen('http://origin:8000', timeout=10).read()")
            # Xray 26.7.11 blocks private outbound targets by default. Permit only
            # the disposable mock endpoint, in a separate temporary test config.
            # Never weaken the generated production config for this test.
            test_xray = root / "test-xray"
            test_xray.mkdir(mode=0o755)
            wrapper = test_xray / "xray-test"
            wrapper.write_text("#!/usr/local/bin/python\nimport json,os,sys\nfrom pathlib import Path\n"
                               "args=sys.argv[1:];p=Path(args[args.index('-config')+1]);c=json.loads(p.read_text())\n"
                               "c['outbounds'][0]['settings']={'finalRules':[{'action':'allow','network':'tcp','port':'8000','ip':["
                               + repr(origin_ip) + "]}]}\np.write_text(json.dumps(c))\n"
                               "os.execv('/usr/local/bin/xray',['xray']+args)\n")
            wrapper.chmod(0o755)
            xray = identifier + "-xray"
            containers.append(xray)
            runtime_code = ("import signal,threading; from proxy_stack.storage import Store; from proxy_stack.runtime import Runtime,Supervisor; "
                            "s=Store('/data'); stop=threading.Event(); signal.signal(signal.SIGTERM,lambda *_:stop.set()); "
                            "Runtime(s,Supervisor(s.runtime/'xray','/test/xray-test')).run(stop)")
            docker("run", "-d", "--name", xray, "--network", identifier, "--network-alias", "xray",
                   "--read-only", "--cap-drop=ALL", "-v", f"{data}:/data", "-v", f"{test_xray}:/test:ro",
                   "--entrypoint", "python", TOOL, "-c", runtime_code)
            def wait_xray():
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    model = json.loads((data / 'model/state.json').read_text())
                    node = model['nodes']['local']
                    if node['ready'] and node['acked'] == node['desired']:
                        return
                    time.sleep(0.1)
                raise AssertionError('supervised local node did not apply and acknowledge')
            wait_xray()
            # The same exact control paths also work in the external-Caddy layout.
            api = identifier + '-hub'
            containers.append(api)
            docker('run','-d','--name',api,'--network',identifier,'--network-alias','hub',
                   '--read-only','--cap-drop=ALL','-v',f'{data}:/data',TOOL,'hub-api')
            caddy = identifier + "-caddy"
            containers.append(caddy)
            docker("run", "-d", "--name", caddy, "--network", identifier, "--network-alias", "caddy",
                   "--read-only", "--cap-drop=ALL", "--cap-add=NET_BIND_SERVICE", "-p", "127.0.0.1::8080", "-p", "127.0.0.1::8443",
                   "-v", f"{test_config}:/etc/caddy/Caddyfile:ro", "-v", f"{data / 'public'}:/srv/public:ro",
                   "-v", f"{cert_data}:/data", "--tmpfs", "/config", CADDY)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                details = json.loads(docker("inspect", caddy))[0]
                ports = details["NetworkSettings"]["Ports"]
                if ports.get("8080/tcp") and ports.get("8443/tcp"):
                    break
                if details["State"]["Status"] == "exited":
                    raise RuntimeError("test gateway exited: " + docker("logs", caddy))
                time.sleep(0.1)
            else:
                raise RuntimeError("test gateway ports not ready")
            http_port = int(ports["8080/tcp"][0]["HostPort"])
            tls_port = int(ports["8443/tcp"][0]["HostPort"])
            assert http_port > 1024 and tls_port > 1024
            ca = cert_data / "caddy/pki/authorities/local/root.crt"
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", http_port), timeout=0.2):
                        if ca.exists():
                            break
                except OSError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("test gateway did not start")

            manager("verify", "--base-url", "http://caddy:8080")
            token = state["users"]["owner"]["token"]
            def request(path, method="GET", tls=False):
                context = ssl.create_default_context(cafile=str(ca))
                connection = (http.client.HTTPSConnection("localhost", tls_port, context=context, timeout=10)
                              if tls else http.client.HTTPConnection("127.0.0.1", http_port, timeout=10))
                try:
                    connection.request(method, path)
                    response = connection.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    connection.close()

            asset = "/sub/" + token + "/clash.yaml"
            from proxy_stack.routes import asset_paths
            for tls in (False, True):
                for clean, legacy, mime in asset_paths(token):
                    status, headers, body = request(clean, tls=tls)
                    assert status == 200 and headers["Content-Type"].split(';')[0] == mime
                    assert headers["Cache-Control"] == "no-store"
                    assert headers["Referrer-Policy"] == "no-referrer" and headers["X-Content-Type-Options"] == "nosniff"
                    assert request('/' + legacy, tls=tls)[2] == body
                    for alias in (clean, '/' + legacy):
                        status, head, empty = request(alias, method="HEAD", tls=tls)
                        assert status == 200 and not empty and head['Content-Type'].split(';')[0] == mime
                        for method in ('POST', 'PUT', 'DELETE', 'OPTIONS'):
                            assert request(alias, method=method, tls=tls)[0] == 404
                    if clean.endswith('/clash'):
                        config = json.loads(body)
                        assert config["proxies"][0]["uuid"] == state["users"]["owner"]["uuid"]
                        urls = [provider['url'] for provider in config['rule-providers'].values()]
                    elif clean.endswith('/quantumult-x'):
                        urls = re.findall(r'https://[^,\s]+', body.decode())
                        assert any(url.endswith('/quantumult-x/rules') for url in urls)
                        assert not any(url.endswith('/rules-portable') for url in urls)
                    elif clean.endswith('/quantumult-x/rules-portable'):
                        assert {row.rsplit(', ', 1)[1] for row in body.decode().splitlines()} == {'direct', 'proxy'}
                        managed = request('/' + token + '/quantumult-x/rules', tls=tls)[2].decode().splitlines()
                        portable = body.decode().splitlines()
                        assert [row.rsplit(', ', 1)[0] for row in portable] == [row.rsplit(', ', 1)[0] for row in managed]
                        urls = []
                    else:
                        urls = []
                    for url in urls:
                        from urllib.parse import urlsplit
                        path = urlsplit(url).path
                        assert path.startswith('/' + token + '/') and not path.endswith(('.yaml', '.txt', '.list', '.conf'))
                        assert request(path, tls=tls)[0] == 200
                for path in ("/", "/manifest.json", "/releases/", "/sub/" + token + "/", "/sub/" + token + "/qx/../clash.yaml",
                             "/sub/" + token + "/%2e%2e/%2e%2e/model/state.json", "/sub/" + "0" * 48 + "/clash.yaml",
                             asset + "?extra=1", asset.replace("/clash.yaml", "/./clash.yaml"), asset.replace("/clash.yaml", "//clash.yaml"),
                             '/' + token + '/', '/' + token + '/rules/unknown', '/' + token + '/quantumult-x/../clash',
                             '/' + token + '/rules/%2e%2e/clash', '/' + token + '/clash?extra=1',
                             '/' + token + '//clash', '/' + token + '/./clash', '/' + token + '/clash/',
                             '/' + '0' * 48 + '/clash', '/' + token + '/clash.yaml'):
                    assert request(path, tls=tls)[0] == 404, path
                for control_path in ('poll','ack','detach'):
                    # Empty POST bodies are rejected by the actual internal API,
                    # proving Caddy forwards exact routes in HTTP and TLS modes.
                    assert request('/control/v1/'+control_path,method='POST',tls=tls)[0] == 413
                assert request('/control/v1/enroll',method='POST',tls=tls)[0] == 413
            print("PASS: exact GET/HEAD routes; denied unknown paths, traversal and directory listing; real TLS chain verified")

            context = ssl.create_default_context(cafile=str(ca))
            tunnel(tls_port, context, state, state["users"]["owner"], origin_ip)
            tunnel(tls_port, context, state, state["users"]["alice"], origin_ip)
            print("PASS: actual TLS + WebSocket + VLESS TCP egress for owner and friend")
            ip_integration(root, data, identifier, containers, state, origin_ip)
            old = state["users"]["alice"]
            manager("user", "revoke", "alice")
            for clean, legacy, _ in asset_paths(old['token']):
                for alias in (clean, '/' + legacy):
                    for method in ('GET', 'HEAD'):
                        assert request(alias, method=method)[0] == 404
                        assert request(alias, method=method, tls=True)[0] == 404
            wait_xray()
            tunnel(tls_port, context, state, state["users"]["owner"], origin_ip)
            tunnel(tls_port, context, state, state["users"]["bob"], origin_ip)
            try:
                tunnel(tls_port, context, state, old, origin_ip)
            except (EOFError, ConnectionResetError, ssl.SSLError):
                pass
            else:
                raise AssertionError("revoked UUID still accepted after supervised apply")
            print("PASS: revoked URL gone; revoked VLESS UUID rejected after automatic apply; other users independent")

            # Test updater with exactly its production mounts, including read-only
            # model and no runtime/Xray mount. No live upstream network is used.
            before = (data / "runtime/xray/config.json").read_bytes()
            docker("run", "--rm", "--read-only", "--cap-drop=ALL", "--network", "none", "--entrypoint", "python",
                   "-v", f"{data / 'model'}:/data/model:ro", "-v", f"{data / 'cache'}:/data/cache",
                   "-v", f"{data / 'public'}:/data/public", "-v", f"{data / 'locks'}:/data/locks", TOOL,
                   "-c", "from proxy_stack.storage import Store; from proxy_stack.rules import bootstrap; Store('/data').update(lambda n: bootstrap()['lists'][n])")
            assert (data / "runtime/xray/config.json").read_bytes() == before
            manager("verify", "--base-url", "http://caddy:8080")
            print("PASS: isolated offline updater refresh without private configuration writes")
            from multinode import multinode_integration
            multinode_integration(root, identifier, containers)
        except BaseException:
            for name in containers:
                print("DIAGNOSTIC (disposable test container): " + name + "\n" + re.sub(r"/[0-9a-f]{48}(?=/|[\s?\"]|$)", "/[redacted]", docker("logs", name, check=False)), flush=True)
            raise
        finally:
            for name in reversed(containers):
                docker("rm", "-f", name, check=False)
            if network_created:
                docker("network", "rm", identifier, check=False)
            # Linux Docker creates root-owned test files on bind mounts. Cleanup
            # only this temporary tree, never the caller's repository/data.
            docker("run", "--rm", "--entrypoint", "python", "-v", f"{root}:/cleanup", TOOL,
                   "-c", "import shutil; from pathlib import Path; [shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink() for p in Path('/cleanup').iterdir()]", check=False)


if __name__ == "__main__":
    main()
