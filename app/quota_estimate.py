"""Quota estimate for the admin panel (sub2api-style usage bars + $ estimate).

Inputs per account:
  * the cached getCreditRateLimitStatus snapshot (rolling 6h % and billing-period %),
  * our own request_log token counts.
Token counts are priced at the official Claude Opus API list rates (env-overridable):
  input $5/M, cache read $0.50/M, output $25/M (uncached input = prompt - cached).

Per-account capacity ($ for 100% of a window) = logged $ inside the window / (window % / 100)
when spend > 0 and used% >= MIN_PCT. Coverage of the full window is recorded as source quality
(account vs account_partial) but does NOT fall back to the pool median — accounts without
enough data show null (「不足」 in the UI). Pool median is only a summary card over accounts
that have their own estimate. Pool totals sum estimable accounts only.
Everything here is an *estimate*: Notion meters credits, not tokens.
"""
from __future__ import annotations

import calendar
import os
import statistics
import time
from datetime import datetime, timezone
from typing import Optional

MIN_PCT = 5.0
ROLLING_SECONDS = 6 * 3600


def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def rates() -> dict:
    return {"input": _envf("QUOTA_RATE_INPUT", 5.0), "cache_read": _envf("QUOTA_RATE_CACHE_READ", 0.5),
            "output": _envf("QUOTA_RATE_OUTPUT", 25.0)}


def cost_usd(prompt: float, cached: float, completion: float, r: Optional[dict] = None) -> float:
    r = r or rates()
    prompt, cached, completion = float(prompt or 0), float(cached or 0), float(completion or 0)
    cached = min(cached, prompt)
    return ((prompt - cached) * r["input"] + cached * r["cache_read"] + completion * r["output"]) / 1e6


def period_start(reset_at: int) -> int:
    """Billing period start = same instant one calendar month before the reset."""
    d = datetime.fromtimestamp(reset_at, tz=timezone.utc)
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    day = min(d.day, calendar.monthrange(y, m)[1])
    return int(d.replace(year=y, month=m, day=day).timestamp())


def _capacity(cost: float, pct: Optional[float]) -> Optional[float]:
    """Invert capacity from this account's own spend and used%. No pool fallback."""
    if pct is None or pct < MIN_PCT or cost <= 0:
        return None
    return cost / (pct / 100.0)


def _covered_month(ps: Optional[int], first: Optional[float]) -> bool:
    return bool(ps and first and first <= ps + 3600)


def _covered_rolling(ws: Optional[int], first: Optional[float]) -> bool:
    return bool(ws and first and first <= ws)


def estimate(accounts: list[dict], cost_since, first_log_ts, now: Optional[float] = None) -> dict:
    """accounts: [{id,label,rate_limit}], cost_since(label, ts)->$ , first_log_ts(label)->ts|None."""
    now = time.time() if now is None else now
    rows = []
    for a in accounts:
        rl = a.get("rate_limit") or {}
        label = a.get("label") or ""
        first = first_log_ts(label)
        row = {"id": a.get("id"), "label": label, "status": rl.get("status"), "checked_at": rl.get("checked_at"),
               "rolling_pct": rl.get("rolling_pct"), "monthly_pct": rl.get("monthly_pct"),
               "rolling_reset_at": rl.get("rolling_reset_at"), "monthly_reset_at": rl.get("monthly_reset_at"),
               "limited_by": rl.get("limited_by"), "resume_at": rl.get("resume_at")}
        mr = rl.get("monthly_reset_at")
        ps = period_start(int(mr)) if mr else None
        row["period_start"] = ps
        row["month_cost"] = round(cost_since(label, ps), 4) if ps else None
        m_cov = _covered_month(ps, first)
        row["_m_cap"] = _capacity(row["month_cost"] or 0, row["monthly_pct"])
        row["_m_cov"] = m_cov
        rr = rl.get("rolling_reset_at")
        ws = (int(rr) - ROLLING_SECONDS) if rr else None
        row["rolling_cost"] = round(cost_since(label, ws), 4) if ws else None
        r_cov = _covered_rolling(ws, first)
        row["_r_cap"] = _capacity(row["rolling_cost"] or 0, row["rolling_pct"])
        row["_r_cov"] = r_cov
        rows.append(row)

    m_vals = [r["_m_cap"] for r in rows if r["_m_cap"]]
    r_vals = [r["_r_cap"] for r in rows if r["_r_cap"]]
    m_med = statistics.median(m_vals) if m_vals else None
    r_med = statistics.median(r_vals) if r_vals else None
    # Basis for the median card only (not applied to rows).
    if m_vals:
        m_src = "measured"
    else:
        v = _envf("NOTION_MONTHLY_USD_EST", 0.0)
        m_med = v if v > 0 else None
        m_src = "configured" if m_med else "unknown"
    if r_vals:
        r_src = "measured"
    else:
        v = _envf("NOTION_ROLLING_USD_EST", 0.0)
        r_med = v if v > 0 else None
        r_src = "configured" if r_med else "unknown"

    tot = {"month_capacity": 0.0, "month_remaining": 0.0, "rolling_remaining": 0.0, "known": 0}
    for r in rows:
        mcap = r.pop("_m_cap")
        m_cov = r.pop("_m_cov")
        rcap = r.pop("_r_cap")
        r_cov = r.pop("_r_cov")
        if mcap:
            msrc = "account" if m_cov else "account_partial"
        else:
            msrc = "insufficient"
        if rcap:
            rsrc = "account" if r_cov else "account_partial"
        else:
            rsrc = "insufficient"
        r["month_capacity_usd"] = round(mcap, 2) if mcap else None
        r["month_capacity_source"] = msrc
        r["rolling_capacity_usd"] = round(rcap, 2) if rcap else None
        r["rolling_capacity_source"] = rsrc
        mp, rp = r["monthly_pct"], r["rolling_pct"]
        r["month_remaining_usd"] = round(max(0.0, mcap * (1 - mp / 100.0)), 2) if mcap and mp is not None else None
        r["rolling_remaining_usd"] = round(max(0.0, rcap * (1 - rp / 100.0)), 2) if rcap and rp is not None else None
        if r["status"] == "rate_limited" and r["limited_by"] != "billing_period":
            r["rolling_remaining_usd"] = 0.0
        if r["month_remaining_usd"] is not None:
            tot["known"] += 1
            tot["month_capacity"] += mcap
            tot["month_remaining"] += r["month_remaining_usd"]
            tot["rolling_remaining"] += r["rolling_remaining_usd"] or 0.0
    pool = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in tot.items()}
    pool["accounts"] = len(rows)
    pool["month_pct"] = round(100 * (1 - tot["month_remaining"] / tot["month_capacity"]), 2) if tot["month_capacity"] else None
    pool["month_capacity_median"] = round(m_med, 2) if m_med else None
    pool["rolling_capacity_median"] = round(r_med, 2) if r_med else None
    pool["month_capacity_basis"], pool["rolling_capacity_basis"] = m_src, r_src
    return {"items": rows, "pool": pool, "rates": rates(), "generated_at": int(now)}
