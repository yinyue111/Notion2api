"""Notion AI usage-limit awareness (rolling 6h window + monthly billing-period allowance).

Notion exposes both limits through the cheap, read-only `getCreditRateLimitStatus`
endpoint (the one the web app's Settings > Notion AI > usage page and its
"usage limit reached" banner use).  Shape (2026-10):

  {"status": "within_limit" | "rate_limited" | "not_applicable",
   "window": {"window": "6h", "used": 13.67, "limit": 100, ...},   # rolling, percent
   "resetsInSeconds": 21275,             # within_limit: rolling window reset
   "retryAfterSeconds": 8237,            # rate_limited: when it can be used again
   "resumesAtMs": ...,                   # (optional) absolute resume time
   "limitedBy": "rolling" | "billing_period",
   "billingPeriodWindow": {"used": 27.5, "limit": 100, "periodEndMs": ...},  # monthly
   "freeResetAllowance": {"rollingRemaining": 1, ...}}

The one-time free reset (`resetCreditRateLimitUsage`) is NEVER called here.

When an account is limited, the in-chat symptoms are an empty stream (502), a
transcript "error" step with "temporarily-unavailable" (503) or a fair-use
"rate limit" (429), or an AiCreditUsageLimitError (429, clientData.code
"ai_credit_usage_limit").  Instead of probing with model calls, any such failure
triggers (debounced) one status lookup, which marks the account exhausted until the
exact reset time.  A background poller refreshes every account periodically and
pre-emptively demotes accounts at >= USAGE_DEMOTE_PCT of either window.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Optional

from app.logger import logger

ENDPOINT = "getCreditRateLimitStatus"
REASON_PREFIX = "usage:"


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def demote_pct() -> float:
    return _f("USAGE_DEMOTE_PCT", 95.0)


def poll_seconds() -> float:
    return _f("USAGE_POLL_SECONDS", 600.0)


def trigger_debounce() -> float:
    return _f("USAGE_CHECK_DEBOUNCE_SECONDS", 60.0)


def fallback_seconds() -> float:
    return _f("USAGE_FALLBACK_EXHAUST_SECONDS", 1800.0)


def _num(v) -> Optional[float]:
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def _pct(win: Any) -> Optional[float]:
    if not isinstance(win, dict):
        return None
    used, lim = _num(win.get("used")), _num(win.get("limit"))
    if used is None or not lim:
        return None
    return round(100.0 * used / lim, 2)


def parse_status(body: Any, now: Optional[float] = None) -> Optional[dict]:
    """Normalise a getCreditRateLimitStatus response. None if unrecognisable."""
    if not isinstance(body, dict) or "status" not in body:
        return None
    now = time.time() if now is None else now
    status = str(body.get("status") or "")
    win = body.get("window") if isinstance(body.get("window"), dict) else {}
    bp = body.get("billingPeriodWindow") if isinstance(body.get("billingPeriodWindow"), dict) else {}
    out: dict = {
        "status": status,
        "checked_at": int(now),
        "window": win.get("window"),
        "rolling_pct": _pct(win),
        "monthly_pct": _pct(bp),
        "limited_by": body.get("limitedBy") if status == "rate_limited" else None,
    }
    rs = _num(body.get("resetsInSeconds"))
    out["rolling_reset_at"] = int(now + rs) if rs is not None else None
    pe = _num(bp.get("periodEndMs"))
    out["monthly_reset_at"] = int(pe / 1000) if pe else None
    resume = None
    if status == "rate_limited":
        ra = _num(body.get("resumesAtMs"))
        if ra:
            resume = ra / 1000.0
        else:
            sec = _num(body.get("retryAfterSeconds"))
            if sec is not None:
                resume = now + sec
            elif out["limited_by"] == "billing_period" and out["monthly_reset_at"]:
                resume = float(out["monthly_reset_at"])
        if resume and out["limited_by"] != "billing_period" and out["rolling_reset_at"] is None:
            out["rolling_reset_at"] = int(resume)
    out["resume_at"] = int(resume) if resume else None
    fr = body.get("freeResetAllowance")
    if isinstance(fr, dict):
        out["free_reset_remaining"] = {k: fr.get(k) for k in ("rollingRemaining", "monthlyRemaining")}
    if status == "not_applicable":
        _apply_flex_allowance(out, body)
    return out


def _apply_flex_allowance(out: dict, body: dict) -> None:
    """Workspaces on the newer credit-bucket plan answer status "not_applicable" (no
    per-user standard window) and bill explicit-model chats to a small per-workspace
    "flex" allowance (billing period).  Expose it as monthly_pct / monthly_reset_at so
    the UI and the demote logic can see it; status stays "not_applicable" (headroom keeps
    the account ineligible) unless the bucket is used up, then it becomes rate_limited
    by billing_period until the period ends."""
    flex = None
    for a in body.get("allowances") or []:
        if (isinstance(a, dict) and a.get("bucketId") == "flex" and a.get("state") == "resolved"
                and a.get("cadence", "billing_period") == "billing_period"):
            flex = a
            break
    if not flex:
        return
    pct = _pct(flex)
    if pct is None:
        cr = flex.get("credits") if isinstance(flex.get("credits"), dict) else {}
        pct = _pct(cr)
    if pct is None:
        return
    out["monthly_pct"] = pct
    pe = _num(flex.get("periodEndMs"))
    if pe:
        out["monthly_reset_at"] = int(pe / 1000)
    cr = flex.get("credits") if isinstance(flex.get("credits"), dict) else {}
    out["credit_bucket"] = {"bucket": "flex", "used": cr.get("used"), "limit": cr.get("limit"),
                            "enforcement": flex.get("enforcement")}
    msd = body.get("modelSelectionDecisions") if isinstance(body.get("modelSelectionDecisions"), dict) else {}
    fx = msd.get("flex") if isinstance(msd.get("flex"), dict) else {}
    if pct >= 100.0 or fx.get("allowanceExhausted") is True:
        out["status"] = "rate_limited"
        out["limited_by"] = "billing_period"
        out["raw_status"] = "not_applicable"
        if out.get("monthly_reset_at"):
            out["resume_at"] = int(out["monthly_reset_at"])


def decide(st: Optional[dict], now: Optional[float] = None, threshold: Optional[float] = None) -> tuple:
    """-> ("exhaust", until_ts, reason) | ("clear", 0, "") | ("keep", 0, "")."""
    if not st:
        return ("keep", 0, "")
    now = time.time() if now is None else now
    thr = demote_pct() if threshold is None else threshold
    if st["status"] == "rate_limited":
        until = st.get("resume_at") or (now + fallback_seconds())
        by = st.get("limited_by") or "rolling"
        pct = st.get("monthly_pct") if by == "billing_period" else st.get("rolling_pct")
        return ("exhaust", max(until, now + 30), f"{REASON_PREFIX} {by} limit reached ({pct}%)")
    if st["status"] == "not_applicable":
        mp = st.get("monthly_pct")
        if mp is not None and mp >= thr and st.get("monthly_reset_at"):
            return ("exhaust", st["monthly_reset_at"], f"{REASON_PREFIX} flex {mp}% >= {thr:g}% (pre-emptive)")
        return ("keep", 0, "")
    if st["status"] != "within_limit":
        return ("keep", 0, "")
    mp, rp = st.get("monthly_pct"), st.get("rolling_pct")
    if mp is not None and mp >= thr and st.get("monthly_reset_at"):
        return ("exhaust", st["monthly_reset_at"], f"{REASON_PREFIX} monthly {mp}% >= {thr:g}% (pre-emptive)")
    if rp is not None and rp >= thr:
        until = st.get("rolling_reset_at") or (now + fallback_seconds())
        return ("exhaust", until, f"{REASON_PREFIX} rolling {rp}% >= {thr:g}% (pre-emptive)")
    return ("clear", 0, "")


def _auto_reason(reason: str) -> bool:
    """Exhaustion we may lift on a healthy status (not manual / 401 auth)."""
    r = reason or ""
    return r.startswith(REASON_PREFIX) or r.startswith("HTTP ") or r.startswith("credits lookup")


def apply(pool, client, st: Optional[dict], now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    if st is not None:
        client.usage_status = st
    action, until, reason = decide(st, now)
    if action == "exhaust":
        cur = getattr(client, "exhausted_until", 0) or 0
        if cur < until - 5 or not str(getattr(client, "exhausted_reason", "")).startswith(REASON_PREFIX):
            pool.mark_exhausted(client, seconds=until - now, reason=reason)
        return "exhausted"
    if action == "clear":
        if (getattr(client, "exhausted_until", 0) or 0) > now and _auto_reason(getattr(client, "exhausted_reason", "")):
            pool.clear_exhausted(client)
            return "cleared"
        if str(getattr(client, "exhausted_reason", "")).startswith(REASON_PREFIX):
            client.exhausted_until, client.exhausted_reason = 0, ""  # expired: forget quietly
        return "ok"
    return "unknown"


def fetch_status(client) -> Optional[dict]:
    """One cheap read-only lookup (no model call)."""
    try:
        code, body = client.api_post(ENDPOINT, {"spaceId": client.space_id}, timeout=15)
    except Exception as exc:
        logger.warning("Usage status lookup failed", extra={"request_info": {
            "event": "usage_status_error", "account": getattr(client, "account_key", ""), "error": type(exc).__name__}})
        return None
    if code != 200:
        logger.warning("Usage status lookup failed", extra={"request_info": {
            "event": "usage_status_error", "account": getattr(client, "account_key", ""), "status": code}})
        return None
    return parse_status(body)


_lock = threading.Lock()
_last_check: dict = {}
_inflight: set = set()


def check_account(pool, client, source: str = "poll", fetch: Callable = fetch_status) -> str:
    key = getattr(client, "account_key", "") or id(client)
    with _lock:
        if key in _inflight:
            return "skipped"
        _inflight.add(key)
        _last_check[key] = time.time()
    try:
        st = fetch(client)
        res = apply(pool, client, st)
        if st:
            logger.info("Usage status", extra={"request_info": {
                "event": "usage_status", "account": getattr(client, "account_key", ""), "source": source,
                "status": st.get("status"), "rolling_pct": st.get("rolling_pct"), "monthly_pct": st.get("monthly_pct"),
                "limited_by": st.get("limited_by"), "resume_at": st.get("resume_at"),
                "rolling_reset_at": st.get("rolling_reset_at"), "action": res}})
        cb = getattr(pool, "on_usage_status", None)
        if st and cb:
            try:
                cb(client, st)
            except Exception:
                pass
        refresh_capabilities(pool, client, source)
        return res
    finally:
        with _lock:
            _inflight.discard(key)


def refresh_capabilities(pool, client, source: str = "poll", force: bool = False) -> Optional[dict]:
    """With NOTION_MULTI_WORKSPACE: re-read the workspace's getAvailableModels
    (restricted / GPT-only vs Claude-capable) every NOTION_CAPABILITY_REFRESH_SECS.
    Cheap metadata call, never a model call. Failures keep the previous capability."""
    from app import workspaces as ws
    if not ws.multi_enabled() or not hasattr(client, "api_post"):
        return None
    cur = getattr(client, "capabilities", None)
    if not force and cur and time.time() - float(cur.get("checked_at") or 0) < ws.refresh_secs():
        return cur
    try:
        code, body = client.api_post("getAvailableModels", {"spaceId": client.space_id}, timeout=15)
    except Exception as exc:
        logger.warning("Capability check failed", extra={"request_info": {
            "event": "capability_check_error", "account": getattr(client, "account_key", ""), "error": type(exc).__name__}})
        return cur
    cap = ws.parse_capabilities(body) if code == 200 else None
    if cap is None:
        logger.warning("Capability check failed", extra={"request_info": {
            "event": "capability_check_error", "account": getattr(client, "account_key", ""), "status": code}})
        return cur
    prev = ws.capability_label(cur)
    client.capabilities = cap
    new = ws.capability_label(cap)
    logger.info("Workspace capability", extra={"request_info": {
        "event": "workspace_capability" if prev == new else "workspace_capability_changed",
        "account": getattr(client, "account_key", ""), "source": source, "from": prev, "to": new,
        "restricted": cap.get("restricted"), "models": len(cap.get("models") or [])}})
    if not getattr(client, "workspace_name", ""):
        try:
            from app.admin.notion_login import candidates_from_spaces
            c2, sp = client.api_post("getSpaces", {}, timeout=15)
            for cand in candidates_from_spaces(sp) if c2 == 200 else []:
                if cand.get("space_id") == client.space_id and cand.get("space_name"):
                    client.workspace_name = cand["space_name"]
                    break
        except Exception:
            pass
    cb = getattr(pool, "on_capabilities", None)
    if cb:
        try:
            cb(client, cap)
        except Exception:
            pass
    return cap


def trigger_check(pool, client, source: str = "failure") -> bool:
    """Debounced async lookup after a failed request on `client`."""
    if os.getenv("USAGE_MONITOR", "1").strip().lower() in ("0", "false", "off"):
        return False
    key = getattr(client, "account_key", "") or id(client)
    now = time.time()
    with _lock:
        if key in _inflight or now - _last_check.get(key, 0) < trigger_debounce():
            return False
        _last_check[key] = now  # reserve
    threading.Thread(target=check_account, args=(pool, client, source), name="usage-check", daemon=True).start()
    return True


def startup_refresh(pool, timeout: Optional[float] = None) -> dict:
    """Synchronously refresh every account's usage once before the first pick, all in
    parallel, bounded by USAGE_STARTUP_REFRESH_TIMEOUT seconds (default 15). Accounts that
    fail or time out keep the previous behaviour (no_fresh_usage until the poller gets them)."""
    import concurrent.futures as _cf
    clients = list(getattr(pool, "clients", []) or [])
    timeout = _f("USAGE_STARTUP_REFRESH_TIMEOUT", 15.0) if timeout is None else timeout
    t0 = time.time()
    res = {"accounts": len(clients), "ok": 0, "failed": 0, "timed_out": 0}
    if not clients:
        return res
    ex = _cf.ThreadPoolExecutor(max_workers=min(8, len(clients)), thread_name_prefix="usage-startup")
    futs = {ex.submit(check_account, pool, c, "startup"): c for c in clients}
    done, pending = _cf.wait(futs, timeout=max(0.1, timeout))
    for f in done:
        try:
            ok = f.result() not in ("skipped",) and bool(getattr(futs[f], "usage_status", None))
        except Exception:
            ok = False
        res["ok" if ok else "failed"] += 1
    res["timed_out"] = len(pending)
    ex.shutdown(wait=False)
    res["elapsed_ms"] = int((time.time() - t0) * 1000)
    logger.info("Usage startup refresh", extra={"request_info": {"event": "usage_startup_refresh", **res}})
    return res


def _poll_loop(get_pool: Callable) -> None:
    time.sleep(_f("USAGE_POLL_INITIAL_DELAY", 20.0))
    while True:
        try:
            pool = get_pool()
            for c in list(getattr(pool, "clients", []) if pool else []):
                key = getattr(c, "account_key", "") or id(c)
                now = time.time()
                due = now - _last_check.get(key, 0) >= poll_seconds()
                # hot account (rolling >= USAGE_HOT_PCT): poll every USAGE_HOT_POLL_SECONDS so a
                # climb past the limit is seen in minutes, not one 10-min poll later (0 = off)
                hot_s = _f("USAGE_HOT_POLL_SECONDS", 120.0)
                rp = (getattr(c, "usage_status", None) or {}).get("rolling_pct")
                if (hot_s > 0 and isinstance(rp, (int, float)) and rp >= _f("USAGE_HOT_PCT", 80.0)
                        and now - _last_check.get(key, 0) >= hot_s):
                    due = True
                # re-check right after an exhausted account's resume time passes
                ex_until = getattr(c, "exhausted_until", 0) or 0
                if (0 < ex_until <= now and str(getattr(c, "exhausted_reason", "")).startswith(REASON_PREFIX)
                        and now - _last_check.get(key, 0) >= 30):
                    due = True
                if due:
                    check_account(pool, c, "poll")
                    time.sleep(1.0)
        except Exception as exc:
            logger.warning("Usage poll error", extra={"request_info": {"event": "usage_poll_error", "error": type(exc).__name__}})
        time.sleep(30)


def start(get_pool: Callable) -> None:
    if os.getenv("USAGE_MONITOR", "1").strip().lower() in ("0", "false", "off"):
        return
    threading.Thread(target=_poll_loop, args=(get_pool,), name="usage-poller", daemon=True).start()
