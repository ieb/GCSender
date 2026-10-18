"""Accounts and login sessions for LAN access.

* ``users.json``: username -> scrypt hash (stdlib ``hashlib.scrypt``), managed
  from the command line (``gcsender --add-user NAME``). Reloaded when the file
  changes, so adding, removing or re-setting a user takes effect on a running
  server; a password change ends that user's existing sessions.
* Sessions: a random id in an HttpOnly, SameSite=Strict cookie. Only the
  SHA-256 of the id is kept (in memory and in ``sessions.json``, so a server
  restart does not log anyone out). Sessions expire after 7 days without use
  and 30 days after login.
* Failed logins are rate-limited per client address.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
MIN_PASSWORD = 8
MAX_PASSWORD = 1024
SCRYPT = {"n": 1 << 14, "r": 8, "p": 1}
IDLE_S = 7 * 24 * 3600
ABSOLUTE_S = 30 * 24 * 3600
TOUCH_SAVE_S = 300  # persist "last used" at most this often


class AuthError(ValueError):
    pass


def _hash(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=64 << 20)


def _write_private(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class UserStore:
    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._users: dict[str, dict[str, Any]] = {}
        self._mtime: float | None = None
        self._dummy = _hash("x", b"0" * 16, **SCRYPT)  # equalises timing for unknown users

    def _reload(self) -> None:
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._users, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._users = dict(data.get("users") or {})
        except (OSError, ValueError) as e:
            log.error("cannot read %s: %s", self.path, e)
            self._users = {}
        self._mtime = mtime

    def _save(self) -> None:
        if self.path is not None:
            _write_private(self.path, {"version": 1, "users": self._users})
            self._mtime = self.path.stat().st_mtime

    def names(self) -> list[str]:
        with self._lock:
            self._reload()
            return sorted(self._users)

    def get(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            self._reload()
            return self._users.get(name)

    def set_password(self, name: str, password: str) -> bool:
        """Create the user or change the password. Returns True if created."""
        if not NAME_RE.match(name or ""):
            raise AuthError("user names are 1-32 of A-Z a-z 0-9 . _ -")
        if not (MIN_PASSWORD <= len(password) <= MAX_PASSWORD):
            raise AuthError(f"passwords must be at least {MIN_PASSWORD} characters")
        salt = secrets.token_bytes(16)
        rec = {
            "salt": salt.hex(),
            "hash": _hash(password, salt, **SCRYPT).hex(),
            **SCRYPT,
            "changed": time.time(),
        }
        with self._lock:
            self._reload()
            created = name not in self._users
            rec["created"] = self._users.get(name, {}).get("created", rec["changed"])
            self._users[name] = rec
            self._save()
        return created

    def remove(self, name: str) -> bool:
        with self._lock:
            self._reload()
            if self._users.pop(name, None) is None:
                return False
            self._save()
            return True

    def verify(self, name: str, password: str) -> bool:
        rec = self.get(name) if isinstance(name, str) else None
        if not isinstance(password, str) or len(password) > MAX_PASSWORD:
            return False
        if rec is None:
            hmac.compare_digest(_hash(password, b"1" * 16, **SCRYPT), self._dummy)
            return False
        try:
            got = _hash(password, bytes.fromhex(rec["salt"]), int(rec["n"]), int(rec["r"]), int(rec["p"]))
            return hmac.compare_digest(got, bytes.fromhex(rec["hash"]))
        except (KeyError, ValueError):
            return False


class SessionStore:
    def __init__(self, path: Path | None, users: UserStore, idle_s: float = IDLE_S, absolute_s: float = ABSOLUTE_S):
        self.path = path
        self.users = users
        self.idle_s = idle_s
        self.absolute_s = absolute_s
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._last_save = 0.0
        if path is not None and path.exists():
            try:
                self._sessions = dict(json.loads(path.read_text(encoding="utf-8")).get("sessions") or {})
            except (OSError, ValueError) as e:
                log.warning("cannot read %s: %s", path, e)

    @staticmethod
    def _key(sid: str) -> str:
        return hashlib.sha256(sid.encode()).hexdigest()

    def _save(self) -> None:
        self._last_save = time.time()
        if self.path is not None:
            try:
                _write_private(self.path, {"version": 1, "sessions": self._sessions})
            except OSError as e:
                log.warning("cannot save sessions: %s", e)

    def _valid(self, s: dict[str, Any], now: float) -> bool:
        rec = self.users.get(s.get("user", ""))
        return (
            rec is not None
            and s.get("created", 0) >= rec.get("changed", 0) - 1e-6  # a password change ends older sessions
            and now - s.get("last", 0) < self.idle_s
            and now - s.get("created", 0) < self.absolute_s
        )

    def create(self, user: str, client: str, agent: str | None) -> str:
        sid = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._sessions = {k: s for k, s in self._sessions.items() if self._valid(s, now)}
            self._sessions[self._key(sid)] = {
                "user": user, "created": now, "last": now, "client": client, "agent": (agent or "")[:200],
            }
            self._save()
        return sid

    def lookup(self, sid: str | None) -> str | None:
        if not sid or len(sid) > 200:
            return None
        key = self._key(sid)
        now = time.time()
        with self._lock:
            s = self._sessions.get(key)
            if s is None:
                return None
            if not self._valid(s, now):
                del self._sessions[key]
                self._save()
                return None
            s["last"] = now
            if now - self._last_save > TOUCH_SAVE_S:
                self._save()
            return s["user"]

    def revoke(self, sid: str | None) -> None:
        if not sid:
            return
        with self._lock:
            if self._sessions.pop(self._key(sid), None) is not None:
                self._save()

    def active(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            return [
                {k: s.get(k) for k in ("user", "created", "last", "client")}
                for s in self._sessions.values() if self._valid(s, now)
            ]


class LoginLimiter:
    """After FREE failures from one address, each attempt must wait 2^k x 2 s (max 15 min)."""

    FREE = 5
    WINDOW_S = 900.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}

    def retry_after(self, client: str) -> float:
        now = time.time()
        with self._lock:
            fails = [t for t in self._fails.get(client, []) if now - t < self.WINDOW_S]
            self._fails[client] = fails
            if len(fails) < self.FREE:
                return 0.0
            wait = min(2.0 * 2 ** (len(fails) - self.FREE), self.WINDOW_S)
            return max(0.0, fails[-1] + wait - now)

    def failed(self, client: str) -> None:
        with self._lock:
            self._fails.setdefault(client, []).append(time.time())

    def succeeded(self, client: str) -> None:
        with self._lock:
            self._fails.pop(client, None)


class Auth:
    def __init__(self, state_dir: Path | None):
        self.users = UserStore(state_dir / "users.json" if state_dir else None)
        self.sessions = SessionStore(state_dir / "sessions.json" if state_dir else None, self.users)
        self.limiter = LoginLimiter()
