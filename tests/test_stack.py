import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import signal
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml

from proxy_stack import render, rules
from proxy_stack.cli import main
from proxy_stack.cli import serve
from proxy_stack.model import POLICY, Rule, Settings, groups, parse_entry, username
from proxy_stack.storage import Store
from proxy_stack.storage import atomic_file
from proxy_stack.topology import initialize

FIXTURES = Path(__file__).parent / "fixtures"


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings("proxy.example.com", "Test VPS")

    def test_rule_semantics_and_encodings(self):
        cases = [("example.com", "exact", ["example.com"], ["a.example.com"]),
                 ("+.example.com", "suffix", ["example.com", "a.example.com"], ["notexample.com"]),
                 ("*.goo?le.com", "wildcard", ["www.google.com"], ["google.com", "www.gooogle.com"]),
                 ("10.1.2.3/8", "ip4", ["10.9.8.7"], ["11.0.0.1"]),
                 ("fc00::/7", "ip6", ["fd00::1"], ["2001:db8::1"])]
        for value, kind, yes, no in cases:
            with self.subTest(value=value):
                rule = parse_entry(value, POLICY)
                self.assertEqual(rule.kind, kind)
                for target in yes:
                    self.assertTrue(rule.matches(target))
                for target in no:
                    self.assertFalse(rule.matches(target))
                if kind == "wildcard":
                    expression = rule.clash().split(",", 1)[1]
                    for target in yes:
                        self.assertIsNotNone(re.fullmatch(expression, target))
                    self.assertIn("host-wildcard", rule.qx())
        self.assertEqual(parse_entry("fc00::/7", "DIRECT").clash(), "IP-CIDR6,fc00::/7")
        for bad in ("a.com\nfinal, direct", "a.com,PROXY", "/etc/passwd", "http://x.com", "..foo", 123, ""):
            with self.assertRaises((ValueError, TypeError)):
                parse_entry(bad, "DIRECT")

    def test_first_match_priority_and_bootstrap(self):
        lists = rules.bootstrap()["lists"]
        lists["direct"] += ["+.google.com", "+.ip.sb", "+.ipapi.co"]
        lists["proxy"] += ["+.apple.com", "+.proxy.example.com", "+.local"]
        ordered = [r for _, rs in groups(self.settings, lists) for r in rs]
        route = lambda x: next((r.policy for r in ordered if r.matches(x)), POLICY)
        for domain in ("api.ip.sb", "ipapi.co", "api.ipapi.is", "ipwho.is", "www.google.com", "unknown.example"):
            self.assertEqual(route(domain), POLICY)
        for domain in ("proxy.example.com", "localhost", "a.local", "www.apple.com", "a.icloud.com", "a.cn", "10.1.2.3", "fd00::1", "1.0.1.3"):
            self.assertEqual(route(domain), "DIRECT")

    def test_payload_bom_and_reject_unsafe_yaml(self):
        self.assertEqual(rules.parse_payload(b'\xef\xbb\xbfpayload:\n  - +.example.com\n'), ["+.example.com"])
        for data in (b"payload: []", b"payload: [x]\nother: y", b"payload: &a [*a]", b"payload: !!python/object:foo {}", b"payload: [123]", b"payload: [x, x]\npayload: []"):
            with self.assertRaises((ValueError, yaml.YAMLError)):
                rules.parse_payload(data)
        with patch.object(rules, "MAX_BYTES", 2):
            with self.assertRaises(ValueError):
                rules.parse_payload(b"payload: [x]")

    def test_downloader_is_fixed_source_and_bounded(self):
        import io
        seen = []
        def response(request, timeout):
            seen.append((request.full_url, timeout))
            return io.BytesIO(b'payload: ["+.example.com"]')
        with patch("proxy_stack.rules.urllib.request.urlopen", side_effect=response):
            self.assertEqual(rules.download("proxy"), ["+.example.com"])
        self.assertEqual(seen, [(rules.BASE + "proxy.txt", 30)])
        with self.assertRaises(ValueError):
            rules.download("https://untrusted.example/source")
        with patch.object(rules, "MAX_BYTES", 5), patch("proxy_stack.rules.urllib.request.urlopen", side_effect=response):
            with self.assertRaises(ValueError):
                rules.download("proxy")

    def test_settings_reject_injection(self):
        for changes in ({"DOMAIN": "x.com\nimport /etc/passwd"}, {"DOMAIN": "x.com:443"}, {"NODE_NAME": "a,b"},
                        {"NODE_NAME": "a\n[mitm]"}, {"NODE_NAME": "VPS"}, {"PUBLIC_PORT": "0"},
                        {"CONTACT_EMAIL": "a@b.com\n}"}, {"MODE": "wrong"}, {"UPDATE_INTERVAL": "1"}):
            with patch.dict(os.environ, {"DOMAIN": "proxy.example.com", **changes}, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env()
        for value in ("../x", "A", "/owner", "a/b", "a\n", "é", ".", "a" * 33):
            with self.assertRaises(ValueError):
                username(value)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "data")
        self.settings = Settings("proxy.example.com", "测试 VPS")
        self.state = self.store.init(self.settings)

    def snapshot(self):
        return {str(p.relative_to(self.store.root)): p.read_bytes() for p in self.store.root.rglob("*") if p.is_file() and not p.is_symlink()}

    def test_idempotent_init_and_immutable_settings(self):
        state = self.store.state()
        xray = (self.store.runtime / "xray/config.json").read_bytes()
        before = self.store.validate_revision(self.store.current())["files"]
        self.store.init(self.settings)
        self.assertEqual(self.store.state(), state)
        self.assertEqual((self.store.runtime / "xray/config.json").read_bytes(), xray)
        self.assertEqual(self.store.validate_revision(self.store.current())["files"], before)
        for changes in ({"domain": "other.example.com"}, {"port": 8443}, {"mode": "existing-caddy"}):
            with self.assertRaises(ValueError):
                self.store.init(replace(self.settings, **changes))
            self.assertEqual(self.store.state(), state)
        self.assertEqual((self.store.model / "state.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.model.stat().st_mode & 0o777, 0o700)

    def test_independent_friends_and_revocation_removes_all_files(self):
        self.store.mutate_user("add", "alice")
        self.store.mutate_user("add", "bob")
        state = self.store.state()
        self.assertEqual(state["users"]["owner"], self.state["users"]["owner"])
        self.assertNotEqual(state["users"]["alice"], state["users"]["bob"])
        alice = state["users"]["alice"]
        self.assertTrue((self.store.current() / "sub" / alice["token"] / "clash.yaml").exists())
        self.store.mutate_user("revoke", "alice")
        self.assertEqual(self.store.state()["users"]["bob"], state["users"]["bob"])
        for path in (self.store.public / "releases").rglob("*"):
            self.assertNotIn(alice["token"], str(path))
            if path.is_file():
                self.assertNotIn(alice["uuid"], path.read_text())
        clients = json.loads((self.store.runtime / "xray/config.json").read_text())["inbounds"][0]["settings"]["clients"]
        self.assertEqual({c["id"] for c in clients}, {u["uuid"] for u in self.store.state()["users"].values()})
        for action, name in (("revoke", "owner"), ("add", "owner"), ("add", "bob"), ("revoke", "missing")):
            with self.assertRaises(ValueError):
                self.store.mutate_user(action, name)

    def test_updater_never_writes_private_or_rotates_credentials(self):
        private = {p: (p.read_bytes(), p.stat().st_mtime_ns) for root in (self.store.model, self.store.runtime) for p in root.rglob("*") if p.is_file()}
        self.store.update(lambda name: rules.bootstrap()["lists"][name])
        for path, (content, mtime) in private.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (content, mtime))
        self.assertEqual(self.store.health()["rules"], "upstream")

    def test_download_failure_keeps_last_good_and_health_warns(self):
        pointer = os.readlink(self.store.public / "current")
        cache = (self.store.cache / "rules.json").read_bytes()
        def fail(name):
            if name == "proxy":
                raise OSError("synthetic transport failure")
            return rules.bootstrap()["lists"][name]
        with self.assertRaises(OSError):
            self.store.update(fail)
        self.assertEqual(os.readlink(self.store.public / "current"), pointer)
        self.assertEqual((self.store.cache / "rules.json").read_bytes(), cache)
        health = self.store.health()
        self.assertTrue(health["published_valid"])
        self.assertFalse(health["refresh"]["ok"])

    def test_relative_shrink_rejected_per_source(self):
        self.store.update(lambda name: ["d" + str(n) + ".example.com" for n in range(10)])
        current = self.store.current()
        with self.assertRaises(ValueError):
            self.store.update(lambda name: ["d0.example.com"])
        self.assertEqual(self.store.current(), current)
        self.store.update(lambda name: ["d" + str(n) + ".example.com" for n in range(6)])

    def test_staging_and_switch_failure_rollback(self):
        before = self.snapshot()
        current = self.store.current()
        with patch("proxy_stack.render.public_files", side_effect=ValueError("synthetic renderer failure")):
            with self.assertRaises(ValueError):
                self.store.mutate_user("add", "alice")
        self.assertEqual(self.snapshot(), before)
        with patch.object(self.store, "switch", side_effect=OSError("synthetic atomic-switch failure")):
            with self.assertRaises(OSError):
                self.store.mutate_user("add", "alice")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.store.current(), current)

    def test_partial_private_write_rolls_back(self):
        before = self.snapshot()
        failed = []
        def write(path, content, mode=0o600):
            if path == self.store.runtime / "xray/config.json" and not failed:
                failed.append(True)
                raise OSError("synthetic disk write failure after state write")
            return atomic_file(path, content, mode)
        with patch("proxy_stack.storage.atomic_file", side_effect=write):
            with self.assertRaises(OSError):
                self.store.mutate_user("add", "alice")
        self.assertEqual(self.snapshot(), before)

    def test_corrupt_private_model_and_symlink_model_rejected(self):
        state = copy.deepcopy(self.state)
        state["settings"]["domain"] = "example.com\nimport /etc/passwd"
        (self.store.model / "state.json").write_text(json.dumps(state))
        with self.assertRaises(ValueError):
            self.store.state()
        self.store.model.rename(self.store.root / "outside-model")
        self.store.model.symlink_to(self.store.root / "outside-model", target_is_directory=True)
        with self.assertRaises(ValueError):
            with self.store.locked(updater=True):
                pass

    def test_health_rejects_symlink_release_parent(self):
        releases = self.store.public / "releases"
        releases.rename(self.store.root / "outside-releases")
        releases.symlink_to(self.store.root / "outside-releases", target_is_directory=True)
        with self.assertRaises(ValueError):
            self.store.health()

    def test_health_retries_if_publication_changes_mid_validation(self):
        validate = self.store.validate_revision
        calls = []
        def racing(directory):
            if not calls:
                calls.append(directory)
                self.store.init(self.settings)
                raise ValueError("old revision removed while validating")
            return validate(directory)
        with patch.object(self.store, "validate_revision", side_effect=racing):
            self.assertTrue(self.store.health()["published_valid"])

    def test_hash_integrity_and_symlink_traversal(self):
        directory = self.store.current()
        asset = next(directory.glob("sub/*/clash.yaml"))
        content = asset.read_text()
        asset.write_text(content + "tampered")
        with self.assertRaises(ValueError):
            self.store.health()
        asset.unlink()
        asset.symlink_to(self.store.model / "state.json")
        with self.assertRaises(ValueError):
            self.store.health()
        (self.store.public / "current").unlink()
        (self.store.public / "current").symlink_to("../model")
        with self.assertRaises(ValueError):
            self.store.current()
        linked = Path(self.temp.name) / "linked"
        linked.symlink_to(self.store.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            Store(linked).layout()

    def test_cross_process_exclusive_lock(self):
        marker = Path(self.temp.name) / "acquired"
        code = "from proxy_stack.storage import Store; from pathlib import Path; import sys\nwith Store(sys.argv[1]).locked(): Path(sys.argv[2]).write_text('acquired')\n"
        with self.store.locked():
            process = subprocess.Popen([sys.executable, "-c", code, str(self.store.root), str(marker)])
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.3)
                self.assertFalse(marker.exists())
            except BaseException:
                process.kill()
                process.wait()
                raise
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertTrue(marker.exists())

    def test_cli_does_not_log_credentials(self):
        import contextlib
        import io
        output = io.StringIO()
        with patch.dict(os.environ, {"PROXY_STACK_DATA": str(self.store.root)}), contextlib.redirect_stdout(output):
            self.assertEqual(main(["status"]), 0)
            self.assertEqual(main(["health"]), 0)
        for value in self.state["users"]["owner"].values():
            self.assertNotIn(value, output.getvalue())

    def test_scheduler_stops_during_refresh_without_second_download(self):
        handlers = {}
        calls = []
        class FakeStore:
            def update(fake, fetch):
                calls.append("update")
                handlers[signal.SIGTERM](None, None)
                # A stop that arrives during one asset prevents the next fetch.
                with self.assertRaises(InterruptedError):
                    fetch("proxy")
        with patch("proxy_stack.cli.signal.signal", side_effect=lambda sig, handler: handlers.__setitem__(sig, handler)), patch("builtins.print"):
            serve(FakeStore(), 86400)
        self.assertEqual(calls, ["update"])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings("proxy.example.com", "Fixture VPS")
        self.state = {"ws_path": "/ws/" + "b" * 48,
                      "users": {"owner": {"token": "a" * 48, "uuid": "00000000-0000-4000-8000-000000000001"}}}
        self.state["settings"] = __import__("dataclasses").asdict(self.settings)
        initialize(self.state)
        self.state["nodes"]["local"].update(ready=True, acked=1)
        self.files = render.public_files(self.settings, self.state, rules.bootstrap())
        self.base = "sub/" + "a" * 48 + "/"

    def test_all_client_references_exactly_match_published_files(self):
        clash = yaml.safe_load(self.files[self.base + "clash.yaml"])
        node = clash["proxies"][0]
        self.assertEqual(node["servername"], self.settings.domain)
        self.assertEqual(node["ws-opts"]["headers"]["Host"], self.settings.domain)
        self.assertEqual(node["ws-opts"]["path"], self.state["ws_path"])
        for provider in clash["rule-providers"].values():
            path = provider["url"].removeprefix(self.settings.origin + "/")
            self.assertIn(path, self.files)
            self.assertTrue(yaml.safe_load(self.files[path])["payload"])
        self.assertEqual(clash["rules"][-1], "MATCH,VPS")
        qx = self.files[self.base + "qx/full.conf"]
        self.assertFalse(qx.startswith("\ufeff"))
        sections = re.findall(r"^\[([^]]+)\]$", qx, re.M)
        self.assertEqual(len(sections), len(set(sections)))
        self.assertEqual(sections, ["general", "dns", "policy", "server_remote", "filter_remote", "server_local", "filter_local", "rewrite_remote", "rewrite_local", "task_local", "mitm"])
        self.assertIn("static = VPS, Fixture VPS", qx)
        self.assertIn("final, VPS", qx)
        for path in re.findall(r"https://[^,\s]+", qx):
            self.assertIn(path.removeprefix(self.settings.origin + "/"), self.files)
        nodes = self.files[self.base + "qx/nodes.txt"]
        for field in ("vless=proxy.example.com:443", "method=none", "obfs=wss", "obfs-host=proxy.example.com", "tls-verification=true", "tag=Fixture VPS"):
            self.assertIn(field, nodes)
        official = (FIXTURES / "qx-official-sample.conf").read_text()
        self.assertIn("vless=example.com:443, method=none", official)
        self.assertIn("obfs=wss", official)
        qx_rules = self.files[self.base + "qx/rules.list"].splitlines()
        expected_count = sum(len(rs) for _, rs in groups(self.settings, rules.bootstrap()["lists"]))
        self.assertEqual(len(qx_rules), expected_count)
        self.assertIn("host, api.ip.sb, VPS", qx_rules[:4])

    def test_asset_allowlist_and_golden_output_hashes(self):
        pattern = re.compile(render.ASSET_PATTERN)
        for path in self.files:
            self.assertIsNotNone(pattern.fullmatch("/" + path))
        for path in ("/sub/" + "a" * 48 + "/", "/manifest.json", "/releases/x/sub/x/clash.yaml", "/sub/" + "a" * 48 + "/qx/../clash.yaml"):
            self.assertIsNone(pattern.fullmatch(path))
        hashes = {path: hashlib.sha256(text.encode()).hexdigest() for path, text in self.files.items()}
        self.assertEqual(hashes, json.loads((FIXTURES / "render-hashes.json").read_text()))


if __name__ == "__main__":
    unittest.main()
