"""Xray child supervision with validated atomic configs and bounded offline leases."""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import uuid
from pathlib import Path

from . import render
from .control import Hub, object_keys, request
from .storage import atomic_file
from .topology import ws_path


def validate_desired(data, expected_path):
    object_keys(data, ('generation', 'lease_seconds', 'ws_path', 'users'))
    if (type(data['generation']) is not int or not 1 <= data['generation'] <= 2**53 or
            type(data['lease_seconds']) is not int or not 10 <= data['lease_seconds'] <= 3600 or
            ws_path(data['ws_path']) != expected_path or not isinstance(data['users'], list) or
            not 1 <= len(data['users']) <= 128):
        raise ValueError('invalid desired configuration')
    if any(not isinstance(u, str) or str(uuid.UUID(u)) != u for u in data['users']) or len(set(data['users'])) != len(data['users']):
        raise ValueError('invalid desired users')
    return data


class Supervisor:
    def __init__(self, directory, binary='/usr/local/bin/xray'):
        self.directory, self.binary = Path(directory), binary
        self.child = None
        self.generation = 0
        self.last_config = None
        self.lock = threading.RLock()

    def alive(self):
        with self.lock:
            return self.child is not None and self.child.poll() is None

    def stop(self, force=False):
        with self.lock:
            if self.child is not None:
                if self.child.poll() is None:
                    if force:
                        self.child.kill()
                    else:
                        self.child.terminate()
                    try:
                        self.child.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self.child.kill()
                        self.child.wait(timeout=1)
                self.child = None
            self.generation = 0

    def apply(self, desired, on_start=lambda: None):
        if self.generation == desired['generation'] and self.alive():
            return
        config = render.xray({'ws_path': desired['ws_path'],
                              'users': {str(i): {'uuid': u} for i, u in enumerate(desired['users'])}})
        candidate = self.directory / 'candidate.json'
        target = self.directory / 'active.json'
        atomic_file(candidate, config)
        replaced = False
        try:
            subprocess.run([self.binary, 'run', '-test', '-config', str(candidate)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            with self.lock:
                self.stop()
                on_start()
                replaced = True
                os.replace(candidate, target)
                # No child output containing per-user identifiers/destinations.
                self.child = subprocess.Popen([self.binary, 'run', '-config', str(target)],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not self.alive():
                    raise RuntimeError('Xray exited during startup')
                try:
                    with socket.create_connection(('127.0.0.1', 10000), timeout=0.2):
                        time.sleep(0.3)
                        if not self.alive():
                            raise RuntimeError('Xray startup failed')
                        with self.lock:
                            if not self.alive():
                                raise RuntimeError('Xray expired during startup')
                            self.generation = desired['generation']
                            self.last_config = config
                            return
                except OSError:
                    time.sleep(0.05)
            raise RuntimeError('Xray listener not ready')
        except BaseException:
            # Validation failures leave a running last-good child intact;
            # failures after stopping it must never claim an active generation.
            if replaced or not self.alive():
                self.stop()
            raise
        finally:
            candidate.unlink(missing_ok=True)


class Runtime:
    def __init__(self, store, supervisor=None, monotonic=time.monotonic):
        self.store = store
        self.supervisor = supervisor or Supervisor(store.runtime / 'xray')
        self.clock = monotonic
        self.deadline = 0  # Never restore an on-disk lease across process restarts.
        self.error = None
        self.desired_generation = 0
        self.status_lock = threading.Lock()

    def exchange(self, action, generation):
        state = self.store.state()
        if self.store.settings(state).role == 'hub':
            return Hub(self.store).exchange(action, {'generation': generation}, local=True)
        peer = self.store.read(self.store.model / 'peer.json')
        return request(peer['origin'], action, {'generation': generation}, 'Bearer ' + peer['id'] + ':' + peer['token'])

    def tick(self):
        supervisor = self.supervisor
        if not supervisor.alive():
            supervisor.stop()
        if self.clock() >= self.deadline:
            supervisor.stop()
        try:
            state = self.store.state()
            settings = self.store.settings(state)
            disabled_local = settings.role == 'hub' and not state['nodes'].get('local', {}).get('enabled', False)
            unjoined = settings.role == 'node' and not (self.store.model / 'peer.json').exists()
            if disabled_local or unjoined:
                supervisor.stop()
                self.deadline, self.desired_generation = 0, 0
                self.error = 'NotEnrolled' if unjoined else None
                self.report()
                return
            started = self.clock()
            desired = validate_desired(self.exchange('poll', supervisor.generation), state['ws_path'])
            self.desired_generation = desired['generation']
            deadline = started + desired['lease_seconds']
            def starting():
                if self.clock() >= deadline:
                    raise TimeoutError('lease exhausted during config validation')
                self.deadline = deadline
            supervisor.apply(desired, on_start=starting)
            # A slow validation/start may exhaust a short lease. Never ack it.
            deadline = started + desired['lease_seconds']
            if self.clock() >= deadline:
                raise TimeoutError('lease exhausted during apply')
            with supervisor.lock:
                self.deadline = deadline
                if not supervisor.alive() or supervisor.generation != desired['generation']:
                    raise RuntimeError('Xray stopped before acknowledgement')
                generation = supervisor.generation
            self.exchange('ack', generation)
            self.error = None
        except Exception as error:
            self.error = type(error).__name__
            if isinstance(error, PermissionError) or isinstance(error, urllib.error.HTTPError) and error.code in (401, 403, 404):
                supervisor.stop()
                self.deadline = 0
        if self.clock() >= self.deadline:
            supervisor.stop()
        self.report()

    def report(self):
        with self.status_lock:
            supervisor = self.supervisor
            alive = supervisor.alive()
            atomic_file(self.store.runtime / 'node-status.json', render.encoded({
                'desired': self.desired_generation,
                'generation': supervisor.generation if alive else 0,
                'running': alive, 'lease_remaining': max(0, int(self.deadline - self.clock())),
                'error': self.error, 'at': time.time()}))

    def guard(self, stop):
        # Independent of socket/DNS/file-lock/config-test waits. The watchdog
        # kills Xray at the monotonic deadline even while the poller is blocked.
        while not stop.wait(0.05):
            expired = False
            # Recheck under the same lock used by apply/on_start. A renewal
            # while the watchdog waits must not kill the fresh child.
            with self.supervisor.lock:
                if self.clock() >= self.deadline and self.supervisor.alive():
                    self.supervisor.stop(force=True)
                    self.error = "LeaseExpired"
                    expired = True
            if expired:
                self.report()

    def run(self, stop):
        guardian = threading.Thread(target=self.guard, args=(stop,), daemon=True)
        guardian.start()
        try:
            while not stop.is_set():
                self.tick()
                stop.wait(2)
        finally:
            stop.set()
            guardian.join(timeout=2)
            self.supervisor.stop()
            atomic_file(self.store.runtime / 'node-status.json', render.encoded({
                'desired': self.desired_generation, 'generation': 0, 'running': False, 'lease_remaining': 0, 'error': None, 'at': time.time()}))
