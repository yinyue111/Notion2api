"""Admin API (/admin/api/*): session login, accounts, overview, request log, stats."""
import asyncio
import sqlite3
import threading
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.admin import security as sec
from app.logger import logger

router = APIRouter()

TEST_MIN_INTERVAL = 30
CREDITS_MIN_INTERVAL = 60
_last_call: dict[tuple, float] = {}
_last_call_lock = threading.Lock()


def _store(request: Request):
    st = getattr(request.app.state, "admin_store", None)
    if st is None:
        raise HTTPException(503, "Admin store not configured (TOKEN_ENC_KEY missing)")
    return st


def require_session(request: Request) -> dict:
    sess = sec.get_session(request)
    if not sess:
        raise HTTPException(401, "login required")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not sec.same_origin(request):
            raise HTTPException(403, "cross-origin request rejected")
        if not sec.const_eq(request.headers.get("x-csrf-token", ""), sess["csrf"]):
            raise HTTPException(403, "invalid CSRF token")
    return sess


def _reload_pool(request: Request) -> None:
    request.app.state.account_pool.reload(_store(request).enabled_account_configs())


class LoginBody(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=500)


@router.post("/login")
async def login(request: Request, body: LoginBody):
    ip = sec.client_ip(request)
    lock: sec.LoginLockout = request.app.state.admin_lockout
    creds: sec.AdminCredentials = request.app.state.admin_creds
    rem = lock.locked_for(ip)
    if rem:
        return JSONResponse({"error": "too many failed attempts", "retry_after": rem}, status_code=429,
                            headers={"Retry-After": str(rem)})
    if not sec.same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    if not creds.configured:
        raise HTTPException(503, "admin login not configured")
    ok = await asyncio.to_thread(creds.check, body.username, body.password)
    if not ok:
        lock.record_failure(ip)
        logger.warning("Admin login failed", extra={"request_info": {"event": "admin_login_failed", "ip": ip}})
        rem = lock.locked_for(ip)
        if rem:
            return JSONResponse({"error": "too many failed attempts", "retry_after": rem}, status_code=429,
                                headers={"Retry-After": str(rem)})
        return JSONResponse({"error": "invalid username or password"}, status_code=401)
    lock.clear(ip)
    token, csrf = sec.new_token(), sec.new_token()
    _store(request).create_session(sec.token_hash(token), creds.username, csrf, ip, sec.SESSION_TTL)
    logger.info("Admin login ok", extra={"request_info": {"event": "admin_login_ok", "ip": ip}})
    resp = JSONResponse({"ok": True, "username": creds.username, "csrf": csrf})
    resp.set_cookie(sec.SESSION_COOKIE, token, max_age=sec.SESSION_TTL, path="/", secure=True,
                    httponly=True, samesite="strict")
    return resp


