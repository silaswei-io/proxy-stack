"""Run-once manager and stop-aware updater entry point."""
import argparse
import hashlib
import json
import os
import signal
import ssl
import sys
import threading
import urllib.error
import urllib.request

from . import __version__
from .model import Settings, default_node_name, username
from .rules import download
from .storage import Store


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify(store, base_url=None):
    """Compare real GET/HEAD responses with the private manifest; TLS verifies by default."""
    with store.locked(updater=True):
        state = store.state()
        manifest = store.validate_revision(store.current())
        settings = store.settings(state)
        origin = (base_url or settings.origin).rstrip("/")
        if not origin.startswith(("https://", "http://")):
            raise ValueError("verify base must be HTTP(S)")
        opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        token = state["users"]["owner"]["token"]
        prefix = "sub/" + token + "/"
        checked = 0
        for name, expected in manifest["files"].items():
            if not name.startswith(prefix):
                continue
            for method in ("GET", "HEAD"):
                request = urllib.request.Request(origin + "/" + name, method=method)
                with opener.open(request, timeout=20) as response:
                    if response.status != 200 or response.headers.get("Cache-Control") != "no-store":
                        raise ValueError("invalid asset response")
                    expected_type = "application/yaml" if name.endswith(".yaml") else "text/plain"
                    if response.headers.get_content_type() != expected_type:
                        raise ValueError("unexpected asset MIME type")
                    if method == "GET":
                        digest = hashlib.sha256()
                        total = 0
                        while True:
                            chunk = response.read(65536)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > 128 * 1024 * 1024:
                                raise ValueError("published asset too large")
                            digest.update(chunk)
                        if digest.hexdigest() != expected:
                            raise ValueError("served asset differs from revision")
            checked += 1
        for path in ("/", "/manifest.json", "/current/", "/sub/" + token + "/", "/sub/" + "0" * 48 + "/clash.yaml"):
            try:
                opener.open(origin + path, timeout=20).close()
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    continue
                raise ValueError("unexpected unknown-path response") from None
            raise ValueError("unknown path was exposed")
        return {"http_verified": checked, "tls_chain_verified": origin.startswith("https://"),
                "native_qx": "UNVERIFIED on iOS", "external_client_reachability": "not tested", "vless_egress": "not tested by verify"}


def serve(store, interval):
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    def fetch(name):
        if stop.is_set():
            raise InterruptedError("updater stopping")
        return download(name)
    while not stop.is_set():
        try:
            store.update(fetch)
            print("rules refresh succeeded", flush=True)
            delay = interval
        except Exception as error:
            print("rules refresh warning: " + type(error).__name__ + "; keeping last good revision", file=sys.stderr, flush=True)
            delay = min(interval, 300)
        stop.wait(delay)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="proxy-stack")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("installer", help="emit a self-contained Bash installer on stdout")
    sub.add_parser("validate-input", help="validate installer inputs without writing data")
    sub.add_parser("detect-public-ip", help="detect a global public IP with bounded HTTPS probes")
    sub.add_parser("default-node-name", help="sanitize the host-provided hostname")
    sub.add_parser("init")
    links = sub.add_parser("links")
    links.add_argument("--user", default="owner", type=username)
    links.add_argument("--labelled", action="store_true", help="label the four private resource URLs")
    sub.add_parser("status")
    sub.add_parser("health")
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--base-url", help="optional operator-supplied HTTP gateway URL (TLS then untested)")
    sub.add_parser("update-rules")
    sub.add_parser("serve")
    user = sub.add_parser("user")
    user.add_argument("action", choices=("add", "revoke"))
    user.add_argument("name", type=username)
    args = parser.parse_args(argv)
    if args.command == "installer":
        from .installer import script
        sys.stdout.write(script())
        return 0
    store = Store(os.environ.get("PROXY_STACK_DATA", "/data"))
    try:
        if args.command == "detect-public-ip":
            from .discovery import detect_public_ip
            print(detect_public_ip())
        elif args.command == "default-node-name":
            print(default_node_name(os.environ.get("HOST_NODE_NAME", "")))
        elif args.command == "validate-input":
            Settings.from_env()
        elif args.command == "init":
            store.init(Settings.from_env())
            print("Initialized; credentials preserved. Run links explicitly to display private URLs.")
        elif args.command == "user":
            store.mutate_user(args.action, args.name)
            print("User configuration published. Restart Xray to apply credential changes.")
        elif args.command == "update-rules":
            store.update()
            print("Rules refreshed; credentials and Xray configuration unchanged.")
        elif args.command == "serve":
            serve(store, Settings.from_env().interval)
        elif args.command == "verify":
            print(json.dumps(verify(store, args.base_url), ensure_ascii=False))
        elif args.command == "health":
            print(json.dumps(store.health()))
        else:
            with store.locked(updater=True):
                if args.command == "links":
                    state = store.state()
                    if args.user not in state["users"]:
                        raise ValueError("unknown user")
                    url = store.settings(state).origin + "/sub/" + state["users"][args.user]["token"] + "/"
                    for label, suffix in (("Mihomo / Clash Meta", "clash.yaml"), ("QX 节点", "qx/nodes.txt"),
                                          ("QX 完整配置（iOS 待验证）", "qx/full.conf"), ("QX 规则", "qx/rules.list")):
                        print((label + ": " if args.labelled else "") + url + suffix)
                else:
                    result = store.health()
                    if args.command == "status":
                        result["users"] = sorted(store.state()["users"])
                    print(json.dumps(result))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        # Avoid URLs, UUIDs and raw state appearing in routine container logs.
        if isinstance(error, (ValueError, FileNotFoundError)):
            message = str(error) if isinstance(error, ValueError) else "not initialized or required data missing"
        else:
            message = type(error).__name__
        print("proxy-stack: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
