"""SQLite (WAL) storage: accounts (encrypted token_v2), sessions, request log, stats."""
import json
import os
import queue
import sqlite3
import threading
import time
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

from app.logger import logger

LOG_RETENTION_DAYS = int(os.getenv("REQUEST_LOG_RETENTION_DAYS", "30"))
ACCOUNT_FIELDS = ("space_id", "user_id", "space_view_id", "user_name", "user_email")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT, token_enc TEXT NOT NULL, token_last4 TEXT,
  space_id TEXT NOT NULL, user_id TEXT NOT NULL, space_view_id TEXT, user_name TEXT, user_email TEXT,
  extra_enc TEXT, enabled INTEGER NOT NULL DEFAULT 1,
  created_at REAL, updated_at REAL,
  last_test_at REAL, last_test_ok INTEGER, last_test_msg TEXT,
  last_credits_at REAL, credits_json TEXT,
  UNIQUE(space_id, user_id)
);
CREATE TABLE IF NOT EXISTS sessions (
  id_hash TEXT PRIMARY KEY, username TEXT, csrf TEXT, ip TEXT,
  created_at REAL, expires_at REAL
);
CREATE TABLE IF NOT EXISTS request_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, ip TEXT, method TEXT, path TEXT, model TEXT, stream INTEGER,
  status INTEGER, latency_ms INTEGER, ttfb_ms INTEGER, account TEXT, auth TEXT,
  prompt_tokens INTEGER, cached_tokens INTEGER, completion_tokens INTEGER, usage_reported INTEGER,
  error_code TEXT, error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_rl_ts ON request_log(ts);