@router.post("/logout")
async def logout(request: Request, sess: dict = Depends(require_session)):
    _store(request).delete_session(sess["id_hash"])
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(sec.SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return resp


@router.get("/session")
async def session(request: Request):
    sess = sec.get_session(request)
    if not sess:
        return {"authenticated": False}
    return {"authenticated": True, "username": sess["username"], "csrf": sess["csrf"],
            "expires_at": sess["expires_at"]}


# ---------------- accounts ----------------
class AccountIn(BaseModel):
    token_v2: str = Field(min_length=10, max_length=4000)
    space_id: str = Field(min_length=1, max_length=100)
    user_id: str = Field(min_length=1, max_length=100)
    label: str = Field(default="", max_length=200)
    user_email: str = Field(default="", max_length=200)
    user_name: str = Field(default="", max_length=200)
    space_view_id: str = Field(default="", max_length=100)


class AccountPatch(BaseModel):
    label: Optional[str] = Field(default=None, max_length=200)
    enabled: Optional[bool] = None
    token_v2: Optional[str] = Field(default=None, min_length=10, max_length=4000)
    user_email: Optional[str] = Field(default=None, max_length=200)


@router.get("/accounts")
async def list_accounts(request: Request, _: dict = Depends(require_session)):
    pool = request.app.state.account_pool
    cooling = pool.cooling_map()
    status = pool.status_map()
    items = await asyncio.to_thread(_store(request).list_accounts)
    for a in items:
        a["in_pool"] = a["id"] in cooling
        a["cooling_seconds"] = cooling.get(a["id"], 0)
        a["pool_status"] = status.get(a["id"])
    from app.account_pool import strategy
    return {"items": items, "strategy": strategy()}


class ExhaustedBody(BaseModel):
    exhausted: bool
    seconds: Optional[int] = Field(default=None, ge=10, le=7 * 86400)


@router.post("/accounts/{acc_id}/exhausted")
async def set_exhausted(acc_id: int, request: Request, body: ExhaustedBody, _: dict = Depends(require_session)):
    """Manually mark an account out of credits (fail over to the next one) or clear it."""
    pool = request.app.state.account_pool
    c = next((c for c in pool.clients if getattr(c, "admin_id", None) == acc_id), None)
    if c is None:
        raise HTTPException(404, "account not in pool")
    if body.exhausted:
        pool.mark_exhausted(c, body.seconds, reason="manual (admin)")
    else:
        pool.clear_exhausted(c)
    return {"ok": True, "status": pool.status_map().get(acc_id)}


@router.post("/accounts")
async def add_account(request: Request, body: AccountIn, _: dict = Depends(require_session)):
    try:
        acc_id = _store(request).add_account(body.model_dump(), label=body.label or body.user_email)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "account with this space_id/user_id already exists")
    except ValueError as e:
        raise HTTPException(400, str(e))
    _reload_pool(request)
    return {"ok": True, "id": acc_id}


@router.patch("/accounts/{acc_id}")
async def patch_account(acc_id: int, request: Request, body: AccountPatch, _: dict = Depends(require_session)):
    if not _store(request).update_account(acc_id, label=body.label, enabled=body.enabled,
                                          token_v2=body.token_v2, user_email=body.user_email):
        raise HTTPException(404, "account not found or nothing to update")
    _reload_pool(request)
    return {"ok": True}


@router.delete("/accounts/{acc_id}")
async def delete_account(acc_id: int, request: Request, _: dict = Depends(require_session)):
    if not _store(request).delete_account(acc_id):
        raise HTTPException(404, "account not found")
    _reload_pool(request)
    return {"ok": True}


def _rate_limit(kind: str, acc_id: int, interval: int) -> int:
    now = time.time()
    with _last_call_lock:
        last = _last_call.get((kind, acc_id), 0)
        if now - last < interval:
            return int(interval - (now - last)) + 1
        _last_call[(kind, acc_id)] = now
    return 0


def _client_for(request: Request, acc_id: int):
    from app.notion_client import NotionOpusAPI
    for c in request.app.state.account_pool.clients:
        if getattr(c, "admin_id", None) == acc_id:
            return c
    cfg = _store(request).get_account_config(acc_id)
    if not cfg:
        raise HTTPException(404, "account not found")
    return NotionOpusAPI(cfg)


@router.post("/accounts/{acc_id}/test")
async def test_account(acc_id: int, request: Request, _: dict = Depends(require_session)):
    wait = _rate_limit("test", acc_id, TEST_MIN_INTERVAL)
    if wait:
        return JSONResponse({"error": "rate limited", "retry_after": wait}, status_code=429)
    client = _client_for(request, acc_id)
    try:
        status, body = await asyncio.to_thread(client.api_post, "getAvailableModels", {"spaceId": client.space_id})
        models = body.get("models") if isinstance(body, dict) else None
        ok = status == 200 and isinstance(body, dict)
        msg = f"HTTP {status}" + (f", {len(models)} models" if isinstance(models, list) else "")
    except Exception as exc:
        ok, msg = False, f"{type(exc).__name__}"
    _store(request).set_test_result(acc_id, ok, msg)
    return {"ok": ok, "message": msg}


