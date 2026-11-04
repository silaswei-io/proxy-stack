"""Explicit same-v1 installer transaction; ordinary init remains immutable."""
import copy
import hashlib
import json
import os
import shutil
import urllib.error
from dataclasses import asdict, replace
from pathlib import Path

from .storage import atomic_file
from .topology import invalidate, validate

PARTS = ('model', 'runtime', 'cache', 'public')
TRANSACTION = Path('/transaction')


def desired(store, state):
    previous = store.settings(state)
    settings = replace(previous, domain=os.environ.get('DOMAIN', previous.domain),
                       node_name=os.environ.get('NODE_NAME', previous.node_name),
                       port=int(os.environ.get('PUBLIC_PORT', previous.port)),
                       mode=os.environ.get('MODE', previous.mode), http01_only=True)
    changed_endpoint = (settings.domain, settings.port) != (previous.domain, previous.port)
    if changed_endpoint and previous.role == 'hub' and any(k != 'local' and n['enabled'] for k, n in state['nodes'].items()):
        raise ValueError('中心已有远程节点；不能自动更改控制地址/端口，原配置未改变')
    if changed_endpoint and previous.role == 'node' and any((store.model / p).exists() for p in ('peer.json', 'pending-peer.json')):
        raise ValueError('已登记节点不能自动更改地址/端口；须先解除登记，原凭据保留')
    return settings


def candidate(store):
    state = copy.deepcopy(store.state())
    settings = desired(store, state)
    if store.settings(state) != settings:
        state['settings'] = asdict(settings)
        if 'local' in state['nodes']:
            state['nodes']['local']['settings'] = asdict(settings)
        invalidate(state)
    validate(state)
    # Pure rendering catches unsupported topology/settings before any install
    # write. It never emits generated files or private content to logs/stdout.
    from . import render
    render.public_files(settings, state, store.read(store.cache / 'rules.json'))
    return state


def tree_hash(root):
    result = {}
    for path in sorted(root.rglob('*')):
        name = str(path.relative_to(root))
        if path.is_symlink():
            target = os.readlink(path)
            if name != 'public/current' or not target.startswith('releases/') or len(Path(target).parts) != 2:
                raise ValueError('unsafe installation backup symlink')
            result[name] = ['link', target]
        elif path.is_file():
            result[name] = ['file', hashlib.sha256(path.read_bytes()).hexdigest()]
    return result


def operate(store, action):
    backup = TRANSACTION
    if action == 'cancel-enrollment':
        from .control import request
        with store.locked():
            identity = next((p for p in (store.model / 'peer.json', store.model / 'pending-peer.json') if p.exists()), None)
            if identity is None:
                return 0
            peer = store.read(identity)
        try:
            request(peer['origin'], 'detach', {'generation': 0}, 'Bearer ' + peer['id'] + ':' + peer['token'])
        except urllib.error.HTTPError as error:
            if error.code not in (403, 404):
                raise
        # No running process survives installer rollback; keep identity state and
        # explicitly clear unusable enrollment only after remote removal is known.
        with store.locked():
            if store.read(identity) != peer:
                raise ValueError('enrollment changed during rollback; private state preserved')
            (store.model / 'peer.json').unlink(missing_ok=True)
            (store.model / 'pending-peer.json').unlink(missing_ok=True)
            atomic_file(store.model / 'enrollment-rollback.json', '{"revoked":true}\n')
        return 0
    if action == 'check':
        # A read-only preflight mount must never create locks/layout or chmod.
        candidate(store)
        return 0
    with store.locked():
        if action == 'save':
            store.state()
            backup.mkdir(mode=0o700, exist_ok=True)
            data = backup / 'data'
            data.mkdir(mode=0o700)
            for name in PARTS:
                shutil.copytree(store.root / name, data / name, symlinks=True)
            atomic_file(backup / 'manifest.json', json.dumps(tree_hash(data)))
        elif action == 'restore':
            data = backup / 'data'
            if tree_hash(data) != json.loads((backup / 'manifest.json').read_text()):
                raise ValueError('installation backup integrity check failed')
            for name in PARTS:
                target = store.root / name
                if target.is_symlink():
                    raise ValueError('unsafe installation restore path')
                shutil.rmtree(target, ignore_errors=True)
                shutil.copytree(data / name, target, symlinks=True)
        elif action == 'apply':
            state = candidate(store)
            store.commit(state, store.read(store.cache / 'rules.json'), private=True)
    return 0
