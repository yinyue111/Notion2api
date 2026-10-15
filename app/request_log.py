"""Pure-ASGI request-log middleware (metadata only, never prompt/response text).

Streaming bodies pass through untouched; the record is enqueued after the last
chunk (or on disconnect) and written by a background thread.
"""
import contextvars
import json
import time
from typing import Any, Optional

current_log: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("n2a_request_log", default=None)

LOGGED_PATHS = ("/v1/chat/completions", "/v1/responses")


def annotate(**kw: Any) -> None:
    rec = current_log.get()
    if rec is not None:
        rec.update({k: v for k, v in kw.items() if v is not None})


def annotate_meta(meta, **kw: Any) -> None:
    """Annotate the request record carried in meta["_rl"] (set by the request path), so
    worker threads / generators without the context var still reach it; falls back to
    the context var."""
    rec = meta.get("_rl") if isinstance(meta, dict) else None
    if rec is None:
        rec = current_log.get()
    if rec is not None:
        rec.update({k: v for k, v in kw.items() if v is not None})


def _finalize(rec: dict) -> dict:
    col = rec.pop("_collector", None)
    keys = rec.pop("_conv_keys", None)
    raw = getattr(col, "raw", None) if col is not None else None
    if raw:
        inp = int(raw.get("inputTokens", 0) or 0)
        created = int(raw.get("cachedTokensCreated", 0) or 0)
        cread = int(raw.get("cachedTokensRead", 0) or 0) if ("cachedTokensRead" in raw) else None
        rec["prompt_tokens"] = inp + created
        rec["completion_tokens"] = int(raw.get("outputTokens", 0) or 0)
        rec["cached_tokens"] = cread
        # Always write cost-estimate fields when usage is present (0 if Notion omitted the key).
        # Notion inputTokens = uncached + cache read; cachedTokensCreated counted separately.
        rec["cache_write_tokens"] = created
        rec["uncached_input_tokens"] = max(0, inp - (cread or 0))
        rec["usage_reported"] = 1
        prev = rec.get("prev_prompt_tokens")
        if prev is not None:
            try:
                prev_i = int(prev)
                rec["prompt_shrink"] = 1 if rec["prompt_tokens"] < prev_i else 0
            except (TypeError, ValueError):
                pass
    else:
        rec["usage_reported"] = 0
    if col is not None and getattr(col, "error", None) and not rec.get("error_code"):
        rec["error_code"] = "stream_error"
        rec["error_message"] = str(col.error)[:300]
    rec["stream"] = 1 if rec.get("stream") else 0
    if rec.get("notion_output_tokens") is None and rec.get("completion_tokens") is not None:
        rec["notion_output_tokens"] = rec["completion_tokens"]
    for b in ("has_tools", "long_gen_match", "silent_close", "would_downgrade"):
        if b in rec:
            rec[b] = 1 if rec[b] else 0
    if rec.get("cap_memory") == "probe" and keys and not rec.get("silent_close") \
            and (rec.get("status") or 0) < 400 and not rec.get("error_code"):
        try:
            from app import cap_memory
            cap_memory.probe_ok(keys)
            rec["cap_memory"] = "probe_ok"
        except Exception:
            pass
    return rec


class RequestLogMiddleware:
    def __init__(self, app, get_store, get_ip):
        self.app = app
        self.get_store = get_store
        self.get_ip = get_ip

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # the raw ASGI receive, for disconnect polling in long non-stream handlers:
            # BaseHTTPMiddleware's wrapped receive never reports http.disconnect
            scope["n2a_raw_receive"] = receive
        if scope["type"] != "http" or scope.get("path") not in LOGGED_PATHS:
            await self.app(scope, receive, send)
            return
        from starlette.requests import Request
        t0 = time.time()
        rec: dict = {"ts": t0, "method": scope.get("method"), "path": scope.get("path"),
                     "ip": self.get_ip(Request(scope))}
        token = current_log.set(rec)
        state = {"err_buf": b"", "done": False}

        def finish():
            if state["done"]:
                return
            state["done"] = True
            rec["latency_ms"] = int((time.time() - t0) * 1000)
            if state["err_buf"] and not rec.get("error_code"):
                try:
                    err = json.loads(state["err_buf"].decode("utf-8", "ignore")).get("error") or {}
                    rec["error_code"] = str(err.get("code") or err.get("type") or "")[:80] or None
                    rec["error_message"] = str(err.get("message") or "")[:300] or None
                except Exception:
                    pass
            store = self.get_store()
            if store is not None:
                store.enqueue_log(_finalize(rec))

        async def wrapped_send(message):
            if message["type"] == "http.response.start":
                rec["status"] = message.get("status")
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body and "ttfb_ms" not in rec:
                    rec["ttfb_ms"] = int((time.time() - t0) * 1000)
                if (rec.get("status") or 0) >= 400 and len(state["err_buf"]) < 4096:
                    state["err_buf"] += body[:4096]
                await send(message)
                if not message.get("more_body", False):
                    finish()
                return
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        except BaseException as exc:
            rec.setdefault("status", 500)
            rec.setdefault("error_code", type(exc).__name__)
            raise
        finally:
            if not state["done"]:
                rec.setdefault("error_code", "client_disconnected" if rec.get("status", 0) < 400 else None)
                finish()
            current_log.reset(token)
