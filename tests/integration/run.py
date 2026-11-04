#!/usr/bin/env python3
"""Disposable Docker integration; only loopback ephemeral high ports are published.

No GitHub access or real deployment inputs are needed. Existing containers are not
touched. The tool image must first be built as proxy-stack-tool:0.1.0.
"""
import base64
import hashlib
import http.client
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL = os.environ.get("TEST_TOOL_IMAGE", "proxy-stack-tool:0.1.0")
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


def tunnel(port, context, state, user, origin_ip):
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname="localhost") as stream:
            key = base64.b64encode(os.urandom(16)).decode()
            request = (f"GET {state['ws_path']} HTTP/1.1\r\nHost: localhost:{port}\r\n"
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


def main():
    environment = dict(os.environ, DOMAIN="proxy.example.com", MODE="standalone")
    for external in (False, True):
        command = ["docker", "compose", "--env-file", ".env.example", "--profile", "tools", "-f", "compose.yaml"]
        if external:
            command += ["-f", "compose.external.yaml"]
        layout = json.loads(subprocess.check_output(command + ["config", "--format", "json"], cwd=REPO, env=environment))
        services = layout["services"]
        assert services["manager"]["image"] == services["updater"]["image"]
        assert services["manager"]["tmpfs"] == ["/tmp:size=64m,mode=1777"]
        assert not services["xray"].get("ports")
        for service in services.values():
            assert not service.get("privileged") and service.get("network_mode") != "host"
            assert not any("docker.sock" in mount.get("source", "") for mount in service.get("volumes", []))
        mounts = {mount["target"]: mount for mount in services["updater"]["volumes"]}
        assert mounts["/data/model"]["read_only"]
        assert set(mounts) == {"/data/model", "/data/public", "/data/cache", "/data/locks"}
        if external:
            ports = services["caddy"]["ports"]
            assert len(ports) == 1 and ports[0]["host_ip"] == "127.0.0.1" and ports[0]["published"] == "18090" and ports[0]["target"] == 8080
    print("PASS: both Compose layouts and container isolation invariants")
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
            def sync_test_xray():
                config = json.loads((data / "runtime/xray/config.json").read_text())
                config["outbounds"][0]["settings"] = {"finalRules": [{"action": "allow", "network": "tcp", "port": "8000", "ip": [origin_ip]}]}
                (test_xray / "config.json").write_text(json.dumps(config))
            sync_test_xray()
            xray = identifier + "-xray"
            containers.append(xray)
            docker("run", "-d", "--name", xray, "--network", identifier, "--network-alias", "xray",
                   "--read-only", "--cap-drop=ALL", "-v", f"{test_xray}:/etc/xray:ro", XRAY,
                   "run", "-config", "/etc/xray/config.json")
            def wait_xray():
                docker("run", "--rm", "--network", identifier, "--entrypoint", "python", TOOL, "-c", """import socket,time
for attempt in range(100):
    try:
        with socket.create_connection(('xray',10000),timeout=0.2): pass
        break
    except OSError: time.sleep(0.1)
else: raise RuntimeError('test Xray not ready')
""")
            wait_xray()
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
            for tls in (False, True):
                status, headers, body = request(asset, tls=tls)
                assert status == 200 and headers["Cache-Control"] == "no-store"
                assert json.loads(body)["proxies"][0]["uuid"] == state["users"]["owner"]["uuid"]
                assert request(asset, method="HEAD", tls=tls)[0] == 200
                for path in ("/", "/manifest.json", "/releases/", "/sub/" + token + "/", "/sub/" + token + "/qx/../clash.yaml",
                             "/sub/" + token + "/%2e%2e/%2e%2e/model/state.json", "/sub/" + "0" * 48 + "/clash.yaml",
                             asset + "?extra=1", asset.replace("/clash.yaml", "/./clash.yaml"), asset.replace("/clash.yaml", "//clash.yaml")):
                    assert request(path, tls=tls)[0] == 404, path
                assert request(asset, method="POST", tls=tls)[0] == 404
            print("PASS: exact GET/HEAD routes; denied unknown paths, traversal and directory listing; real TLS chain verified")

            context = ssl.create_default_context(cafile=str(ca))
            tunnel(tls_port, context, state, state["users"]["owner"], origin_ip)
            tunnel(tls_port, context, state, state["users"]["alice"], origin_ip)
            print("PASS: actual TLS + WebSocket + VLESS TCP egress for owner and friend")
            old = state["users"]["alice"]
            manager("user", "revoke", "alice")
            assert request("/sub/" + old["token"] + "/clash.yaml")[0] == 404
            sync_test_xray()
            docker("restart", xray)
            wait_xray()
            tunnel(tls_port, context, state, state["users"]["owner"], origin_ip)
            tunnel(tls_port, context, state, state["users"]["bob"], origin_ip)
            try:
                tunnel(tls_port, context, state, old, origin_ip)
            except (EOFError, ConnectionResetError, ssl.SSLError):
                pass
            else:
                raise AssertionError("revoked UUID still accepted after Xray restart")
            print("PASS: revoked URL gone; revoked VLESS UUID rejected after restart; other users independent")

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
        except BaseException:
            for name in containers:
                print("DIAGNOSTIC (disposable test container): " + name + "\n" + docker("logs", name, check=False), flush=True)
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
