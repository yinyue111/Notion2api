"""Server-side Notion login for the admin panel (agent2api-style "send code / verify" flow).

The admin types the Notion account e-mail; the gateway asks Notion to mail a one-time login
code (sendTemporaryPassword), the admin types the code, and the gateway completes
loginWithEmail itself. token_v2 arrives as a Set-Cookie on *our* HTTP session, so it is
captured without touching anyone's browser, then encrypted into the admin store.

Secrets never leave this module: token_v2 / cookies are not returned to the browser and are
never logged. Pending flows live only in memory (10 min TTL) and are bound to the admin
session that started them.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from typing import Any, Callable, Optional

FLOW_TTL = 600
MAX_FLOWS = 8
SEND_MIN_INTERVAL = 60
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")


class LoginError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _notion_url() -> str:
    from app.notion_client import NOTION_URL
    return NOTION_URL


def _default_session():
    from app.notion_client import _create_http_session
    return _create_http_session()


def _safe_err(body: Any, status: int) -> str:
    if isinstance(body, dict):
        msg = str(body.get("message") or body.get("name") or "")[:200]
        if msg:
            return f"Notion: {msg}"
    return f"Notion returned HTTP {status}"


def _val(raw: Any) -> dict:
    if isinstance(raw, dict):
        v = raw.get("value")
        if isinstance(v, dict):
            return v.get("value") if isinstance(v.get("value"), dict) else v
    return {}


def candidates_from_spaces(body: Any) -> list[dict]:
    """getSpaces -> [{user_id, user_email, user_name, space_id, space_name, space_view_id}]."""
    out = []
    if not isinstance(body, dict):
        return out
    for uid, grp in body.items():
        if not isinstance(grp, dict):
            continue
        users = grp.get("notion_user") or {}
        u = _val(users.get(uid)) if isinstance(users, dict) else {}
        views = {}
        for vid, raw in (grp.get("space_view") or {}).items():
            sid = _val(raw).get("space_id")
            if sid:
                views.setdefault(sid, vid)
        for sid, raw in (grp.get("space") or {}).items():
            sp = _val(raw)
            out.append({"user_id": uid, "user_email": str(u.get("email") or ""),
                        "user_name": str(u.get("given_name") or u.get("name") or ""),
                        "space_id": sid, "space_name": str(sp.get("name") or ""),
                        "plan": str(sp.get("plan_type") or ""), "space_view_id": views.get(sid, "")})
    return out


class LoginFlows:
    def __init__(self, session_factory: Callable = _default_session, base_url: Optional[str] = None):
        self._factory = session_factory
        self._base = base_url
        self._flows: dict[str, dict] = {}
        self._last_send: dict[str, float] = {}
        self._lock = threading.Lock()

    def _url(self, ep: str) -> str:
        return f"{(self._base or _notion_url()).rstrip('/')}/api/v3/{ep}"

    def _headers(self) -> dict:
        base = (self._base or _notion_url()).rstrip("/")
        return {"content-type": "application/json", "user-agent": UA, "origin": base,
                "referer": f"{base}/login", "notion-client-version": "23.13.0.0"}

    def _gc(self) -> None:
        now = time.time()
        for k in [k for k, f in self._flows.items() if now - f["created"] > FLOW_TTL]:
            self._flows.pop(k, None)

    def _post(self, sess, ep: str, payload: dict):
        try:
            r = sess.post(self._url(ep), json=payload, headers=self._headers(), timeout=20)
        except Exception as exc:  # network
            raise LoginError(f"Notion request failed: {type(exc).__name__}", 502)
        try:
            body = r.json()
        except Exception:
            body = None
        return r, body

    def _get_flow(self, flow_id: str, owner: str) -> dict:
        with self._lock:
            self._gc()
            f = self._flows.get(flow_id)
        if not f or not secrets.compare_digest(f["owner"], owner):
            raise LoginError("login flow expired or not found; start again", 404)
        return f

    def start(self, email: str, owner: str) -> dict:
        email = (email or "").strip().lower()
        if not EMAIL_RE.match(email):
            raise LoginError("invalid e-mail address")
        now = time.time()
        with self._lock:
            self._gc()
            last = self._last_send.get(email, 0)
            if now - last < SEND_MIN_INTERVAL:
                raise LoginError(f"code already sent; retry in {int(SEND_MIN_INTERVAL - (now - last)) + 1}s", 429)
            if len(self._flows) >= MAX_FLOWS:
                raise LoginError("too many pending logins; wait a few minutes", 429)
            self._last_send[email] = now
        sess = self._factory()
        r, body = self._post(sess, "sendTemporaryPassword",
                             {"email": email, "disableLoginLink": False, "native": False, "isSignup": False})
        state = body.get("csrfState") if isinstance(body, dict) else None
        if r.status_code != 200 or not state:
            raise LoginError(_safe_err(body, r.status_code), 502 if r.status_code >= 500 else 400)
        flow_id = secrets.token_urlsafe(18)
        with self._lock:
            self._flows[flow_id] = {"owner": owner, "email": email, "state": state, "session": sess,
                                    "created": now, "attempts": 0, "token": None, "candidates": None}
        return {"flow_id": flow_id, "email": email, "expires_in": FLOW_TTL}

    def verify(self, flow_id: str, code: str, owner: str) -> dict:
        f = self._get_flow(flow_id, owner)
        code = re.sub(r"\s+", "", code or "")
        if not code or len(code) > 64:
            raise LoginError("enter the login code from the e-mail")
        if f["attempts"] >= 5:
            self.cancel(flow_id, owner)
            raise LoginError("too many wrong codes; start again", 429)
        f["attempts"] += 1
        sess = f["session"]
        r, body = self._post(sess, "loginWithEmail", {"state": f["state"], "password": code})
        token = None
        try:
            token = sess.cookies.get("token_v2")
        except Exception:
            token = None
        if r.status_code != 200 or not token:
            raise LoginError(_safe_err(body, r.status_code) if r.status_code != 200 else
                             "login accepted but no token_v2 cookie was issued", 400)
        f["token"] = token
        r2, spaces = self._post(sess, "getSpaces", {})
        cands = candidates_from_spaces(spaces) if r2.status_code == 200 else []
        if not cands:
            raise LoginError("logged in, but no Notion workspace was found for this account", 400)
        for c in cands:  # restricted (GPT-only) vs Claude-capable, shown in the picker
            try:
                from app import workspaces as _ws
                r3, models = self._post(sess, "getAvailableModels", {"spaceId": c["space_id"]})
                cap = _ws.parse_capabilities(models) if r3.status_code == 200 else None
                c["capability"] = _ws.capability_label(cap)
                if cap:
                    c["_capabilities"] = cap
            except Exception:
                c["capability"] = "unknown"
        f["candidates"] = cands
        return {"flow_id": flow_id, "candidates": [{k: c.get(k) for k in ("user_email", "user_name", "space_id",
                                                                            "space_name", "plan", "capability")} for c in cands]}

    def take(self, flow_id: str, owner: str, space_id: Optional[str] = None) -> dict:
        """Pop the finished flow -> full account dict (with token_v2) for the store."""
        f = self._get_flow(flow_id, owner)
        if not f.get("token") or not f.get("candidates"):
            raise LoginError("login not verified yet")
        cands = f["candidates"]
        if space_id:
            cands = [c for c in cands if c["space_id"] == space_id]
        if len(cands) != 1:
            raise LoginError("choose a workspace")
        self.cancel(flow_id, owner)
        c = dict(cands[0])
        c["token_v2"] = f["token"]
        c["_space_name"] = c.pop("space_name", None) or ""
        c.pop("plan", None); c.pop("capability", None)
        if not c.get("user_email"):
            c["user_email"] = f["email"]
        return c

    def cancel(self, flow_id: str, owner: str) -> bool:
        with self._lock:
            f = self._flows.get(flow_id)
            if f and secrets.compare_digest(f["owner"], owner):
                self._flows.pop(flow_id, None)
                return True
        return False
