"""Admin auth primitives: password hashing, sessions, lockout, client IP, CSRF."""
import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
import time
from typing import Optional

import bcrypt
from fastapi import Request

SESSION_COOKIE = "__Host-n2a_session"
SESSION_TTL = int(os.getenv("ADMIN_SESSION_TTL_SECONDS", str(12 * 3600)))
LOCKOUT_MAX_FAILURES = int(os.getenv("ADMIN_LOCKOUT_MAX_FAILURES", "5"))
LOCKOUT_WINDOW = int(os.getenv("ADMIN_LOCKOUT_WINDOW_SECONDS", "900"))
LOCKOUT_DURATION = int(os.getenv("ADMIN_LOCKOUT_SECONDS", "900"))
# 全局兜底：攻击者轮换 IP 时，窗口内全站失败次数超过阈值则暂停所有登录
GLOBAL_MAX_FAILURES = int(os.getenv("ADMIN_GLOBAL_MAX_FAILURES", "30"))
GLOBAL_LOCK_SECONDS = int(os.getenv("ADMIN_GLOBAL_LOCK_SECONDS", "300"))


def const_eq(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw((password or "").encode(), (hashed or "").encode())
    except Exception:
        return False


class AdminCredentials:
    """Admin identity comes from env only (ADMIN_USERNAME + ADMIN_PASSWORD_HASH or ADMIN_PASSWORD).

    No web first-run setup -> nothing to hijack. If unset, admin login is disabled.
    """

    def __init__(self) -> None:
        self.username = os.getenv("ADMIN_USERNAME", "").strip()
        h = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
        p = os.getenv("ADMIN_PASSWORD", "")
        if not h and p:
            h = hash_password(p)
        self.password_hash = h
        # dummy hash keeps timing similar for unknown usernames
        self._dummy = hash_password(secrets.token_hex(8))

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password_hash)

    def check(self, username: str, password: str) -> bool:
        if not self.configured:
            verify_password(password, self._dummy)
            return False
        ok_user = const_eq(username or "", self.username)
        ok_pw = verify_password(password, self.password_hash)
        return ok_user and ok_pw


class LoginLockout:
    """Per-IP failed-login tracking (in memory; resets on restart)."""

    def __init__(self) -> None:
        self._fail: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._global: list[float] = []
        self._global_until = 0.0
        self._lock = threading.Lock()

    def locked_for(self, ip: str) -> int:
        with self._lock:
            until = max(self._locked.get(ip, 0), self._global_until)
            rem = int(until - time.time())
            if rem <= 0:
                self._locked.pop(ip, None)
                return 0
            return rem

    def record_failure(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            arr = [t for t in self._fail.get(ip, []) if now - t < LOCKOUT_WINDOW]
            arr.append(now)
            self._fail[ip] = arr
            self._global = [t for t in self._global if now - t < LOCKOUT_WINDOW] + [now]
            if len(self._global) >= GLOBAL_MAX_FAILURES:
                self._global_until = now + GLOBAL_LOCK_SECONDS
                self._global = []
            if len(arr) >= LOCKOUT_MAX_FAILURES:
                self._locked[ip] = now + LOCKOUT_DURATION
                self._fail.pop(ip, None)
            if len(self._fail) > 10000:  # bound memory
                self._fail.clear()

    def clear(self, ip: str) -> None:
        with self._lock:
            self._fail.pop(ip, None)


def client_ip(request: Request) -> str:
    """Peer IP; trust the last X-Forwarded-For hop only when the peer is private (Caddy on host)."""
    peer = request.client.host if request.client else ""
    try:
        private = ipaddress.ip_address(peer).is_private or ipaddress.ip_address(peer).is_loopback
    except ValueError:
        private = False
    xff = request.headers.get("x-forwarded-for", "")
    if private and xff:
        last = xff.split(",")[-1].strip()
        try:
            ipaddress.ip_address(last)
            return last
        except ValueError:
            pass
    return peer or "unknown"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def same_origin(request: Request) -> bool:
    """Reject cross-site unsafe requests: Origin (or Referer) host must equal Host when present."""
    origin = request.headers.get("origin") or ""
    if not origin:
        ref = request.headers.get("referer") or ""
        if not ref:
            return True  # non-browser client; CSRF token still required
        origin = ref
    try:
        from urllib.parse import urlparse
        o = urlparse(origin)
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        return bool(o.netloc) and o.netloc.lower() == host.lower()
    except Exception:
        return False


def get_session(request: Request) -> Optional[dict]:
    store = getattr(request.app.state, "admin_store", None)
    tok = request.cookies.get(SESSION_COOKIE)
    if not store or not tok:
        return None
    return store.get_session(token_hash(tok))
