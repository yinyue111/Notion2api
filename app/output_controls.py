"""OpenAI output controls Notion doesn't support natively, applied after generation:
`stop` sequences, `max_tokens`/`max_completion_tokens` (approximate token count, cut at the
limit with finish_reason="length"), and `response_format` json_object/json_schema (prompted,
then code fences / surrounding prose removed). Works on the final JSON body and on the SSE
stream (wraps the body iterator; on an early stop the upstream generator is closed so the
Notion stream ends)."""
from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator, Optional

_CJK = re.compile(r"[\u3000-\u9fff\uac00-\ud7af\uff00-\uffef]")


def approx_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return cjk + max(0, (len(text) - cjk + 3) // 4)


def cut_to_tokens(text: str, limit: int) -> str:
    if approx_tokens(text) <= limit:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if approx_tokens(text[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def parse_controls(body: dict) -> dict:
    stop = body.get("stop")
    if isinstance(stop, str):
        stop = [stop]
    stop = [s for s in (stop or []) if isinstance(s, str) and s][:4]
    mt = body.get("max_completion_tokens") or body.get("max_tokens") or body.get("max_output_tokens")
    try:
        mt = int(mt) if mt is not None else None
        if mt is not None and mt <= 0:
            mt = None
    except (TypeError, ValueError):
        mt = None
    rf = body.get("response_format")
    fmt = None
    if isinstance(rf, dict) and rf.get("type") in ("json_object", "json_schema"):
        fmt = rf
    return {"stop": stop, "max_tokens": mt, "format": fmt}


def json_instruction(fmt: Optional[dict]) -> str:
    if not fmt:
        return ""
    base = ("Output format requirement: reply with ONLY one valid JSON value (an object unless the schema "
            "says otherwise), with no explanation, no Markdown and no code fences.")
    if fmt.get("type") == "json_schema":
        js = fmt.get("json_schema") or {}
        schema = js.get("schema") if isinstance(js, dict) else None
        if schema is None:
            schema = fmt.get("schema")
        if schema is not None:
            base += " It must conform to this JSON Schema (all required properties present, no extra " \
                    "properties if additionalProperties is false):\n" + json.dumps(schema, ensure_ascii=False)
    return base


_FENCE_OPEN = re.compile(r"^\s*```[a-zA-Z0-9_-]*[ \t]*\n")
_FENCE_CLOSE = re.compile(r"\n?```\s*$")


def extract_json(text: str) -> str:
    t = text.strip()
    t = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", t)).strip()
    try:
        json.loads(t)
        return t
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.S)
    if m:
        try:
            json.loads(m.group(1).strip())
            return m.group(1).strip()
        except Exception:
            pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                obj, end = dec.raw_decode(text[i:])
                return text[i:i + end]
            except Exception:
                continue
    return t


def apply_text(text: str, ctl: dict) -> tuple[str, Optional[str]]:
    """Returns (text, forced_finish_reason or None)."""
    fin = None
    if ctl.get("format") and text:
        text = extract_json(text)
    if ctl.get("stop") and text:
        idx = [text.find(s) for s in ctl["stop"] if text.find(s) != -1]
        if idx:
            text, fin = text[:min(idx)], "stop"
    if ctl.get("max_tokens") and text and approx_tokens(text) > ctl["max_tokens"]:
        text, fin = cut_to_tokens(text, ctl["max_tokens"]), "length"
    return text, fin


def apply_json_body(data: dict, ctl: dict) -> dict:
    for ch in data.get("choices") or []:
        msg = ch.get("message") or {}
        if isinstance(msg.get("content"), str) and not msg.get("tool_calls"):
            new, fin = apply_text(msg["content"], ctl)
            msg["content"] = new
            if fin:
                ch["finish_reason"] = fin
                if fin == "length" and data.get("usage"):
                    u = data["usage"]
                    u["completion_tokens"] = min(u.get("completion_tokens") or 0, ctl["max_tokens"]) or ctl["max_tokens"]
                    u["total_tokens"] = (u.get("prompt_tokens") or 0) + u["completion_tokens"]
    return data


class _StreamState:
    def __init__(self, ctl: dict):
        self.ctl = ctl
        self.emitted = ""
        self.hold = ""
        self.done_reason: Optional[str] = None
        self.json_lead = bool(ctl.get("format"))  # still deciding about a leading fence
        self.maxstop = max([len(s) for s in ctl.get("stop") or []] or [0])

    def push(self, piece: str, final: bool = False) -> str:
        """Return the content that may be emitted now."""
        if self.done_reason:
            return ""
        buf = self.hold + piece
        self.hold = ""
        if self.ctl.get("format"):
            if self.json_lead:
                s = buf.lstrip()
                if not s and not final:
                    self.hold = buf
                    return ""
                if s.startswith("`") and "\n" not in s and not final:
                    self.hold = buf
                    return ""
                m = _FENCE_OPEN.match(buf)
                if m:
                    buf = buf[m.end():]
                elif s and s[0] not in "{[" and not final:
                    # prose before JSON: skip until the first { or [
                    j = min([p for p in (buf.find("{"), buf.find("[")) if p != -1] or [-1])
                    if j == -1:
                        return ""
                    buf = buf[j:]
                else:
                    buf = buf.lstrip()
                self.json_lead = False
            # hold a possible trailing fence
            if not final:
                k = buf.rfind("`")
                tailstart = max(0, len(buf) - 5)
                if k != -1 and k >= tailstart:
                    cut = buf.rfind("\n", 0, k + 1)
                    cut = cut if cut != -1 and cut >= tailstart - 1 else k
                    self.hold, buf = buf[cut:], buf[:cut]
            else:
                buf = _FENCE_CLOSE.sub("", buf)
        stops = self.ctl.get("stop") or []
        if stops:
            text = buf
            hits = [text.find(s) for s in stops if text.find(s) != -1]
            if hits:
                buf = text[:min(hits)]
                self.done_reason = "stop"
            elif not final and self.maxstop > 1:
                keep = self.maxstop - 1
                self.hold = text[-keep:] + self.hold if len(text) >= keep else text + self.hold
                buf = text[:-keep] if len(text) >= keep else ""
        mt = self.ctl.get("max_tokens")
        if mt and buf:
            if approx_tokens(self.emitted + buf) > mt:
                buf = cut_to_tokens(self.emitted + buf, mt)[len(self.emitted):]
                self.done_reason = "length"
                self.hold = ""
        self.emitted += buf
        return buf


def _chunk(template: dict, content: Optional[str] = None, finish: Optional[str] = None) -> str:
    obj = {k: template.get(k) for k in ("id", "object", "created", "model") if k in template}
    obj.setdefault("object", "chat.completion.chunk")
    delta = {} if content is None else {"content": content}
    obj["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


async def wrap_stream(it: AsyncIterator, ctl: dict) -> AsyncIterator[str]:
    """Rewrite an OpenAI chat SSE stream applying ctl."""
    st = _StreamState(ctl)
    template: dict = {}
    buf = ""
    finished_sent = False
    saw_tools = False
    try:
        async for raw in it:
            s = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            buf += s
            while "\n\n" in buf:
                ev, buf = buf.split("\n\n", 1)
                out = _process_event(ev, st, template, ctl)
                if out is None:
                    continue
                text, ended, tools = out
                saw_tools = saw_tools or tools
                if text:
                    yield text
                if ended == "abort":
                    finished_sent = True
                    # our own finish + [DONE]; close upstream (Notion stream) right away
                    yield _chunk(template, None, st.done_reason)
                    yield "data: [DONE]\n\n"
                    return
        if buf.strip():
            out = _process_event(buf, st, template, ctl)
            if out and out[0]:
                yield out[0]
    finally:
        aclose = getattr(it, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass


def _process_event(ev: str, st: _StreamState, template: dict, ctl: dict):
    lines = ev.split("\n")
    data_lines = [l[5:].lstrip() for l in lines if l.startswith("data:")]
    if not data_lines:
        return (ev + "\n\n", None, False)
    data = "\n".join(data_lines)
    if data.strip() == "[DONE]":
        return ("data: [DONE]\n\n", None, False)
    try:
        obj = json.loads(data)
    except Exception:
        return (ev + "\n\n", None, False)
    for k in ("id", "object", "created", "model"):
        if k in obj and k not in template:
            template[k] = obj[k]
    choices = obj.get("choices") or []
    if not choices:
        return (ev + "\n\n", None, False)
    ch = choices[0]
    delta = ch.get("delta") or {}
    tools = bool(delta.get("tool_calls"))
    pre = ""
    fin = ch.get("finish_reason")
    if isinstance(delta.get("content"), str) and delta["content"]:
        allowed = st.push(delta["content"])
        if st.done_reason:
            pre = _chunk(template, allowed) if allowed else ""
            # emit any other fields of this delta? (role) — role already sent earlier normally
            return (pre, "abort", tools)
        if allowed == delta["content"]:
            return (ev + "\n\n", None, tools)
        delta = dict(delta)
        if allowed:
            delta["content"] = allowed
        else:
            delta.pop("content", None)
            if not delta and not fin:
                return ("", None, tools)
        obj["choices"] = [dict(ch, delta=delta)] + choices[1:]
        return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n", None, tools)
    if fin:
        tail = st.push("", final=True)
        pre = _chunk(template, tail) if tail else ""
        if st.done_reason:
            obj["choices"] = [dict(ch, finish_reason=st.done_reason)] + choices[1:]
        return (pre + "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n", None, tools)
    return (ev + "\n\n", None, tools)
