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
from .routes import asset_paths


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify(store, base_url=None):
    """Compare real GET/HEAD responses with the private manifest; TLS verifies by default."""
    with store.locked(updater=True):
        state = store.state()
        if store.settings(state).role != "hub":
            raise ValueError("verify subscriptions on the hub")
        manifest = store.validate_revision(store.current())
        settings = store.settings(state)
        origin = (base_url or settings.origin).rstrip("/")
        if not origin.startswith(("https://", "http://")):
            raise ValueError("verify base must be HTTP(S)")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        token = state["users"]["owner"]["token"]
        checked = 0
        for path, name, expected_type in asset_paths(token):
            expected = manifest["files"][name]
            for alias in (path, "/" + name):
                for method in ("GET", "HEAD"):
                    request = urllib.request.Request(origin + alias, method=method)
                    with opener.open(request, timeout=20) as response:
                        if (response.status != 200 or response.headers.get("Cache-Control") != "no-store"
                                or response.headers.get("Referrer-Policy") != "no-referrer"
                                or response.headers.get("X-Content-Type-Options") != "nosniff"):
                            raise ValueError("invalid asset response")
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
        for path in ("/", "/manifest.json", "/current/", "/sub/" + token + "/", "/sub/" + "0" * 48 + "/clash.yaml",
                     "/" + token + "/", "/" + token + "/rules/unknown", "/" + "0" * 48 + "/clash",
                     "/" + token + "/quantumult-x/../clash", "/" + token + "/clash?extra=1"):
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
    sub.add_parser("preview-caddy", help=argparse.SUPPRESS)
    sub.add_parser("check-address", help=argparse.SUPPRESS)
    state_parser = sub.add_parser("install-state", help=argparse.SUPPRESS)
    state_parser.add_argument("action", choices=("check", "save", "apply", "restore", "cancel-enrollment"))
    sub.add_parser("init")
    links = sub.add_parser("links")
    links.add_argument("--user", default="owner", type=username)
    links.add_argument("--labelled", action="store_true", help="label the four private resource URLs")
    finish_parser = sub.add_parser("finish", help="bounded HTTPS/subscription and node readiness check")
    finish_parser.add_argument("--wait", type=int, choices=range(0, 121), default=0, metavar="0..120")
    finish_parser.add_argument("--https-only", action="store_true", help=argparse.SUPPRESS)
    finish_parser.add_argument("--silent", action="store_true", help=argparse.SUPPRESS)
    finish_parser.add_argument("--progress", action="store_true", help=argparse.SUPPRESS)
    finish_parser.add_argument("--command", dest="display_command", default="./proxyctl", help=argparse.SUPPRESS)
    sub.add_parser("status")
    sub.add_parser("health")
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--base-url", help="optional operator-supplied HTTP gateway URL (TLS then untested)")
    sub.add_parser("update-rules")
    sub.add_parser("serve")
    sub.add_parser("hub-api")
    sub.add_parser("runtime")
    sub.add_parser("join", help="read one-use join code from stdin, never argv")
    sub.add_parser("detach", help="stop this node and revoke its hub enrollment")
    node = sub.add_parser("node")
    node.add_argument("action", choices=("code", "list", "remove", "join"))
    node.add_argument("id", nargs="?")
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
            print(Settings.from_env().domain)
        elif args.command == "preview-caddy":
            from . import render
            settings = Settings.from_env()
            print(render.caddy(settings, {"ws_path": "/ws/" + "0" * 48}, external=settings.mode == "existing-caddy"))
        elif args.command == "check-address":
            from .discovery import check_address
            check_address(Settings.from_env().domain)
        elif args.command == "install-state":
            from .install_state import operate
            return operate(store, args.action)
        elif args.command == "init":
            store.init(Settings.from_env())
            print("Initialized; credentials preserved. Run links explicitly to display private URLs.")
        elif args.command == "user":
            store.mutate_user(args.action, args.name)
            print("User configuration published; runtimes will apply and acknowledge. Check node list for pending/synced/offline.")
        elif args.command == "update-rules":
            store.update()
            print("Rules refreshed; credentials and Xray configuration unchanged.")
        elif args.command in ("hub-api", "runtime"):
            stop = threading.Event()
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
            if args.command == "hub-api":
                from .control import serve as hub_serve
                hub_serve(store, stop)
            else:
                from .runtime import Runtime
                Runtime(store).run(stop)
        elif args.command == "node":
            from .control import Hub
            hub = Hub(store)
            if args.action == "join":
                if args.id:
                    raise ValueError("join reads its code from stdin")
                from .control import join
                join(store, sys.stdin.readline(1026).strip())
                print("Enrolled; start services and check sync status on the hub.")
            elif args.action == "code":
                if args.id:
                    raise ValueError("code takes no node ID")
                print(hub.code())
            elif args.action == "remove":
                hub.remove(args.id)
                print("Node removed; polling credentials invalidated. Offline authorization expires within its lease.")
            else:
                if args.id:
                    raise ValueError("list takes no node ID")
                state = store.state()
                if store.settings(state).role == "node":
                    peer = store.model / "peer.json"
                    print(json.dumps({"role": "node", "hub": store.read(peer)["origin"] if peer.exists() else None,
                                      "hint": "manage friends/subscriptions on hub; node join retries enrollment"}))
                else:
                    print(json.dumps(hub.status(), ensure_ascii=False))
        elif args.command == "join":
            from .control import join
            code = sys.stdin.readline(1026).strip()
            join(store, code)
            print("Enrolled; start services and check sync status on the hub.")
        elif args.command == "detach":
            from .control import request
            acknowledged = False
            with store.locked():
                if store.settings(store.state()).role != "node":
                    raise ValueError("detach is only for joined or pending nodes")
                paths = [store.model / "peer.json", store.model / "pending-peer.json"]
                identity = next((p for p in paths if p.exists()), None)
                if identity is None:
                    raise ValueError("no enrolled or pending node identity")
                peer = store.read(identity)
                try:
                    request(peer["origin"], "detach", {"generation": 0}, "Bearer " + peer["id"] + ":" + peer["token"])
                    acknowledged = True
                except Exception:
                    pass  # Still stop locally; never claim a remote acknowledgement.
                finally:
                    for path in paths:
                        path.unlink(missing_ok=True)
            print("Local node detached; runtime stops at the next poll. Hub removal " +
                  ("acknowledged." if acknowledged else "pending; offline authorization expires within its lease."))
        elif args.command == "serve":
            if store.settings(store.state()).role == "node":
                threading.Event().wait()
            else:
                serve(store, Settings.from_env().interval)
        elif args.command == "finish":
            from .finish import finish
            return finish(store, args.wait, args.display_command, args.https_only, args.silent, args.progress)
        elif args.command == "verify":
            print(json.dumps(verify(store, args.base_url), ensure_ascii=False))
        else:
            with store.locked(updater=True):
                if args.command == "links":
                    state = store.state()
                    if store.settings(state).role == "node":
                        peer_path = store.model / "peer.json"
                        hub_origin = store.read(peer_path)["origin"] if peer_path.exists() else "not enrolled; run proxyctl node join"
                        raise ValueError("subscription links belong to the hub: " + hub_origin)
                    if args.user not in state["users"]:
                        raise ValueError("unknown user")
                    paths = asset_paths(state["users"][args.user]["token"])
                    labels = ("Mihomo / Clash Meta", "Quantumult X 完整配置（iOS 待验证）",
                              "Quantumult X 节点", "Quantumult X 规则")
                    for label, (path, _, _) in zip(labels, paths):
                        print((label + ": " if args.labelled else "") + store.settings(state).origin + path)
                else:
                    result = store.health()
                    if args.command == "status":
                        state = store.state()
                        result["users"] = sorted(state["users"])
                        result["role"] = store.settings(state).role
                        if store.settings(state).role == "node":
                            peer = store.model / "peer.json"
                            result["hub"] = store.read(peer)["origin"] if peer.exists() else None
                    if (store.runtime / "node-status.json").exists():
                        result["runtime"] = store.read(store.runtime / "node-status.json")
            if args.command in ("health", "status"):
                from .control import Hub
                if store.settings(store.state()).role == "hub":
                    result["nodes"] = Hub(store).status()
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
