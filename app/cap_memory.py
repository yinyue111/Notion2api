"""Conversation cap memory + effort-autoroute shadow.

Cap memory (NOTION_CAP_MEMORY=on|off, default on): when a xhigh/max request in a
conversation hits Notion's silent close / 40k output cap and has to be hedged at a
lower effort (or fails after burning the Max inference), the conversation key is
remembered: the next turns run at `high` instead of xhigh/max. After
NOTION_CAP_MEMORY_PROBE (default 3) forced-high turns one turn is sent at the
requested effort again (probe); a probe that answers without a silent close clears
the memory, one that hits the cap again re-arms it. Entries expire after
NOTION_CAP_MEMORY_TTL seconds (default 1800) since the last cap.

Conversation key (conv_key): sha256 of caller identity (hashed API key + `user` +
relay user headers, as thread_reuse) + the FIRST user message text. Stable across
turns of the same chat whether thread reuse hits or misses. The Notion thread id
of a reused thread is also registered as an alias. Weakness: two different chats
of the same caller that open with the identical first message share a key, and
clients that rewrite/compact the first message (or one-shot calls: thread_reuse
`miss:short`, `nomatch`) get a fresh key each time, so memory can't follow them.
In-memory only (lost on restart; TTL is short anyway).

Autoroute shadow (NOTION_EFFORT_AUTOROUTE=off|shadow|on, default shadow): the
pre-request rule "xhigh/max AND long-generation ask AND (client max_tokens >=
NOTION_AUTOROUTE_MIN_MAX_TOKENS or a file-writing tool offered)" is only logged
(request_log.would_downgrade); `on` would apply it (not enabled in prod).
"""
import hashlib
import os
import threading
import time

MAX_EFFORTS = ("xhigh", "max")
_WRITE_TOOL_HINTS = ("write", "edit", "create_file", "str_replace", "apply_patch", "save", "put_file", "notebook")

_lock = threading.Lock()
_mem: dict = {}  # key -> {"until", "high_turns", "caps", "alias"}


def _on(name, default):
    return os.getenv(name, default).strip().lower() not in ("0", "false", "off", "no", "")


def enabled() -> bool:
    return _on("NOTION_CAP_MEMORY", "on")


def ttl() -> float:
    return float(os.getenv("NOTION_CAP_MEMORY_TTL", "1800") or 1800)


def probe_every() -> int:
    return int(os.getenv("NOTION_CAP_MEMORY_PROBE", "3") or 3)


def autoroute_mode() -> str:
    m = os.getenv("NOTION_EFFORT_AUTOROUTE", "shadow").strip().lower()
    return m if m in ("off", "shadow", "on") else "shadow"


def _text(c) -> str:
    if isinstance(c, str):
        return c
    out = []
    for p in c or []:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            out.append(p["text"])
    return "\n".join(out)


def conv_key(ident: str, messages) -> str:
    first = ""
    for m in messages or []:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role == "user":
            c = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
            first = " ".join(_text(c).split())
            break
    if not first:
        return ""
    return "c:" + hashlib.sha256(f"{ident}\x00{first}".encode()).hexdigest()[:20]


def _get(keys, now):
    for k in keys:
        if not k:
            continue
        e = _mem.get(k)
        if e is None:
            continue
        if e["until"] < now:
            _mem.pop(k, None)
            continue
        return k, e
    return None, None


def decide(keys, effort):
    """-> (effort_to_use, status). status: None (no memory), 'forced_high', 'probe'."""
    if not enabled() or effort not in MAX_EFFORTS:
        return effort, None
    now = time.time()
    with _lock:
        k, e = _get(keys, now)
        if e is None:
            return effort, None
        if e["high_turns"] >= probe_every():
            e["high_turns"] = 0
            e["probing"] = True
            return effort, "probe"
        e["high_turns"] += 1
        return "high", "forced_high"


def mark(keys, reason=""):
    """A xhigh/max inference of this conversation hit the silent close / cap."""
    keys = [k for k in (keys or []) if k]
    if not enabled() or not keys:
        return
    now = time.time()
    with _lock:
        _, e = _get(keys, now)
        if e is None:
            e = {"high_turns": 0, "caps": 0}
        e["until"] = now + ttl()
        e["caps"] = e.get("caps", 0) + 1
        e["high_turns"] = 0
        e["probing"] = False
        e["reason"] = reason
        for k in keys:
            _mem[k] = e
        if len(_mem) > 5000:
            for k in [k for k, v in _mem.items() if v["until"] < now]:
                _mem.pop(k, None)


def alias(keys, new_key):
    """Register another key (e.g. reused Notion thread id) for an existing entry."""
    if not new_key:
        return
    with _lock:
        _, e = _get(keys, time.time())
        if e is not None:
            _mem[new_key] = e


def probe_ok(keys):
    """A probe at the requested effort answered without a silent close: forget."""
    with _lock:
        _, e = _get(keys, time.time())
        if e is None or not e.get("probing"):
            return
        for k in [k for k, v in _mem.items() if v is e]:
            _mem.pop(k, None)


def state(keys):
    with _lock:
        _, e = _get(keys, time.time())
        return dict(e) if e else None


def clear():
    with _lock:
        _mem.clear()


def has_write_tool(tools) -> bool:
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        name = str((fn or t or {}).get("name") or "").lower() if isinstance(fn or t, dict) else ""
        if any(h in name for h in _WRITE_TOOL_HINTS):
            return True
    return False


def would_downgrade(effort, long_match, client_max_tokens, write_tool) -> bool:
    if effort not in MAX_EFFORTS or not long_match:
        return False
    min_mt = int(os.getenv("NOTION_AUTOROUTE_MIN_MAX_TOKENS", "16000") or 16000)
    return bool(write_tool) or (client_max_tokens or 0) >= min_mt


def map_max_target() -> str:
    """NOTION_MAP_MAX_TO=xhigh|high|off (default xhigh): effort sent to Notion when client asks max."""
    v = (os.getenv("NOTION_MAP_MAX_TO", "xhigh") or "xhigh").strip().lower()
    return v if v in ("xhigh", "high", "off") else "xhigh"


def map_max(eff: str) -> str:
    if eff != "max":
        return eff
    t = map_max_target()
    return eff if t == "off" else t
