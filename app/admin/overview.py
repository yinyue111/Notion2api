"""Admin overview: time-range usage stats + official list-price value (UTC+8 by default)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import pricing

PRESETS = ("today", "yesterday", "7d", "30d", "month", "all")


def resolve_range(preset: str = "", start: Optional[float] = None, end: Optional[float] = None,
                  tz_hours: int = 8, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    tz = timezone(timedelta(hours=tz_hours))
    local = datetime.fromtimestamp(now, tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if start is not None or end is not None:
        s = float(start or 0)
        e = float(end) if end is not None else now
        preset = "custom"
    else:
        preset = preset if preset in PRESETS else "today"
        if preset == "today":
            s, e = midnight.timestamp(), now
        elif preset == "yesterday":
            s, e = (midnight - timedelta(days=1)).timestamp(), midnight.timestamp()
        elif preset == "7d":
            s, e = now - 7 * 86400, now
        elif preset == "30d":
            s, e = now - 30 * 86400, now
        elif preset == "month":
            s, e = midnight.replace(day=1).timestamp(), now
        else:
            s, e = 0.0, now
    if e <= s:
        raise ValueError("结束时间必须晚于开始时间")
    return {"preset": preset, "start": s, "end": e, "tz_hours": tz_hours}


def _bucket_label(ts: float, hourly: bool, tz: timezone) -> str:
    d = datetime.fromtimestamp(ts, tz)
    return d.strftime("%m-%d %H:00") if hourly else d.strftime("%Y-%m-%d")


def _new_agg() -> dict:
    return {"requests": 0, "errors": 0, "usage_requests": 0, "prompt_tokens": 0, "uncached_tokens": 0,
            "cached_tokens": 0, "cache_write_tokens": 0, "completion_tokens": 0, "_lat_sum": 0, "_lat_n": 0,
            "value_usd": 0.0, "input_usd": 0.0, "cache_read_usd": 0.0, "cache_write_usd": 0.0, "output_usd": 0.0,
            "priced_requests": 0, "unpriced_requests": 0, "unpriced_tokens": 0, "long_context_requests": 0}


def _add(a: dict, r: dict, toks: tuple, val: Optional[dict]) -> None:
    unc, cr, cw, out = toks
    a["requests"] += 1
    a["errors"] += r["is_err"]
    if r["lat"] is not None:
        a["_lat_sum"] += r["lat"]
        a["_lat_n"] += 1
    if not r["has_usage"]:
        return
    a["usage_requests"] += 1
    a["prompt_tokens"] += unc + cr + cw
    a["uncached_tokens"] += unc
    a["cached_tokens"] += cr
    a["cache_write_tokens"] += cw
    a["completion_tokens"] += out
    if val is None:
        a["unpriced_requests"] += 1
        a["unpriced_tokens"] += unc + cr + cw + out
        return
    a["priced_requests"] += 1
    a["long_context_requests"] += 1 if val["long"] else 0
    for k in ("input_usd", "cache_read_usd", "cache_write_usd", "output_usd"):
        a[k] += val[k]
    a["value_usd"] += val["usd"]


def _finish(a: dict) -> dict:
    n = a.pop("_lat_n")
    s = a.pop("_lat_sum")
    a["avg_latency_ms"] = int(s / n) if n else None
    p = a["prompt_tokens"]
    a["cache_hit_rate"] = round(a["cached_tokens"] / p, 4) if p else None
    for k in ("value_usd", "input_usd", "cache_read_usd", "cache_write_usd", "output_usd"):
        a[k] = round(a[k], 6)
    return a


def _tokens(row) -> tuple:
    prompt = int(row["prompt_tokens"] or 0)
    cr = int(row["cached_tokens"] or 0)
    cw = int(row["cache_write_tokens"] or 0)
    unc = row["uncached_input_tokens"]
    unc = int(unc) if unc is not None else max(0, prompt - cr - cw)
    return unc, cr, cw, int(row["completion_tokens"] or 0)


def compute(rows, rng: dict) -> dict:
    tz = timezone(timedelta(hours=rng.get("tz_hours", 8)))
    hourly = (rng["end"] - rng["start"]) <= 2 * 86400 + 1
    total = _new_agg()
    by_time: dict = {}
    by_model: dict = {}
    by_account: dict = {}
    by_key: dict = {}
    err_codes: dict = {}
    price_keys: dict = {}
    for row in rows:
        status = row["status"] or 0
        code = row["error_code"]
        r = {"is_err": 1 if (status >= 400 or code) else 0, "lat": row["latency_ms"],
             "has_usage": bool(row["usage_reported"]) or bool(row["prompt_tokens"] or row["completion_tokens"])}
        model = row["model"] or "?"
        if model not in price_keys:
            price_keys[model] = pricing.lookup(row["model"])
        pkey, entry = price_keys[model]
        toks = _tokens(row)
        val = pricing.cost(entry, *toks, ts=row["ts"]) if r["has_usage"] else None
        _add(total, r, toks, val)
        for d, k in ((by_time, _bucket_label(row["ts"], hourly, tz)), (by_model, model),
                     (by_account, row["account"] or "?"), (by_key, row["auth"] or "?")):
            if k not in d:
                d[k] = _new_agg()
            _add(d[k], r, toks, val)
        if r["is_err"]:
            c = code or f"HTTP {status}"
            err_codes[c] = err_codes.get(c, 0) + 1

    def rows_of(d: dict, label: str, sort_time: bool = False) -> list:
        items = [{label: k, **_finish(v)} for k, v in d.items()]
        if sort_time:
            items.sort(key=lambda x: x[label])
        else:
            items.sort(key=lambda x: (-x["value_usd"], -x["requests"]))
        return items

    models = rows_of(by_model, "model")
    for m in models:
        pkey, entry = price_keys.get(m["model"], (None, None))
        m["price_key"] = pkey
        m["priced"] = bool(entry and not entry.get("unpriced"))
        if m["priced"]:
            m["price"] = {f: entry[f] for f in pricing.PRICE_FIELDS}
            m["price_name"] = entry.get("name")
            m["long_context"] = entry.get("long_context")
            m["offpeak"] = bool(entry.get("offpeak"))
    return {
        "range": {**rng, "bucket": "hour" if hourly else "day"},
        "totals": _finish(total),
        "by_time": rows_of(by_time, "bucket", sort_time=True),
        "by_model": models,
        "by_account": rows_of(by_account, "account"),
        "by_key": rows_of(by_key, "auth"),
        "errors_by_code": sorted(({"error_code": k, "count": v} for k, v in err_codes.items()), key=lambda x: -x["count"]),
    }
