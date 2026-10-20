"""Resend dedupe (NOTION_RESEND_DEDUPE=on|off, default on).

Some callers give up on a slow request (~85-90 s with xhigh) and send the very same request
again while our first copy is still running (2026-10-20 c:2300: 5 identical xhigh copies
84-92 s apart, each run to completion, hedged and billed). When a request arrives whose
caller identity + model + tool prompt + full message list hash equals that of a copy that
has been running for >= NOTION_RESEND_DEDUPE_MIN_AGE s (default 20), the older copy is
superseded: flagged cancelled, its Notion inference interrupted and its hedges stopped.

Legitimate parallel work is not touched: subagents / forks differ in their messages (any
difference -> different hash); identical copies sent at (almost) the same moment (fan-out)
are younger than MIN_AGE; requests with fewer than NOTION_RESEND_DEDUPE_MIN_MSGS messages
(default 3: short one-shot prompts that different end users behind one relay key could
share) are never deduped. In-memory, per process.
"""
import hashlib
import json
import os
import threading
import time

from app.logger import logger

_lock = threading.Lock()
_live: dict = {}  # fingerprint -> [holder]
_MAX_AGE = 1200.0  # leak guard: a holder never released is forgotten after this


def _on(name, default):
    return os.getenv(name, default).strip().lower() not in ("0", "false", "off", "no", "")


def enabled() -> bool:
    return _on("NOTION_RESEND_DEDUPE", "on")


def min_age() -> float:
    try:
        return float(os.getenv("NOTION_RESEND_DEDUPE_MIN_AGE", "20") or 20)
    except ValueError:
        return 20.0


def min_msgs() -> int:
    try:
        return int(os.getenv("NOTION_RESEND_DEDUPE_MIN_MSGS", "3") or 3)
    except ValueError:
        return 3


def fingerprint(ident: str, model: str, tool_prompt: str, messages) -> str | None:
    if not enabled() or not ident or not isinstance(messages, list) or len(messages) < min_msgs():
        return None
    h = hashlib.sha256()
    h.update(f"{ident}\x00{model}\x00".encode())
    h.update(hashlib.sha256((tool_prompt or "").encode()).digest())
    h.update(json.dumps(messages, sort_keys=True, ensure_ascii=False, default=str).encode())
    return h.hexdigest()


def begin(fp: str | None, rec=None, conv_key: str = "", abort=None, now: float | None = None):
    """Register this request; supersede older running copies of the same fingerprint.
    -> (holder or None, [superseded holders])."""
    if not fp:
        return None, []
    now = time.time() if now is None else now
    holder = {"fp": fp, "ts": now, "metas": [], "rec": rec, "superseded": False, "done": False,
              "conv_key": conv_key}
    victims = []
    with _lock:
        for k in [k for k, hs in _live.items() if all(now - h["ts"] > _MAX_AGE for h in hs)]:
            _live.pop(k, None)
        olds = _live.setdefault(fp, [])
        for h in list(olds):
            if h["done"] or h["superseded"] or now - h["ts"] > _MAX_AGE:
                olds.remove(h)
                continue
            if now - h["ts"] >= min_age():
                h["superseded"] = True
                olds.remove(h)
                victims.append(h)
        olds.append(holder)
    for h in victims:
        _supersede(h, now, abort)
    return holder, victims


def _supersede(h, now, abort):
    threads = []
    for m in list(h["metas"]):
        if isinstance(m, dict):
            threads.append(str(m.get("thread_id") or "")[:36])
            m["_superseded"] = True
            if abort is not None:
                try:
                    abort(m)
                except Exception:
                    pass
            else:
                m["_cancelled"] = True
    rec = h.get("rec")
    if isinstance(rec, dict):
        rec["error_code"] = "superseded_duplicate"
        rec["error_message"] = "identical request resent by the caller; this older copy was stopped"
    logger.info("Older identical request superseded by a resend", extra={"request_info": {
        "event": "superseded_duplicate", "age_s": round(now - h["ts"], 1), "conv_key": h.get("conv_key"),
        "threads": [t for t in threads if t], "attempts": len(h["metas"])}})


def attach(holder, meta) -> None:
    """Track an attempt's meta (abort target). Already superseded: cancel it at once."""
    if holder is None or not isinstance(meta, dict):
        return
    with _lock:
        holder["metas"].append(meta)
        sup = holder["superseded"]
    if sup:
        meta["_cancelled"] = True
        meta["_superseded"] = True


def release(holder) -> None:
    if holder is None:
        return
    with _lock:
        holder["done"] = True
        hs = _live.get(holder["fp"])
        if hs and holder in hs:
            hs.remove(holder)
            if not hs:
                _live.pop(holder["fp"], None)


def live_count() -> int:
    with _lock:
        return sum(len(v) for v in _live.values())


def clear() -> None:
    with _lock:
        _live.clear()