def _pick_credits(body: Any) -> dict:
    if not isinstance(body, dict):
        return {}
    keep = {}
    for k in ("basicCredits", "premiumCredits", "usage", "isEligible", "type", "limitType"):
        if k in body:
            keep[k] = body[k]
    return keep or {"raw_keys": sorted(body.keys())[:30]}


@router.post("/accounts/{acc_id}/credits")
async def account_credits(acc_id: int, request: Request, _: dict = Depends(require_session)):
    wait = _rate_limit("credits", acc_id, CREDITS_MIN_INTERVAL)
    if wait:
        return JSONResponse({"error": "rate limited", "retry_after": wait}, status_code=429)
    client = _client_for(request, acc_id)
    try:
        status, body = await asyncio.to_thread(client.api_post, "getAIUsageEligibilityV2", {"spaceId": client.space_id})
    except Exception as exc:
        raise HTTPException(502, f"Notion request failed: {type(exc).__name__}")
    if status != 200:
        raise HTTPException(502, f"Notion returned HTTP {status}")
    credits = _pick_credits(body)
    # rolling (6h) + monthly usage window: read-only, never the one-time free reset
    from app import usage_monitor
    try:
        st = await asyncio.to_thread(usage_monitor.fetch_status, client)
    except Exception:
        st = None
    if st:
        credits["rateLimit"] = st
        usage_monitor.apply(request.app.state.account_pool, client, st)
    _store(request).set_credits(acc_id, credits)
    if credits.get("isEligible") is False:
        pool = request.app.state.account_pool
        pool.mark_exhausted(client, reason="credits lookup: not eligible")
    return {"ok": True, "credits": credits}


# ---------------- overview / logs / stats ----------------
@router.get("/overview")
async def overview(request: Request, _: dict = Depends(require_session)):
    st = _store(request)
    pool = request.app.state.account_pool
    accs = await asyncio.to_thread(st.list_accounts)
    s1 = await asyncio.to_thread(st.stats, days=1)
    s7 = await asyncio.to_thread(st.stats, days=7)
    recent = await asyncio.to_thread(st.list_requests, limit=10, status="error")
    return {
        "accounts": {"total": len(accs), "enabled": sum(1 for a in accs if a["enabled"]), **pool.get_status_summary()},
        "last_24h": s1["totals"],
        "last_7d": s7["totals"],
        "by_day": s7["by_day"],
        "recent_errors": recent["items"],
        "uptime": int(time.time() - request.app.state.start_time),
        "mode": getattr(request.app.state, "mode", ""),
    }


@router.get("/requests")
async def requests_log(request: Request, limit: int = 50, offset: int = 0, model: str = "", account: str = "",
                       status: str = "", _: dict = Depends(require_session)):
    limit = max(1, min(limit, 200))
    return await asyncio.to_thread(_store(request).list_requests, limit=limit, offset=max(0, offset), model=model, account=account,
                                         status=status)


@router.get("/stats")
async def stats(request: Request, days: int = 7, _: dict = Depends(require_session)):
    return await asyncio.to_thread(_store(request).stats, days=max(1, min(days, 30)))


# ---------------- Notion e-mail login (server-side token capture) ----------------
from app.admin import notion_login as nl

_login_flows = nl.LoginFlows()


def _owner(sess: dict) -> str:
    return str(sess.get("id_hash") or sess.get("csrf") or "")


class LoginStartBody(BaseModel):
    email: str = Field(max_length=254)


class LoginVerifyBody(BaseModel):
    flow_id: str = Field(max_length=64)
    code: str = Field(max_length=64)


class LoginCommitBody(BaseModel):
    flow_id: str = Field(max_length=64)
    space_id: Optional[str] = Field(default=None, max_length=100)
    label: Optional[str] = Field(default=None, max_length=200)


def _login_err(e: "nl.LoginError"):
    return JSONResponse({"error": str(e)}, status_code=e.status)


