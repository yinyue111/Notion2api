"""Admin API (/admin/api/*): session login, accounts, overview, request log, stats."""
import asyncio
import os
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


# ---------------- 6h rolling window reset (admin display) ----------------
_TPE = __import__("datetime").timezone(__import__("datetime").timedelta(hours=8), "Asia/Taipei")


def _iso_tpe(ts):
    import datetime as _dt
    try:
        return _dt.datetime.fromtimestamp(int(ts), _TPE).isoformat() if ts else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def rolling_window_info(usage, now=None) -> dict:
    """From a usage_status dict -> 6h window reset info for the admin UI/API.
    next_refresh_at = resume_at while rate-limited on the rolling window, else rolling_reset_at.
    All *_at are epoch seconds (None when unknown / no active window), *_iso in Asia/Taipei."""
    now = time.time() if now is None else now
    u = usage if isinstance(usage, dict) else {}
    rr, ra, mr = u.get("rolling_reset_at"), u.get("resume_at"), u.get("monthly_reset_at")
    limited = u.get("status") == "rate_limited"
    lb = u.get("limited_by") if limited else None
    nxt = ra if (limited and lb != "billing_period" and ra) else rr
    return {
        "status": u.get("status"), "window": u.get("window") or ("6h" if u else None),
        "rolling_pct": u.get("rolling_pct"), "monthly_pct": u.get("monthly_pct"),
        "limited": limited, "limited_by": lb,
        "rolling_reset_at": rr, "rolling_reset_iso": _iso_tpe(rr),
        "resume_at": ra, "resume_iso": _iso_tpe(ra),
        "next_refresh_at": nxt, "next_refresh_iso": _iso_tpe(nxt),
        "next_refresh_in_s": (max(0, int(nxt - now)) if nxt else None),
        "monthly_reset_at": mr, "monthly_reset_iso": _iso_tpe(mr),
        "checked_at": u.get("checked_at"),
    }


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
        a["rolling_window"] = rolling_window_info((a["pool_status"] or {}).get("usage"))
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
        if ok:
            from app import workspaces as _ws
            cap = _ws.parse_capabilities(body)
            if cap:
                msg += " · " + ("仅GPT(受限)" if cap["gpt_only"] else "Claude")
                client.capabilities = cap
                cb = getattr(request.app.state.account_pool, "on_capabilities", None)
                if cb:
                    try:
                        cb(client, cap)
                    except Exception:
                        pass
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
    prev = next((a.get("credits") for a in _store(request).list_accounts() if a.get("id") == acc_id), None) or {}
    for k in ("capabilities", "workspace_name"):  # owned by the capability refresh
        if isinstance(prev, dict) and prev.get(k):
            credits[k] = prev[k]
    _store(request).set_credits(acc_id, credits)
    if credits.get("isEligible") is False:
        pool = request.app.state.account_pool
        pool.mark_exhausted(client, reason="credits lookup: not eligible")
    return {"ok": True, "credits": credits}


# ---------------- overview / logs / stats ----------------
@router.get("/overview")
async def overview(request: Request, range: str = "", start: Optional[float] = None, end: Optional[float] = None,
                   _: dict = Depends(require_session)):
    """Usage + official-price value for a time range (presets in UTC+8, or start/end epoch seconds)."""
    from app.admin import overview as ov
    from app import pricing
    st = _store(request)
    pool = request.app.state.account_pool
    try:
        rng = ov.resolve_range(range, start, end)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    accs = await asyncio.to_thread(st.list_accounts)
    rows = await asyncio.to_thread(st.usage_rows, rng["start"], rng["end"])
    data = await asyncio.to_thread(ov.compute, rows, rng)
    data["recent_errors"] = await asyncio.to_thread(st.errors_in_range, rng["start"], rng["end"], 10)
    data["first_log_ts"] = await asyncio.to_thread(st.first_log_ts)
    tbl = pricing.load()
    data["prices"] = {"updated": tbl.get("updated"), "count": len(tbl["models"]), "file": os.path.basename(pricing.path()),
                      "error": pricing.load_error()}
    data.update({
        "accounts": {"total": len(accs), "enabled": sum(1 for a in accs if a["enabled"]), **pool.get_status_summary()},
        "uptime": int(time.time() - request.app.state.start_time),
        "mode": getattr(request.app.state, "mode", ""),
    })
    return data


