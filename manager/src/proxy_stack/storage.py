"""Locked private state and immutable, atomically selected public revisions."""
import copy
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from . import render, rules
from .model import Settings, TOKEN, username


def atomic_file(path, content, mode=0o600):
    if path.is_symlink():
        raise ValueError("refusing symlink file")
    candidate = path.with_name("." + path.name + "." + secrets.token_hex(8))
    try:
        fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)


class Store:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.model = self.root / "model"
        self.runtime = self.root / "runtime"
        self.public = self.root / "public"
        self.cache = self.root / "cache"
        self.locks = self.root / "locks"

    def layout(self, updater=False):
        # Refuse symlinks in every directory component, including caller-supplied roots.
        for parent in (self.root,) + tuple(self.root.parents):
            if parent.is_symlink():
                raise ValueError("symlink directory is not allowed")
        if self.model.is_symlink():
            raise ValueError("symlink model directory is not allowed")
        paths = [self.public, self.public / "releases", self.cache, self.locks]
        if not updater:
            paths += [self.model, self.runtime, self.runtime / "xray", self.runtime / "caddy"]
        for path in paths:
            if path.is_symlink():
                raise ValueError("symlink directory is not allowed")
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o755 if path in (self.public / "releases", self.runtime / "xray") else 0o700)

    @contextmanager
    def locked(self, updater=False):
        self.layout(updater)
        fd = os.open(self.locks / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def read(self, path):
        self.safe_path(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def safe_path(self, path):
        path.relative_to(self.root)
        if any(p.is_symlink() for p in (path,) + tuple(path.parents)):
            raise ValueError("refusing symlink path")

    def state(self):
        state = self.read(self.model / "state.json")
        if state.get("schema") != 1 or not state.get("users") or "owner" not in state["users"]:
            raise ValueError("unsupported or invalid state schema")
        for name, user in state["users"].items():
            username(name)
            if not TOKEN.fullmatch(user["token"]) or str(uuid.UUID(user["uuid"])) != user["uuid"]:
                raise ValueError("invalid credentials in state")
        if len({u["token"] for u in state["users"].values()}) != len(state["users"]) or len({u["uuid"] for u in state["users"].values()}) != len(state["users"]):
            raise ValueError("duplicate user credentials in state")
        self.settings(state)
        if not state["ws_path"].startswith("/ws/") or not TOKEN.fullmatch(state["ws_path"][4:]):
            raise ValueError("invalid WebSocket path")
        return state

    def settings(self, state):
        return Settings(**state["settings"])

    @staticmethod
    def credentials():
        return {"uuid": str(uuid.uuid4()), "token": secrets.token_hex(24)}

    def stage(self, settings, state, cache):
        files = render.public_files(settings, state, cache)
        revision = str(time.time_ns()) + "-" + secrets.token_hex(6)
        directory = self.public / "releases" / revision
        directory.mkdir(mode=0o755)
        try:
            hashes = {}
            for name, content in files.items():
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_file(path, content, 0o644)
                hashes[name] = hashlib.sha256(content.encode()).hexdigest()
            manifest = {"schema": 1, "revision": revision, "quality": cache["quality"], "files": hashes}
            atomic_file(directory / "manifest.json", render.encoded(manifest), 0o644)
            self.validate_revision(directory)
            return directory
        except BaseException:
            shutil.rmtree(directory)
            raise

    def validate_revision(self, directory):
        self.safe_path(directory)
        if directory.is_symlink() or directory.parent != self.public / "releases":
            raise ValueError("unsafe revision")
        for path in directory.rglob("*"):
            if path.is_symlink() or (not path.is_dir() and not stat.S_ISREG(path.stat().st_mode)):
                raise ValueError("unsafe public file")
        manifest = self.read(directory / "manifest.json")
        if manifest["schema"] != 1 or manifest["revision"] != directory.name:
            raise ValueError("invalid revision schema")
        actual = {str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file() and p.name != "manifest.json"}
        if actual != set(manifest["files"]):
            raise ValueError("revision file set differs from manifest")
        for name, expected in manifest["files"].items():
            if Path(name).is_absolute() or ".." in Path(name).parts or hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
                raise ValueError("revision integrity check failed")
        return manifest

    def current(self):
        path = self.public / "current"
        self.safe_path(self.public)
        if not path.is_symlink():
            raise ValueError("no published revision")
        target = Path(os.readlink(path))
        if len(target.parts) != 2 or target.parts[0] != "releases" or target.parts[1] in (".", ".."):
            raise ValueError("unsafe current symlink")
        return self.public / target

    def switch(self, directory):
        candidate = self.public / (".current-" + secrets.token_hex(8))
        try:
            candidate.symlink_to("releases/" + directory.name)
            os.replace(candidate, self.public / "current")
        finally:
            candidate.unlink(missing_ok=True)

    def commit(self, state, cache, private=False, refresh=None):
        """Stage first; ordinary failures restore all previously written files.

        This is not a cross-filesystem crash transaction. Interrupted user mutations
        require re-running init before restarting Xray; init reconciles from state.
        """
        settings = self.settings(state)
        writes = [(self.cache / "rules.json", render.encoded(cache))]
        if refresh is not None:
            writes.append((self.cache / "refresh.json", render.encoded(refresh)))
        if private:
            writes += [(self.model / "state.json", render.encoded(state)),
                       (self.runtime / "xray/config.json", render.xray(state)),
                       (self.runtime / "caddy/Caddyfile", render.caddy(settings, state)),
                       (self.runtime / "caddy/external.Caddyfile", render.caddy(settings, state, True)),
                       (self.runtime / "host-caddy.snippet", render.snippet(settings))]
        previous = {p: p.read_text() if p.exists() and not p.is_symlink() else None for p, _ in writes}
        staged = self.stage(settings, state, cache)
        try:
            for path, content in writes:
                atomic_file(path, content, 0o644 if path == self.runtime / "xray/config.json" else 0o600)
            self.switch(staged)
        except BaseException:
            for path, content in previous.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_file(path, content, 0o644 if path == self.runtime / "xray/config.json" else 0o600)
            shutil.rmtree(staged)
            raise
        # Retain only the selected revision: no revoked bearer files in old trees.
        for directory in (self.public / "releases").iterdir():
            if directory != staged:
                if directory.is_symlink():
                    directory.unlink()
                elif directory.is_dir():
                    shutil.rmtree(directory)

    def init(self, settings):
        with self.locked():
            if (self.model / "state.json").exists():
                state = self.state()
                if state["settings"] != asdict(settings):
                    raise ValueError("deployment settings differ from initialized state; restore original .env (migration is not supported)")
            else:
                state = {"schema": 1, "settings": asdict(settings), "ws_path": "/ws/" + secrets.token_hex(24),
                         "users": {"owner": self.credentials()}}
            cache = self.read(self.cache / "rules.json") if (self.cache / "rules.json").exists() else rules.bootstrap()
            self.commit(state, cache, private=True)
            return state

    def mutate_user(self, action, name):
        username(name)
        with self.locked():
            state = copy.deepcopy(self.state())
            if name == "owner":
                raise ValueError("the persistent owner cannot be added or revoked")
            if action == "add":
                if name in state["users"]:
                    raise ValueError("user already exists")
                state["users"][name] = self.credentials()
            elif action == "revoke":
                if name not in state["users"]:
                    raise ValueError("unknown user")
                del state["users"][name]
            else:
                raise ValueError("invalid user action")
            self.commit(state, self.read(self.cache / "rules.json"), private=True)

    def update(self, fetch=rules.download):
        with self.locked(updater=True):
            try:
                state = self.state()
                cache = rules.refresh(self.read(self.cache / "rules.json"), fetch)
                self.commit(state, cache, refresh={"ok": True, "at": int(time.time())})
            except Exception as error:
                # Never persist raw exceptions: HTTP exceptions can contain bearer URLs.
                atomic_file(self.cache / "refresh.json", render.encoded({"ok": False, "at": int(time.time()), "error_type": type(error).__name__}))
                raise

    def health(self):
        # A publisher can remove the previous tree between current() and open().
        for attempt in range(3):
            directory = self.current()
            try:
                manifest = self.validate_revision(directory)
                break
            except (FileNotFoundError, ValueError):
                if attempt == 2 or self.current() == directory:
                    raise
        warning = self.read(self.cache / "refresh.json") if (self.cache / "refresh.json").exists() else None
        return {"published_valid": True, "revision": manifest["revision"], "rules": manifest["quality"],
                "refresh": warning}
