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


def _finalize(rec: dict) -> dict:
    col = rec.pop("_collector", None)
    raw = getattr(col, "raw", None) if col is not None else None
    if raw:
        rec["prompt_tokens"] = int(raw.get("inputTokens", 0)) + int(raw.get("cachedTokensCreated", 0))
        rec["completion_tokens"] = int(raw.get("outputTokens", 0))
        rec["cached_tokens"] = int(raw["cachedTokensRead"]) if "cachedTokensRead" in raw else None
        rec["usage_reported"] = 1
    else:
        rec["usage_reported"] = 0
    if col is not None and getattr(col, "error", None) and not rec.get("error_code"):
        rec["error_code"] = "stream_error"
        rec["error_message"] = str(col.error)[:300]
    rec["stream"] = 1 if rec.get("stream") else 0
    return rec


class RequestLogMiddleware:
    def __init__(self, app, get_store, get_ip):
        self.app = app
        self.get_store = get_store
        self.get_ip = get_ip

    async def __call__(self, scope, receive, send):
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