# ---------------- official price table ----------------
@router.get("/prices")
async def prices_get(request: Request, _: dict = Depends(require_session)):
    from app import pricing
    from app.model_registry import MODEL_MAP
    tbl = await asyncio.to_thread(pricing.load)
    exposed = {}
    for m in MODEL_MAP:
        k, _e = pricing.lookup(m)
        exposed.setdefault(k or "", []).append(m)
    return {**tbl, "file": os.path.basename(pricing.path()), "error": pricing.load_error(),
            "aliases": {k: v for k, v in exposed.items() if k},
            "unmatched_models": exposed.get("", [])}


@router.put("/prices")
async def prices_put(request: Request, _: dict = Depends(require_session)):
    from app import pricing
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON 解析失败"}, status_code=400)
    try:
        tbl = await asyncio.to_thread(pricing.save, body)
    except pricing.PriceError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    logger.info("Official price table updated", extra={"request_info": {"event": "prices_updated", "models": len(tbl["models"])}})
    return {"ok": True, "count": len(tbl["models"]), "updated": tbl.get("updated")}


@router.post("/prices/reset")
async def prices_reset(request: Request, _: dict = Depends(require_session)):
    from app import pricing
    tbl = await asyncio.to_thread(pricing.reset)
    return {"ok": True, "count": len(tbl["models"]), "updated": tbl.get("updated")}


@router.get("/requests")
async def requests_log(request: Request, limit: int = 50, offset: int = 0, model: str = "", account: str = "",
                       status: str = "", auth: str = "", _: dict = Depends(require_session)):
    limit = max(1, min(limit, 200))
    return await asyncio.to_thread(_store(request).list_requests, limit=limit, offset=max(0, offset), model=model, account=account,
                                         status=status, auth=auth[:100])


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
        res = await asyncio.to_thread(_login_flows.verify, body.flow_id, body.code, _owner(sess))
    except nl.LoginError as e:
        return _login_err(e)
    st = request.app.state.admin_store
    if st is not None:  # mark workspaces already in the pool (re-login refreshes their token)
        for c in res.get("candidates") or []:
            try:
                c["existing_id"] = await asyncio.to_thread(st.find_by_space, c.get("space_id") or "")
            except Exception:
                c["existing_id"] = None
    return res


@router.post("/notion-login/commit")
async def notion_login_commit(request: Request, body: LoginCommitBody, sess: dict = Depends(require_session)):
    """Save the captured account (new) or refresh token_v2 of the existing same space/user."""
    st = _store(request)
    try:
        acc = _login_flows.take(body.flow_id, _owner(sess), body.space_id)
    except nl.LoginError as e:
        return _login_err(e)
    ws_name = acc.pop("_space_name", "") or ""
    caps = acc.pop("_capabilities", None)
    existing = await asyncio.to_thread(st.find_account_id, acc["space_id"], acc["user_id"])
    if existing:
        await asyncio.to_thread(st.update_account, existing, token_v2=acc["token_v2"],
                                label=body.label or None)
        acc_id, action = existing, "updated"
    else:
        email = acc.get("user_email") or ""
        same_email = [a for a in await asyncio.to_thread(st.list_accounts) if email and a.get("user_email") == email]
        # another workspace of an email already in the pool: its own entry with a distinct label
        label = body.label or (f"{email}#{str(acc.get('space_id') or '')[:8]}" if same_email else email)
        acc_id = await asyncio.to_thread(st.add_account, acc, label)
        action = "added"
    if ws_name or caps:
        try:
            cur = next((a.get("credits") for a in await asyncio.to_thread(st.list_accounts) if a.get("id") == acc_id), None) or {}
            cur = dict(cur) if isinstance(cur, dict) else {}
            if ws_name:
                cur["workspace_name"] = ws_name
            if caps:
                cur["capabilities"] = caps
            await asyncio.to_thread(st.set_credits, acc_id, cur)
        except Exception:
            pass
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


# ---------------- API keys (scoped keys + per-key model allow-list) ----------------
from app import api_keys as _ak


class KeyCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    models: list[str] = Field(default_factory=list, max_length=200)
    all_models: bool = False
    note: str = Field(default="", max_length=500)
    enabled: bool = True
    expires_at: Optional[int] = Field(default=None, ge=0)
    custom_key: Optional[str] = Field(default=None, max_length=200)


class KeyPatchBody(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    models: Optional[list[str]] = Field(default=None, max_length=200)
    all_models: Optional[bool] = None
    note: Optional[str] = Field(default=None, max_length=500)
    enabled: Optional[bool] = None
    expires_at: Optional[int] = Field(default=None, ge=0)  # 0 = never


def _master_keys() -> list:
    from app.config import API_KEY
    return [API_KEY] if API_KEY else []


def _key_err(e: "_ak.KeyError_"):
    return JSONResponse({"error": str(e)}, status_code=e.status)


def _keys_payload(request: Request) -> dict:
    keys = _ak.list_keys()
    st = getattr(request.app.state, "admin_store", None)
    usage = {}
    if st is not None:
        try:
            usage = st.auth_usage()
        except Exception:
            usage = {}
    for k in keys:
        k["usage"] = usage.get("key:" + str(k.get("name") or "scoped"))
    mk = _master_keys()
    master = {"configured": bool(mk), "has_plaintext": bool(mk), "usage": usage.get("api_key")}
    from app import config as _cfg
    return {"items": keys, "master": master, "models": _ak.model_catalog(),
            "file": os.path.basename(_ak._path()),
            "auth_required": bool(getattr(_cfg, "API_KEY", ""))}


@router.get("/keys")
async def list_api_keys(request: Request, _: dict = Depends(require_session)):
    return await asyncio.to_thread(_keys_payload, request)



@router.post("/keys/master/reveal")
async def reveal_master_key(request: Request, _: dict = Depends(require_session)):
    keys = _master_keys()
    if not keys:
        return JSONResponse({"error": "主 API_KEY 未配置"}, status_code=404)
    resp = JSONResponse({"ok": True, "key": keys[0]})
    resp.headers["Cache-Control"] = "no-store"
    return resp

@router.post("/keys")
async def create_api_key(request: Request, body: KeyCreateBody, _: dict = Depends(require_session)):
    try:
        item, plain = await asyncio.to_thread(
            _ak.create_key, body.name, body.models, body.all_models, body.note, body.enabled,
            body.expires_at or None, body.custom_key, _master_keys())
    except _ak.KeyError_ as e:
        return _key_err(e)
    resp = JSONResponse({"ok": True, "item": item, "key": plain})  # plaintext: this response only
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.patch("/keys/{key_id}")
async def patch_api_key(key_id: str, request: Request, body: KeyPatchBody, _: dict = Depends(require_session)):
    kw = body.model_dump(exclude_unset=True)
    if "expires_at" in kw:
        kw["expires_at"] = kw["expires_at"] or None
    try:
        item = await asyncio.to_thread(lambda: _ak.update_key(key_id[:40], **kw))
    except _ak.KeyError_ as e:
        return _key_err(e)
    return {"ok": True, "item": item}


@router.post("/keys/{key_id}/regenerate")
async def regenerate_api_key(key_id: str, request: Request, _: dict = Depends(require_session)):
    try:
        item, plain = await asyncio.to_thread(_ak.regenerate_key, key_id[:40], _master_keys())
    except _ak.KeyError_ as e:
        return _key_err(e)
    resp = JSONResponse({"ok": True, "item": item, "key": plain})
    resp.headers["Cache-Control"] = "no-store"
    return resp



@router.post("/keys/{key_id}/reveal")
async def reveal_api_key(key_id: str, request: Request, _: dict = Depends(require_session)):
    """Lazy plaintext fetch for admin UI (session + CSRF). Never logged."""
    try:
        plain = await asyncio.to_thread(_ak.get_plaintext, key_id[:40])
    except _ak.KeyError_ as e:
        return _key_err(e)
    resp = JSONResponse({"ok": True, "key": plain})
    resp.headers["Cache-Control"] = "no-store"
    return resp

@router.delete("/keys/{key_id}")
async def delete_api_key(key_id: str, request: Request, _: dict = Depends(require_session)):
    try:
        await asyncio.to_thread(_ak.delete_key, key_id[:40])
    except _ak.KeyError_ as e:
        return _key_err(e)
    return {"ok": True}




