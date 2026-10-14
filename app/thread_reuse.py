"""Notion thread continuation (prompt-cache reuse), behind ENABLE_THREAD_REUSE=1.

Each request normally creates a fresh Notion thread with the whole history, so Notion's
prompt cache only ever hits the static system prefix. When a request's history starts
with a history we already answered (incl. the assistant reply we returned), the new
messages are appended to that same thread on the same account (createThread=False,
isPartialTranscript=True) and the cache grows with the conversation.

Matching: a chained sha256 over canonicalised messages (whitespace-collapsed text, our
zero-width URL defang removed, tool calls as name + sorted-JSON args, call ids ignored),
seeded with a namespace (model + tool prompt). An entry is consumed when used, so a
regenerate/edit or a concurrent request with the same history misses -> new thread.
In-memory, bounded, TTL. Never stores message text, only hashes + thread ids.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Any, Optional


def enabled() -> bool:
    return os.getenv("ENABLE_THREAD_REUSE", "0").strip().lower() in ("1", "true", "yes", "on")


def _ttl() -> float:
    return float(os.getenv("THREAD_REUSE_TTL", "7200") or 7200)


def _max() -> int:
    return int(os.getenv("THREAD_REUSE_MAX", "2000") or 2000)


def _norm_text(v: Any) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        v = json.dumps(v, sort_keys=True, ensure_ascii=False)
    return " ".join(v.replace("\u200b", "").split())


def _norm_args(a: Any) -> str:
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except Exception:
            return _norm_text(a)
    return json.dumps(a, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_THINK_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>\s*", re.S | re.I)


def _visible(v: Any) -> str:
    if isinstance(v, list):  # content parts: keep text parts only
        v = "".join(str(p.get("text") or "") if p.get("type") in ("text", "output_text", "input_text") else
                    ("[img:" + hashlib.sha256(json.dumps(p.get("image_url") or p.get("source") or p, sort_keys=True,
                                                         default=str).encode()).hexdigest()[:12] + "]")
                    if p.get("type") in ("image_url", "input_image", "image") else ""
                    for p in v if isinstance(p, dict))
    if isinstance(v, str):
        v = _THINK_RE.sub("", v)
    return _norm_text(v)


def normalize_history(messages: list[dict]) -> list[dict]:
    """Merge consecutive assistant messages (clients split text / tool_calls / reasoning
    into separate entries) and drop empty assistant entries, so the reuse key matches the
    single reply we produced. Used for hashing AND for slicing new messages."""
    out: list[dict] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            calls = list(m.get("tool_calls") or [])
            text = m.get("content")
            if isinstance(text, list):
                text = "".join(str(p.get("text") or "") for p in text if isinstance(p, dict))
            text = _THINK_RE.sub("", text or "") if isinstance(text, str) else ""
            if not text.strip() and not calls:
                continue
            if out and out[-1].get("role") == "assistant":
                prev = out[-1]
                pt = prev.get("content") or ""
                merged = {**prev, "content": (pt + ("\n" if pt and text else "") + text) or None}
                if calls:
                    merged["tool_calls"] = list(prev.get("tool_calls") or []) + calls
                out[-1] = merged
                continue
            m = {**m, "content": text or None}
            if calls:
                m["tool_calls"] = calls
            else:
                m.pop("tool_calls", None)
        out.append(m)
    return out


def canon(m: dict) -> list:
    role = m.get("role")
    if role in ("system", "developer"):
        return ["s", _visible(m.get("content"))]
    if role == "user":
        return ["u", _visible(m.get("content"))]
    if role == "assistant":
        calls = []
        for tc in m.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            calls.append([fn.get("name") or "", _norm_args(fn.get("arguments"))])
        # clients often drop/alter the visible text next to tool calls
        return ["a", "" if calls else _visible(m.get("content")), calls]
    if role in ("tool", "function"):
        return ["t", _visible(m.get("content"))]
    return [str(role), _visible(m.get("content"))]


def _loose_canon(m: dict) -> Optional[list]:
    """Fallback key: only user/tool content + assistant turn boundaries (assistant text,
    system prompts ignored) -> survives a dynamic system prompt or reformatted replies."""
    role = m.get("role")
    if role == "user":
        return ["u", _visible(m.get("content"))]
    if role in ("tool", "function"):
        return ["t", _visible(m.get("content"))]
    if role == "assistant":
        return ["a", len(m.get("tool_calls") or [])]
    return None


def msg_hash(m: dict) -> str:
    return hashlib.sha256(json.dumps(canon(m), ensure_ascii=False).encode()).hexdigest()[:10]


IDENTITY_HEADERS = ("new-api-user", "x-newapi-user", "x-new-api-user", "x-user-id", "x-forwarded-user",
                    "x-oneapi-user", "openai-user", "x-client-user")


def identity(headers, user: str | None) -> tuple[str, list[str]]:
    """Caller identity for the reuse key: hashed API key + OpenAI `user` + any per-user
    header a relay forwards. Returns (identity, names of the sources present)."""
    parts, srcs = [], []
    auth = (headers.get("authorization") or headers.get("x-api-key") or "").strip()
    if auth:
        parts.append("k:" + hashlib.sha256(auth.encode()).hexdigest()); srcs.append("api_key")
    if user:
        parts.append("u:" + str(user)); srcs.append("user")
    for h in IDENTITY_HEADERS:
        v = headers.get(h)
        if v:
            parts.append(f"{h}:{v}"); srcs.append(h)
    return "\x01".join(parts), srcs


def namespace(model: str, tool_prompt: str, ident: str = "") -> str:
    """Model is deliberately NOT part of the key: a Notion thread accepts any model per
    turn, and agents switch models mid-conversation."""
    return hashlib.sha256(f"{ident}\x00{tool_prompt}".encode()).hexdigest()


def chain(ns: str, messages: list[dict]) -> list[str]:
    """h[k] = hash of messages[:k] (h[0] = namespace)."""
    hs = [ns]
    for m in messages:
        hs.append(hashlib.sha256((hs[-1] + json.dumps(canon(m), ensure_ascii=False)).encode()).hexdigest())
    return hs


def loose_chain(ns: str, messages: list[dict]) -> list[str]:
    hs = ["L" + ns]
    for m in messages:
        c = _loose_canon(m)
        hs.append(hs[-1] if c is None else hashlib.sha256((hs[-1] + json.dumps(c, ensure_ascii=False)).encode()).hexdigest())
    return hs


def _loose_on() -> bool:
    return os.getenv("THREAD_REUSE_LOOSE", "1").strip().lower() not in ("0", "false", "no", "off")


def _db_path() -> str:
    p = os.getenv("THREAD_REUSE_DB")
    if p is not None:
        return p.strip()
    try:
        from app.config import ADMIN_DB_PATH
        return ADMIN_DB_PATH
    except Exception:
        return ""


class ThreadReuseStore:
    """In-memory index mirrored to sqlite (table thread_reuse in admin.db) so restarts
    and deploys keep it. Only hashes, thread ids and account keys are stored."""

    def __init__(self, db_path: Optional[str] = None):
        self._d: "OrderedDict[str, dict]" = OrderedDict()
        self._loose: dict = {}
        self._lock = threading.Lock()
        self.stats = {"hit": 0, "miss": 0, "stored": 0}
        self._db = _db_path() if db_path is None else db_path
        self._loaded = False

    # ---------------- persistence
    def _conn(self):
        c = sqlite3.connect(self._db, timeout=5)
        c.execute("CREATE TABLE IF NOT EXISTS thread_reuse (k TEXT PRIMARY KEY, loose TEXT, ns TEXT, thread_id TEXT,"
                  " account TEXT, ts REAL, n INTEGER, mh TEXT)")
        return c

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._db or not os.path.isdir(os.path.dirname(self._db) or "."):
            self._db = ""
            return
        try:
            c = self._conn()
            rows = c.execute("SELECT k, loose, ns, thread_id, account, ts, n, mh FROM thread_reuse ORDER BY ts").fetchall()
            c.close()
        except Exception:
            self._db = ""
            return
        for k, loose, ns, tid, acc, ts, n, mh in rows:
            e = {"thread_id": tid, "account": acc, "ts": ts, "n": n, "ns": ns, "loose": loose,
                 "mh": (mh or "").split(",") if mh else []}
            self._d[k] = e
            if loose:
                self._loose[loose] = k
        self._gc(time.time())

    def _db_exec(self, sql: str, args: tuple = ()) -> None:
        if not self._db:
            return
        try:
            c = self._conn()
            with c:
                c.execute(sql, args)
            c.close()
        except Exception:
            pass

    def _drop(self, k: str) -> Optional[dict]:
        e = self._d.pop(k, None)
        if e is not None:
            if e.get("loose") and self._loose.get(e["loose"]) == k:
                self._loose.pop(e["loose"], None)
            self._db_exec("DELETE FROM thread_reuse WHERE k=?", (k,))
        return e

    def _gc(self, now: float) -> None:
        ttl = _ttl()
        for k in [k for k, v in self._d.items() if now - v["ts"] > ttl]:
            self._drop(k)
        while len(self._d) > _max():
            self._drop(next(iter(self._d)))

    # ---------------- api
    def take(self, ns: str, messages: list[dict], accept=None) -> tuple[Optional[dict], int, str]:
        """Find the longest stored history that is a strict prefix of messages (normalized)
        ending with an assistant turn. `accept(k)` may veto a candidate; a vetoed entry is
        NOT consumed. The accepted entry is popped. Returns (entry, k, reason)."""
        if len(messages) < 3:
            return None, 0, "short"
        hs = chain(ns, messages)
        ls = loose_chain(ns, messages) if _loose_on() else None
        now = time.time()
        vetoed = None
        with self._lock:
            self._load()
            self._gc(now)
            if not self._d:
                return None, 0, "empty"
            for k in range(len(messages) - 1, 0, -1):
                if messages[k - 1].get("role") != "assistant":
                    continue
                key = hs[k] if hs[k] in self._d else None
                how = "hit"
                if key is None and ls is not None:
                    key = self._loose.get(ls[k])
                    how = "hit_loose"
                if key is None:
                    continue
                if accept is not None:
                    why = accept(k)
                    if why:
                        vetoed = vetoed or why
                        continue
                e = self._drop(key)
                return e, k, how
        return None, 0, vetoed or "nomatch"

    def put(self, ns: str, messages: list[dict], reply: dict, thread_id: str, account: str) -> None:
        if not thread_id or not account:
            return
        full = normalize_history(list(messages) + [reply])
        key = chain(ns, full)[-1]
        loose = loose_chain(ns, full)[-1] if _loose_on() else ""
        mh = [msg_hash(m) for m in full][-400:]
        e = {"thread_id": thread_id, "account": account, "ts": time.time(), "n": len(full), "ns": ns[:16],
             "loose": loose, "mh": mh}
        with self._lock:
            self._load()
            self._d[key] = e
            self._d.move_to_end(key)
            if loose:
                self._loose[loose] = key
            self.stats["stored"] += 1
            self._db_exec("INSERT OR REPLACE INTO thread_reuse VALUES (?,?,?,?,?,?,?,?)",
                          (key, loose, e["ns"], thread_id, account, e["ts"], e["n"], ",".join(mh)))
            self._gc(time.time())

    def diagnose(self, ns: str, messages: list[dict]) -> dict:
        """Miss diagnostics (hashes only): role sequence, per-message hash prefixes, and the
        stored entry (same namespace) sharing the longest message prefix + divergence index."""
        mh = [msg_hash(m) for m in messages]
        roles = "".join({"system": "s", "developer": "s", "user": "u", "assistant": "a", "tool": "t",
                         "function": "t"}.get(m.get("role"), "?") for m in messages)
        best, best_n = None, -1
        with self._lock:
            self._load()
            for e in self._d.values():
                if e.get("ns") != ns[:16]:
                    continue
                em = e.get("mh") or []
                off = max(0, e.get("n", len(em)) - len(em))
                n = 0
                while n < len(em) and off + n < len(mh) and em[n] == mh[off + n]:
                    n += 1
                n += off if n or not off else 0
                if n > best_n:
                    best, best_n = e, n
        d = {"roles": roles[-120:], "n": len(messages), "mh": mh[-30:], "ns_entries": sum(1 for e in self._d.values() if e.get("ns") == ns[:16])}
        if best is not None:
            em = best.get("mh") or []
            d.update({"best_prefix": best_n, "best_n": best.get("n"),
                      "diverge_at": best_n,
                      "stored_at_div": em[best_n - (best.get("n", len(em)) - len(em))] if 0 <= best_n - (best.get("n", len(em)) - len(em)) < len(em) else None,
                      "req_at_div": mh[best_n] if best_n < len(mh) else None})
        return d

    def size(self) -> int:
        with self._lock:
            self._load()
            return len(self._d)

STORE = ThreadReuseStore()


def reply_from_sse(chunks: list[str]) -> Optional[dict]:
    """Rebuild the assistant message a client assembles from our chat SSE stream.
    Returns None unless the stream finished normally (finish_reason seen, no error)."""
    text, calls, finished = [], {}, False
    for raw in chunks:
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("error"):
                return None
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    text.append(d["content"])
                for tc in d.get("tool_calls") or []:
                    e = calls.setdefault(tc.get("index", 0), {"name": "", "arguments": ""})
                    fn = tc.get("function") or {}
                    e["name"] += fn.get("name") or ""
                    e["arguments"] += fn.get("arguments") or ""
                if ch.get("finish_reason"):
                    finished = True
    if not finished:
        return None
    msg: dict = {"role": "assistant", "content": "".join(text)}
    if calls:
        msg["tool_calls"] = [{"type": "function", "function": calls[i]} for i in sorted(calls)]
    if not msg["content"].strip() and not calls:
        return None
    return msg
