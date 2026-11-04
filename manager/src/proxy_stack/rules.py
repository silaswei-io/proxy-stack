"""Bounded, allowlisted upstream downloads; no request-controlled source URLs."""
import hashlib
import json
import time
import urllib.request
from pathlib import Path

import yaml

from .model import SOURCES, parse_entry

BASE = "https://github.com/Loyalsoldier/clash-rules/releases/latest/download/"
MAX_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 500000
MIN_RATIO = 0.60
BOOTSTRAP = Path(__file__).parent / "bootstrap.json"


def parse_payload(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError("rule asset too large")
    # Reject YAML aliases (including recursive aliases) and non-data YAML tags.
    text = raw.decode("utf-8-sig")
    for token in yaml.scan(text):
        if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken, yaml.tokens.TagToken)):
            raise ValueError("YAML aliases, anchors and tags are not permitted")
    document = yaml.safe_load(text)
    if not isinstance(document, dict) or set(document) != {"payload"}:
        raise ValueError("expected a payload mapping")
    values = document["payload"]
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_ENTRIES:
        raise ValueError("invalid rule count")
    for value in values:
        parse_entry(value, "DIRECT")
    return list(dict.fromkeys(values))


def bootstrap():
    return {"schema": 1, "quality": "minimal-bootstrap", "fetched_at": None, "lists": json.loads(BOOTSTRAP.read_text())}


def download(name):
    if name not in SOURCES:
        raise ValueError("unknown rules source")
    request = urllib.request.Request(BASE + name + ".txt", headers={"User-Agent": "proxy-stack/0.1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(MAX_BYTES + 1)
    return parse_payload(raw)


def refresh(previous, fetch=download):
    lists = {}
    for name in SOURCES:
        values = fetch(name)
        # All callers, including injected offline fetchers, pass the same validation.
        values = parse_payload(json.dumps({"payload": values}).encode())
        old = previous["lists"][name]
        if previous["quality"] == "upstream" and len(values) < len(old) * MIN_RATIO:
            raise ValueError("suspicious shrink in " + name)
        lists[name] = values
    return {"schema": 1, "quality": "upstream", "fetched_at": int(time.time()), "lists": lists,
            "sha256": {name: hashlib.sha256(json.dumps(values).encode()).hexdigest() for name, values in lists.items()}}
