"""Ghost-run guard: Notion inferences that think until the 40k output cap and never write text.

Seen 2026-10-19: one agent conversation (38 tools) made Opus think to Notion's per-inference
cap (outputTokens=40000, zero text) in ~430-470 s on EVERY attempt, at high and at medium
effort, on every account. Notion then keeps auto-retrying the inference server-side
(previousAttemptValues) after we give up. The client got an empty 200 after ~450 s and resent
the same conversation 13 times (~1.6 h, quota burnt on 7 accounts).

This module keeps a small in-memory per-conversation record of such empty runs and opens a
breaker after NOTION_GHOST_BREAKER_AFTER consecutive ones, so the next attempts fail fast
(HTTP 502, code EMPTY_STREAM_BREAKER) for a cooldown instead of burning another account.

Env (all optional):
  NOTION_GHOST_GUARD=on            master switch for the breaker (off = never block)
  NOTION_GHOST_BREAKER_AFTER=2     consecutive empty runs that open the breaker
  NOTION_GHOST_WINDOW=1800         s; empty runs further apart than this don't chain
  NOTION_GHOST_COOLDOWN=300        s; first cooldown, doubles per further empty run (max 1800)
"""
from __future__ import annotations

import os
import threading
import time
from typing import Iterable, Optional

_lock = threading.Lock()
_state: dict = {}  # conv key -> {"n": consecutive empty runs, "last": ts, "until": ts}
_MAX_KEYS = 2000


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def enabled() -> bool:
    return os.getenv("NOTION_GHOST_GUARD", "on").strip().lower() not in ("0", "off", "false", "no")


def _keys(conv_keys: Optional[Iterable]) -> list:
    return [k for k in (conv_keys or []) if isinstance(k, str) and k]


def record_empty(conv_keys, now: Optional[float] = None) -> int:
    """Count one empty (ghost) run for this conversation; returns the consecutive count."""
    now = time.time() if now is None else now
    keys = _keys(conv_keys)
    if not keys:
        return 0
    after = max(1, int(_f("NOTION_GHOST_BREAKER_AFTER", 2)))
    window = _f("NOTION_GHOST_WINDOW", 1800)
    base = _f("NOTION_GHOST_COOLDOWN", 300)
    n = 0
    with _lock:
        for k in keys:
            st = _state.get(k)
            if st is None or now - st["last"] > window:
                st = {"n": 0, "last": now, "until": 0.0}
            n = max(n, st["n"] + 1)
        for k in keys:
            until = 0.0
            if n >= after:
                until = now + min(1800.0, base * (2 ** (n - after)))
            _state[k] = {"n": n, "last": now, "until": until}
        if len(_state) > _MAX_KEYS:  # drop the oldest entries
            for k in sorted(_state, key=lambda x: _state[x]["last"])[: len(_state) - _MAX_KEYS]:
                _state.pop(k, None)
    return n


def record_ok(conv_keys) -> None:
    with _lock:
        for k in _keys(conv_keys):
            _state.pop(k, None)


def blocked(conv_keys, now: Optional[float] = None) -> float:
    """Seconds left on an open breaker for this conversation (0 = not blocked)."""
    if not enabled():
        return 0.0
    now = time.time() if now is None else now
    left = 0.0
    with _lock:
        for k in _keys(conv_keys):
            st = _state.get(k)
            if st and st["until"] > now:
                left = max(left, st["until"] - now)
    return left


def state(conv_keys) -> dict:
    with _lock:
        return {k: dict(_state[k]) for k in _keys(conv_keys) if k in _state}


def reset() -> None:  # tests
    with _lock:
        _state.clear()