"""


class AdminStore:
    def __init__(self, path: str, enc_key: str):
        self.path = path
        self.fernet = Fernet(enc_key.encode() if isinstance(enc_key, str) else enc_key)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._local = threading.local()
        c = self._conn()
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        cols = [r[1] for r in c.execute("PRAGMA table_info(request_log)")]
        if "thread_reuse" not in cols:
            c.execute("ALTER TABLE request_log ADD COLUMN thread_reuse TEXT")
        for col, typ in self._EXTRA_COLS:
            if col not in cols:
                c.execute(f"ALTER TABLE request_log ADD COLUMN {col} {typ}")
        c.commit()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._q: "queue.Queue[dict]" = queue.Queue(maxsize=10000)
        threading.Thread(target=self._writer, name="request-log-writer", daemon=True).start()

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=5000")
            self._local.c = c
        return c

    # ---------- crypto ----------
    def enc(self, s: str) -> str:
        return self.fernet.encrypt(s.encode()).decode()

    def dec(self, s: str) -> str:
        return self.fernet.decrypt(s.encode()).decode()

    # ---------- meta ----------
    def meta_get(self, k: str) -> Optional[str]:
        r = self._conn().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def meta_set(self, k: str, v: str) -> None:
        c = self._conn()
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        c.commit()

    # ---------- accounts ----------
    def import_env_accounts(self, accounts: list[dict]) -> int:
        if self.meta_get("env_accounts_imported"):
            return 0
        n = 0
        for acc in accounts or []:
            try:
                self.add_account(acc, label=acc.get("label") or acc.get("user_email") or "")
                n += 1
            except sqlite3.IntegrityError:
                pass
        self.meta_set("env_accounts_imported", str(int(time.time())))
        return n

    def add_account(self, acc: dict, label: str = "") -> int:
        token = str(acc.get("token_v2") or "").strip()
        if not token or not acc.get("space_id") or not acc.get("user_id"):
            raise ValueError("token_v2, space_id and user_id are required")
        extra = {k: v for k, v in acc.items() if k not in ACCOUNT_FIELDS + ("token_v2", "label")}
        now = time.time()
        c = self._conn()
        cur = c.execute(
            "INSERT INTO accounts(label,token_enc,token_last4,space_id,user_id,space_view_id,user_name,user_email,"
            "extra_enc,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
            (label, self.enc(token), token[-4:], str(acc["space_id"]), str(acc["user_id"]),
             acc.get("space_view_id") or "", acc.get("user_name") or "", acc.get("user_email") or "",
             self.enc(json.dumps(extra)) if extra else None, now, now),
        )
        c.commit()
        return int(cur.lastrowid)

    def update_account(self, acc_id: int, *, label=None, enabled=None, token_v2=None, user_email=None) -> bool:
        sets, args = [], []
        if label is not None:
            sets.append("label=?"); args.append(str(label)[:200])
        if enabled is not None:
            sets.append("enabled=?"); args.append(1 if enabled else 0)
        if user_email is not None:
            sets.append("user_email=?"); args.append(str(user_email)[:200])
        if token_v2:
            t = str(token_v2).strip()
            sets += ["token_enc=?", "token_last4=?"]; args += [self.enc(t), t[-4:]]
        if not sets:
            return False
        sets.append("updated_at=?"); args.append(time.time()); args.append(acc_id)
        c = self._conn()
        cur = c.execute(f"UPDATE accounts SET {','.join(sets)} WHERE id=?", args)
        c.commit()
        return cur.rowcount > 0

    def delete_account(self, acc_id: int) -> bool:
        c = self._conn()
        cur = c.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
        c.commit()
        return cur.rowcount > 0

    def list_accounts(self) -> list[dict]:
        rows = self._conn().execute("SELECT * FROM accounts ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = {k: r[k] for k in r.keys() if k not in ("token_enc", "extra_enc", "credits_json")}
            d["token_hint"] = "…" + (r["token_last4"] or "")
            d["enabled"] = bool(r["enabled"])
            d["credits"] = json.loads(r["credits_json"]) if r["credits_json"] else None
            out.append(d)
        return out

    def get_account_config(self, acc_id: int) -> Optional[dict]:
        r = self._conn().execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
        return self._row_to_config(r) if r else None

    def _row_to_config(self, r) -> Optional[dict]:
        try:
            cfg = json.loads(self.dec(r["extra_enc"])) if r["extra_enc"] else {}
            cfg["token_v2"] = self.dec(r["token_enc"])
        except (InvalidToken, ValueError):
            logger.error("Cannot decrypt account (wrong TOKEN_ENC_KEY?)",
                         extra={"request_info": {"event": "account_decrypt_failed", "account_id": r["id"]}})
            return None
        for k in ACCOUNT_FIELDS:
            cfg[k] = r[k] or ""
        cfg["_admin_id"] = r["id"]
        cfg["_label"] = r["label"] or ""
        return cfg

    def find_account_id(self, space_id: str, user_id: str) -> Optional[int]:
        r = self._conn().execute("SELECT id FROM accounts WHERE space_id=? AND user_id=?",
                                 (str(space_id), str(user_id))).fetchone()
        return r["id"] if r else None

    def account_cost_since(self, account: str, since: Optional[float]) -> float:
        """$ estimate of logged token usage for one account label since `since`."""
        from app import quota_estimate as qe
        if not account or since is None:
            return 0.0
        r = self._conn().execute(
            "SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(cached_tokens),0) c, "
            "COALESCE(SUM(completion_tokens),0) o FROM request_log WHERE account=? AND ts>=?",
            (account, float(since))).fetchone()
        return qe.cost_usd(r["p"], r["c"], r["o"])

    def account_first_log_ts(self, account: str) -> Optional[float]:
        if not account:
            return None
        r = self._conn().execute("SELECT MIN(ts) t FROM request_log WHERE account=?", (account,)).fetchone()
        return r["t"] if r else None

    def enabled_account_configs(self) -> list[dict]:
        rows = self._conn().execute("SELECT * FROM accounts WHERE enabled=1 ORDER BY id").fetchall()
        return [c for c in (self._row_to_config(r) for r in rows) if c]

    def set_test_result(self, acc_id: int, ok: bool, msg: str) -> None:
        c = self._conn()
        c.execute("UPDATE accounts SET last_test_at=?, last_test_ok=?, last_test_msg=? WHERE id=?",
                  (time.time(), 1 if ok else 0, msg[:300], acc_id))
        c.commit()

    def set_credits(self, acc_id: int, credits: dict) -> None:
        c = self._conn()
        c.execute("UPDATE accounts SET last_credits_at=?, credits_json=? WHERE id=?",
                  (time.time(), json.dumps(credits)[:20000], acc_id))
        c.commit()

    # ---------- sessions ----------
    def create_session(self, id_hash: str, username: str, csrf: str, ip: str, ttl: int) -> None:
        now = time.time()
        c = self._conn()
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        c.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (id_hash, username, csrf, ip, now, now + ttl))
        c.commit()

    def get_session(self, id_hash: str) -> Optional[dict]:
        r = self._conn().execute("SELECT * FROM sessions WHERE id_hash=? AND expires_at>?",
                                 (id_hash, time.time())).fetchone()
        return dict(r) if r else None

    def delete_session(self, id_hash: str) -> None:
        c = self._conn()
        c.execute("DELETE FROM sessions WHERE id_hash=?", (id_hash,))
        c.commit()

    # ---------- request log (async writer) ----------
    def enqueue_log(self, rec: dict) -> None:
        try:
            self._q.put_nowait(rec)
        except queue.Full:
            pass

    _LOG_COLS = ("ts", "ip", "method", "path", "model", "stream", "status", "latency_ms", "ttfb_ms", "account",
                 "auth", "prompt_tokens", "cached_tokens", "completion_tokens", "usage_reported",
                 "error_code", "error_message", "thread_reuse")
    # effort / cap telemetry (nullable, added by ALTER on startup)
    _EXTRA_COLS = (("effort_req", "TEXT"), ("effort_final", "TEXT"), ("client_max_tokens", "INTEGER"),
                   ("has_tools", "INTEGER"), ("n_tools", "INTEGER"), ("ask_chars", "INTEGER"),
                   ("long_gen_match", "INTEGER"), ("conv_key", "TEXT"), ("silent_close", "INTEGER"),
                   ("silent_close_secs", "INTEGER"), ("hedge_winner", "TEXT"), ("hedge_effort", "TEXT"),
                   ("notion_output_tokens", "INTEGER"), ("cap_memory", "TEXT"), ("would_downgrade", "INTEGER"),
                   ("cache_write_tokens", "INTEGER"), ("uncached_input_tokens", "INTEGER"),
                   ("prev_prompt_tokens", "INTEGER"), ("prompt_shrink", "INTEGER"))
    _LOG_COLS = _LOG_COLS + tuple(c for c, _ in _EXTRA_COLS)

    def _writer(self) -> None:
        last_purge = 0.0
        while True:
            try:
                rec = self._q.get(timeout=60)
                batch = [rec]
                while len(batch) < 200:
                    try:
                        batch.append(self._q.get_nowait())
                    except queue.Empty:
                        break
                c = self._conn()
                c.executemany(
                    f"INSERT INTO request_log({','.join(self._LOG_COLS)}) VALUES({','.join('?' * len(self._LOG_COLS))})",
                    [tuple(b.get(k) for k in self._LOG_COLS) for b in batch],
                )
                c.commit()
            except queue.Empty:
                pass
            except Exception:
                logger.error("request log write failed", exc_info=True,
                             extra={"request_info": {"event": "request_log_write_failed"}})
            if time.time() - last_purge > 3600:
                last_purge = time.time()
                try:
                    self.purge_old_logs()
                except Exception:
                    pass

    def purge_old_logs(self) -> int:
        c = self._conn()
        cur = c.execute("DELETE FROM request_log WHERE ts < ?", (time.time() - LOG_RETENTION_DAYS * 86400,))
        c.commit()
        return cur.rowcount

    def list_requests(self, limit: int = 50, offset: int = 0, model: str = "", account: str = "",
                      status: str = "") -> dict:
        where, args = ["1=1"], []
        if model:
            where.append("model=?"); args.append(model)
        if account:
            where.append("account=?"); args.append(account)
        if status == "error":
            where.append("(status>=400 OR error_code IS NOT NULL)")
        elif status == "ok":
            where.append("status<400 AND error_code IS NULL")
        w = " AND ".join(where)
        c = self._conn()
        total = c.execute(f"SELECT COUNT(*) n FROM request_log WHERE {w}", args).fetchone()["n"]
        rows = c.execute(f"SELECT * FROM request_log WHERE {w} ORDER BY id DESC LIMIT ? OFFSET ?",
                         args + [limit, offset]).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}

    _AGG = ("COUNT(*) requests, SUM(CASE WHEN status>=400 OR error_code IS NOT NULL THEN 1 ELSE 0 END) errors, "
            "COALESCE(SUM(prompt_tokens),0) prompt_tokens, COALESCE(SUM(cached_tokens),0) cached_tokens, "
            "COALESCE(SUM(completion_tokens),0) completion_tokens, CAST(AVG(latency_ms) AS INTEGER) avg_latency_ms")

    @staticmethod
    def _rate(d: dict) -> dict:
        p = d.get("prompt_tokens") or 0
        d["cache_hit_rate"] = round((d.get("cached_tokens") or 0) / p, 4) if p else None
        return d

    def stats(self, days: int = 7, tz_offset_hours: int = 8) -> dict:
        since = time.time() - days * 86400
        off = f"{int(tz_offset_hours):+d} hours"
        c = self._conn()
        q = lambda sel, grp: [self._rate(dict(r)) for r in c.execute(
            f"SELECT {sel}, {self._AGG} FROM request_log WHERE ts>=? GROUP BY {grp} ORDER BY {grp}", (since,)).fetchall()]
        totals = self._rate(dict(c.execute(f"SELECT {self._AGG} FROM request_log WHERE ts>=?", (since,)).fetchone()))
        return {
            "days": days,
            "totals": totals,
            "by_day": q(f"date(ts,'unixepoch','{off}') day", "day"),
            "by_model": q("COALESCE(model,'?') model", "model"),
            "by_account": q("COALESCE(account,'?') account", "account"),
        }
