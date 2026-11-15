"""Bounded HTTPS enrollment/poll protocol and locked hub mutations.

The listener is internal to the Compose network. Caddy exposes only four exact
POST routes. No handler fetches caller-supplied URLs or logs bearer credentials.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import render
from .model import Settings, TOKEN
from .storage import atomic_file
from .topology import ws_path
from . import chains

MAX_BODY = 65536
PATHS = ('/control/v1/enroll', '/control/v1/poll', '/control/v1/ack', '/control/v1/detach')


def origin(value):
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError('invalid hub origin')
    try:
        url = urllib.parse.urlsplit(value)
        settings = Settings(url.hostname or '', 'Hub', port=url.port or 443)
        if url.scheme != 'https' or url.username or url.password or url.path or url.query or url.fragment or settings.origin != value:
            raise ValueError()
    except ValueError:
        raise ValueError('hub origin must be a canonical HTTPS domain or global public IP origin') from None
    return value


def digest(secret):
    return hashlib.sha256(secret.encode('ascii')).hexdigest()


def decode_code(value):
    try:
        if not isinstance(value, str) or len(value) > 1024:
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))
        if set(data) != {'origin', 'secret'} or not TOKEN.fullmatch(data['secret']):
            raise ValueError()
        origin(data['origin'])
        return data
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError('invalid join code') from None


def object_keys(data, keys):
    if not isinstance(data, dict) or set(data) != set(keys):
        raise ValueError('invalid request schema')


class Hub:
    def __init__(self, store):
        self.store = store

    def state(self):
        state = self.store.state()
        if self.store.settings(state).role != 'hub':
            raise ValueError('this machine is a node; use its subscription hub')
        return state

    def save(self, state, publish=False):
        if publish:
            self.store.commit(state, self.store.read(self.store.cache / 'rules.json'), private=True)
        else:
            atomic_file(self.store.model / 'state.json', render.encoded(state))

    def heartbeats(self):
        path = self.store.model / 'heartbeats.json'
        return self.store.read(path) if path.exists() else {}

    def touch(self, identifier):
        data = self.heartbeats()
        data[identifier] = time.time()
        atomic_file(self.store.model / 'heartbeats.json', render.encoded(data))

    def code(self, ttl=600):
        if type(ttl) is not int or not 30 <= ttl <= 3600:
            raise ValueError('code TTL must be 30..3600 seconds')
        with self.store.locked():
            state = self.state()
            codes = {key: expires for key, expires in state.get('join_codes', {}).items() if expires > time.time()}
            if len(codes) >= 32:
                raise ValueError('too many outstanding join codes')
            secret = secrets.token_hex(24)
            codes[digest(secret)] = time.time() + ttl
            state['join_codes'] = codes
            self.save(state)
            return base64.urlsafe_b64encode(json.dumps({'origin': self.store.settings(state).origin, 'secret': secret}, separators=(',', ':')).encode()).decode().rstrip('=')

    def enroll(self, data):
        object_keys(data, ('secret', 'settings', 'ws_path', 'peer'))
        object_keys(data['peer'], ('id', 'token'))
        if not all(isinstance(v, str) and TOKEN.fullmatch(v) for v in data['peer'].values()):
            raise ValueError('invalid proposed peer identity')
        if not isinstance(data['secret'], str) or not TOKEN.fullmatch(data['secret']):
            raise PermissionError()
        endpoint = Settings(**data['settings'])
        if endpoint.role != 'node':
            raise ValueError('enrollment requires node role')
        path = ws_path(data['ws_path'])
        with self.store.locked():
            state = self.state()
            key = digest(data['secret'])
            # Compare digests in constant time, including expired/replayed requests.
            found = next((k for k in state.get('join_codes', {}) if hmac.compare_digest(k, key)), None)
            if found is None or state['join_codes'][found] <= time.time():
                raise PermissionError()
            if sum(n['enabled'] for n in state['nodes'].values()) >= 64:
                raise ValueError('node limit reached')
            if (any(n['enabled'] and Settings(**n['settings']).node_name == endpoint.node_name for n in state['nodes'].values())
                    or any(c['name'] == endpoint.node_name for c in state.get('chains', {}).values())):
                raise ValueError('node names must be unique')
            if any(n['enabled'] and (n['settings']['domain'], n['settings']['port']) == (endpoint.domain, endpoint.port) for n in state['nodes'].values()):
                raise ValueError('node address already enrolled')
            identifier, token = data['peer']['id'], data['peer']['token']
            if identifier in state['nodes'] or any(n.get('auth') == digest(token) for n in state['nodes'].values()):
                raise ValueError('peer identity already registered')
            if len(state['nodes']) >= 128:
                referenced = {c[k] for c in state.get('chains', {}).values() for k in ('entry', 'exit')}
                obsolete = next((k for k,n in state['nodes'].items() if k != 'local' and not n['enabled'] and k not in referenced), None)
                if obsolete is None:
                    raise ValueError('node history full; remove obsolete chains first')
                del state['nodes'][obsolete]
            state['nodes'][identifier] = {'settings': asdict(endpoint), 'ws_path': path, 'enabled': True,
                                          'desired': state['generation'], 'acked': 0, 'ready': False, 'created': time.time(), 'auth': digest(token)}
            del state['join_codes'][found]
            self.save(state, publish=True)
            return {'id': identifier, 'token': token}

    def authenticate(self, state, authorization):
        if not isinstance(authorization, str) or len(authorization) != 7 + 97 or not authorization.startswith('Bearer '):
            raise PermissionError()
        identifier, separator, token = authorization[7:].partition(':')
        if not separator or not TOKEN.fullmatch(identifier) or not TOKEN.fullmatch(token):
            raise PermissionError()
        node = state['nodes'].get(identifier)
        expected = node.get('auth', '0' * 64) if node else '0' * 64
        matched = hmac.compare_digest(expected, digest(token))
        if not node or not node['enabled'] or not matched:
            raise PermissionError()
        return identifier, node

    def desired(self, state, node):
        desired = {'generation': node['desired'], 'lease_seconds': self.store.settings(state).lease_seconds,
                   'ws_path': node['ws_path'], 'users': [u['uuid'] for u in state['users'].values()]}
        routes = chains.entry_routes(state, node)
        if routes:
            desired['chains'] = routes
        return desired

    def exchange(self, action, data, authorization=None, local=False):
        object_keys(data, ('generation',))
        if type(data['generation']) is not int or not 0 <= data['generation'] <= 2**53:
            raise ValueError('invalid generation')
        with self.store.locked():
            state = self.state()
            if local:
                identifier, node = 'local', state['nodes'].get('local')
                if not node or not node['enabled']:
                    raise PermissionError()
            else:
                identifier, node = self.authenticate(state, authorization)
            publish = False
            if action == 'detach':
                node['enabled'], node['ready'] = False, False
                node.pop('auth', None)
                self.invalidate_entries(state, identifier)
                publish = True
            elif action == 'ack':
                if data['generation'] != node['desired']:
                    raise ValueError('generation changed; fetch again')
                if time.time() - self.heartbeats().get(identifier, 0) >= self.store.settings(state).lease_seconds:
                    raise ValueError('poll lease expired; fetch again')
                publish = not node['ready'] or node['acked'] != data['generation']
                node['acked'], node['ready'] = data['generation'], True
            elif action == 'poll':
                # Restarting/failed runtimes explicitly report generation zero.
                expired = time.time() - self.heartbeats().get(identifier, 0) >= self.store.settings(state).lease_seconds
                if node['ready'] and (data['generation'] != node['acked'] or expired):
                    node['ready'] = False
                    publish = True
            else:
                raise ValueError('unknown action')
            if action == 'poll':
                self.touch(identifier)
            if publish:
                self.save(state, publish=True)
            return self.desired(state, node) if action == 'poll' else {'ok': True}

    def remove(self, identifier):
        with self.store.locked():
            state = self.state()
            if identifier not in state['nodes'] or not state['nodes'][identifier]['enabled']:
                raise ValueError('unknown active node ID')
            node = state['nodes'][identifier]
            node['enabled'], node['ready'] = False, False
            node.pop('auth', None)
            self.invalidate_entries(state, identifier)
            self.save(state, publish=True)

    @staticmethod
    def invalidate_entries(state, exit_id):
        entries = {c['entry'] for c in state.get('chains', {}).values() if c['exit'] == exit_id}
        Hub.invalidate_nodes(state, entries)

    @staticmethod
    def invalidate_nodes(state, identifiers):
        if identifiers:
            state['generation'] += 1
            for ident in identifiers:
                node = state['nodes'][ident]
                if node['enabled']:
                    node['desired'], node['ready'] = state['generation'], False

    def publish(self, identifier, visible):
        if type(visible) is not bool:
            raise ValueError('publication must be on or off')
        with self.store.locked():
            state = self.state()
            node = state['nodes'].get(identifier)
            if not node or not node['enabled']:
                raise ValueError('unknown active node ID')
            node['publish'] = visible
            self.save(state, publish=True)

    def chain(self, action, ident=None, name=None, entry=None, exit=None):
        if action == 'list':
            with self.store.locked(updater=True):
                state, beats = self.state(), self.heartbeats()
                lease, now = self.store.settings(state).lease_seconds, time.time()
                def available(ident):
                    return chains.ready(state['nodes'][ident]) and now - beats.get(ident, 0) < lease
                return [dict(ident=k, name=c['name'], entry=c['entry'], exit=c['exit'],
                             status='ready' if all(available(n) for n in (c['entry'], c['exit'])) else 'unavailable')
                        for k, c in state.get('chains', {}).items()]
        with self.store.locked():
            state = self.state()
            from .model import username
            username(ident)
            lines = state.setdefault('chains', {})
            if action == 'add':
                if ident in lines:
                    raise ValueError('chain already exists')
                for node_id in (entry, exit):
                    if node_id not in state['nodes'] or not state['nodes'][node_id]['enabled']:
                        raise ValueError('chain requires active managed physical nodes (local for hub entry)')
                lines[ident] = {'id': secrets.token_hex(24), 'name': name, 'entry': entry, 'exit': exit}
                chains.validate(state)
            elif action == 'remove':
                if ident not in lines:
                    raise ValueError('unknown chain')
                entry = lines.pop(ident)['entry']
            else:
                raise ValueError('invalid chain action')
            self.invalidate_nodes(state, {entry})
            self.save(state, publish=True)

    def reap(self):
        with self.store.locked():
            state = self.state()
            beats, now = self.heartbeats(), time.time()
            changed = False
            for identifier, node in state['nodes'].items():
                if node['ready'] and now - beats.get(identifier, 0) >= self.store.settings(state).lease_seconds:
                    node['ready'] = False
                    changed = True
            if changed:
                self.save(state, publish=True)

    def status(self):
        with self.store.locked(updater=True):
            state, beats = self.state(), self.heartbeats()
            lease = self.store.settings(state).lease_seconds
            return [{'id': identifier, 'name': n['settings']['node_name'], 'address': Settings(**n['settings']).origin,
                     'publish': n.get('publish', True),
                     'desired': n['desired'], 'acknowledged': n['acked'],
                     'active': n['acked'] if n['ready'] and time.time() - beats.get(identifier, 0) < lease else 0,
                     'last_seen': beats.get(identifier),
                     'status': ('removed' if not n['enabled'] else 'offline' if time.time() - beats.get(identifier, n.get('created', 0)) >= lease
                                else 'synced' if n['ready'] and n['acked'] == n['desired'] else 'pending')}
                    for identifier, n in state['nodes'].items()]


class Handler(BaseHTTPRequestHandler):
    server_version = 'proxy-stack'

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def do_POST(self):
        status, result = 400, {'error': 'invalid request'}
        try:
            if self.path not in PATHS:
                status = 404
            elif self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                status = 413
            else:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BODY:
                    status = 413
                elif self.headers.get_content_type() != 'application/json':
                    status = 415
                else:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError('incomplete body')
                    data = json.loads(raw)
                    action = self.path.rsplit('/', 1)[1]
                    result = (self.server.hub.enroll(data) if action == 'enroll' else
                              self.server.hub.exchange(action, data, self.headers.get('Authorization')))
                    status = 200
        except PermissionError:
            status, result = 403, {'error': 'unauthorized'}
        except (ValueError, TypeError, KeyError, UnicodeError):
            pass
        except Exception:
            status, result = 503, {'error': 'temporarily unavailable'}
        content = json.dumps(result).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(content)
        self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, hub):
        self.hub = hub
        # Bound workers before spawning threads (including slow-body clients).
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # Never dump requests, UUIDs or Authorization headers.


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def request(hub_origin, action, data, authorization=None):
    origin(hub_origin)
    if action not in ('enroll', 'poll', 'ack', 'detach'):
        raise ValueError('invalid control action')
    body = json.dumps(data).encode()
    if len(body) > MAX_BODY:
        raise ValueError('request too large')
    headers = {'Content-Type': 'application/json'}
    if authorization:
        headers['Authorization'] = authorization
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                         urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req = urllib.request.Request(hub_origin + '/control/v1/' + action, body, headers, method='POST')
    with opener.open(req, timeout=10) as response:
        raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY or response.status != 200:
            raise ValueError('invalid control response')
        return json.loads(raw)


def join(store, code):
    invitation = decode_code(code)
    with store.locked():
        state = store.state()
        if store.settings(state).role != 'node':
            raise ValueError('joining requires ROLE=node in .env before init')
        if (store.model / 'peer.json').exists():
            raise ValueError('already enrolled; detach before joining again')
        # Save only this node's proposed scoped identity before sending. If the
        # enrollment response is lost, a later join can authenticate with it and
        # finish locally without replaying a consumed code or orphaning a node.
        pending_path = store.model / 'pending-peer.json'
        if pending_path.exists():
            pending = store.read(pending_path)
            if pending['origin'] != invitation['origin']:
                raise ValueError('pending enrollment belongs to another hub; run proxyctl detach before joining a different hub')
            try:
                recovered = request(pending['origin'], 'poll', {'generation': 0},
                                    'Bearer ' + pending['id'] + ':' + pending['token'])
                if recovered.get('ws_path') != state['ws_path']:
                    raise ValueError('pending enrollment does not match this node')
                os.replace(pending_path, store.model / 'peer.json')
                return
            except urllib.error.HTTPError as error:
                if error.code != 403:
                    raise
                # Definitively not enrolled (or removed); use the new code.
        peer = {'id': secrets.token_hex(24), 'token': secrets.token_hex(24)}
        pending = dict(peer, origin=invitation['origin'])
        atomic_file(pending_path, render.encoded(pending))
        assigned = request(invitation['origin'], 'enroll', {'secret': invitation['secret'],
                           'settings': state['settings'], 'ws_path': state['ws_path'], 'peer': peer})
        if assigned != peer:
            raise ValueError('invalid peer identity response')
        os.replace(pending_path, store.model / 'peer.json')



def serve(store, stop):
    if store.settings(store.state()).role != 'hub':
        stop.wait()
        return
    hub = Hub(store)
    server = Server(('0.0.0.0', 8090), hub)
    server.timeout = 1
    try:
        while not stop.is_set():
            server.handle_request()
            hub.reap()
    finally:
        server.server_close()
