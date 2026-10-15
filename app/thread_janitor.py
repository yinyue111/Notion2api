"""Archive the Notion AI threads this service creates once they can no longer be reused.

Every API request creates a thread in the account's Notion workspace; nothing removed
them, so the workspace's AI chat history grew without bound. Thread ids we create are
recorded (sqlite table notion_threads in admin.db, ids + account key + time only); a
background worker sets alive=False (Notion's own "delete chat", soft) on threads older
than NOTION_THREAD_CLEANUP_AGE (default 48h, always > THREAD_REUSE_TTL) that the reuse
index no longer references. Only threads recorded by this service are touched.
NOTION_THREAD_CLEANUP=0 disables both recording and cleanup.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from app.logger import logger

_lock = threading.Lock()
_started = False


def enabled() -> bool:
    return os.getenv("NOTION_THREAD_CLEANUP", "1").strip().lower() not in ("0", "false", "no", "off")


def _db() -> str:
    try:
        from app.config import ADMIN_DB_PATH
        p = ADMIN_DB_PATH
    except Exception:
        return ""
    return p if p and os.path.isdir(os.path.dirname(p) or ".") else ""


def _conn():
    c = sqlite3.connect(_db(), timeout=5)
    c.execute("CREATE TABLE IF NOT EXISTS notion_threads (thread_id TEXT PRIMARY KEY, account TEXT, ts REAL)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_nt_ts ON notion_threads(ts)")
    return c


def record(thread_id: str, account: str) -> None:
    if not (enabled() and thread_id and _db()):
        return
    try:
        with _lock:
            c = _conn()
            with c:
                c.execute("INSERT OR IGNORE INTO notion_threads(thread_id, account, ts) VALUES(?,?,?)",
                          (thread_id, account, time.time()))
            c.close()
    except Exception:
        pass


def _age() -> float:
    from app import thread_reuse as _tr
    age = float(os.getenv("NOTION_THREAD_CLEANUP_AGE", str(48 * 3600)) or 48 * 3600)
    return max(age, _tr._ttl() + 3600)


def run_once(pool, limit: int | None = None) -> dict:
    """Archive due threads. Returns counts."""
    out = {"archived": 0, "failed": 0, "skipped": 0}
    if not (enabled() and _db() and pool is not None):
        return out
    limit = limit or int(os.getenv("NOTION_THREAD_CLEANUP_BATCH", "100") or 100)
    cutoff = time.time() - _age()
    with _lock:
        c = _conn()
        try:
            live = {r[0] for r in c.execute("SELECT thread_id FROM thread_reuse")}
        except sqlite3.Error:
            live = set()
        rows = c.execute("SELECT thread_id, account FROM notion_threads WHERE ts < ? ORDER BY ts LIMIT ?",
                         (cutoff, limit)).fetchall()
        c.close()
    clients = {getattr(cl, "account_key", ""): cl for cl in list(getattr(pool, "clients", []) or [])}
    for tid, acc in rows:
        cl = clients.get(acc)
        if tid in live or cl is None:
            out["skipped"] += 1
            if cl is None:  # account removed: forget the row
                _forget(tid)
            continue
        ok = False
        try:
            ok = bool(cl.delete_thread(tid))
        except Exception:
            ok = False
        if ok:
            out["archived"] += 1
            _forget(tid)
        else:
            out["failed"] += 1
        time.sleep(float(os.getenv("NOTION_THREAD_CLEANUP_PACE", "1.5") or 1.5))
    if any(out.values()):
        logger.info("Notion thread cleanup", extra={"request_info": {"event": "thread_cleanup", **out}})
    return out


def _forget(tid: str) -> None:
    try:
        with _lock:
            c = _conn()
            with c:
                c.execute("DELETE FROM notion_threads WHERE thread_id=?", (tid,))
            c.close()
    except Exception:
        pass


def start(get_pool) -> None:
    global _started
    if _started or not enabled() or not _db():
        return
    _started = True
    every = float(os.getenv("NOTION_THREAD_CLEANUP_INTERVAL", "1800") or 1800)

    def loop():
        time.sleep(120)
        while True:
            try:
                run_once(get_pool())
            except Exception as exc:
                logger.warning(f"thread cleanup failed: {exc}")
            time.sleep(every)

    threading.Thread(target=loop, name="thread-janitor", daemon=True).start()