@router.post("/notion-login/start")
async def notion_login_start(request: Request, body: LoginStartBody, sess: dict = Depends(require_session)):
    """Ask Notion to e-mail a one-time login code to the account."""
    _store(request)
    try:
        return await asyncio.to_thread(_login_flows.start, body.email, _owner(sess))
    except nl.LoginError as e:
        return _login_err(e)


@router.post("/notion-login/verify")
async def notion_login_verify(request: Request, body: LoginVerifyBody, sess: dict = Depends(require_session)):
    """Complete loginWithEmail; token_v2 is captured server-side and never returned."""
    try:
        return await asyncio.to_thread(_login_flows.verify, body.flow_id, body.code, _owner(sess))
    except nl.LoginError as e:
        return _login_err(e)


@router.post("/notion-login/commit")
async def notion_login_commit(request: Request, body: LoginCommitBody, sess: dict = Depends(require_session)):
    """Save the captured account (new) or refresh token_v2 of the existing same space/user."""
    st = _store(request)
    try:
        acc = _login_flows.take(body.flow_id, _owner(sess), body.space_id)
    except nl.LoginError as e:
        return _login_err(e)
    existing = await asyncio.to_thread(st.find_account_id, acc["space_id"], acc["user_id"])
    if existing:
        await asyncio.to_thread(st.update_account, existing, token_v2=acc["token_v2"],
                                label=body.label or None)
        acc_id, action = existing, "updated"
    else:
        label = body.label or acc.get("user_email") or ""
        acc_id = await asyncio.to_thread(st.add_account, acc, label)
        action = "added"
    acc.clear()
    _reload_pool(request)
    logger.info("Notion account login captured", extra={"request_info": {"event": "notion_login", "account_id": acc_id,
                                                                         "action": action}})
    return {"ok": True, "id": acc_id, "action": action}


@router.post("/notion-login/cancel")
async def notion_login_cancel(request: Request, body: LoginVerifyBody, sess: dict = Depends(require_session)):
    return {"ok": _login_flows.cancel(body.flow_id, _owner(sess))}


# ---------------- quota estimate (sub2api-style) ----------------
QUOTA_REFRESH_INTERVAL = 60


def _quota_payload(request: Request) -> dict:
    from app import quota_estimate as qe
    st = _store(request)
    accs = []
    for a in st.list_accounts():
        if not a.get("enabled"):
            continue
        accs.append({"id": a["id"], "label": a.get("label") or "",
                     "rate_limit": (a.get("credits") or {}).get("rateLimit")})
    return qe.estimate(accs, st.account_cost_since, st.account_first_log_ts)


@router.get("/quota")
async def quota(request: Request, _: dict = Depends(require_session)):
    """Cached quota % + $ estimate per account and pool (no Notion call)."""
    return await asyncio.to_thread(_quota_payload, request)


@router.post("/quota/refresh")
async def quota_refresh(request: Request, _: dict = Depends(require_session)):
    """Re-read getCreditRateLimitStatus (read-only, no model call) for every enabled account."""
    wait = _rate_limit("quota", 0, QUOTA_REFRESH_INTERVAL)
    if wait:
        return JSONResponse({"error": "rate limited", "retry_after": wait}, status_code=429)
    from app import usage_monitor
    st = _store(request)
    pool = request.app.state.account_pool
    refreshed = 0
    for a in await asyncio.to_thread(st.list_accounts):
        if not a.get("enabled"):
            continue
        try:
            client = _client_for(request, a["id"])
            s = await asyncio.to_thread(usage_monitor.fetch_status, client)
        except Exception:
            s = None
        if not s:
            continue
        credits = dict(a.get("credits") or {})
        credits["rateLimit"] = s
        await asyncio.to_thread(st.set_credits, a["id"], credits)
        try:
            usage_monitor.apply(pool, client, s)
        except Exception:
            pass
        refreshed += 1
    out = await asyncio.to_thread(_quota_payload, request)
    out["refreshed"] = refreshed
    return out
