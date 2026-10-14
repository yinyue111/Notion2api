"""OpenAI-compatible function calling emulated on top of Notion's text-only transcript.

Notion's runInferenceTranscript has no client-defined tools, so we:
  1. describe the client's tools in the prompt and ask the model to emit
     <tool_call>{"name": ..., "arguments": {...}}</tool_call> blocks;
  2. replay history (assistant tool_calls / role=tool results) as the same text
     protocol so the model sees a consistent format in context;
  3. parse the reply back into OpenAI `tool_calls`, ignoring blocks inside
     markdown code fences, repairing slightly broken JSON and dropping calls to
     unknown tools so the client never gets half-parsed garbage.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

import logging
_log = logging.getLogger(__name__)
OPEN_TAG = "<tool_call>"
CLOSE_TAG = "</tool_call>"
_FENCE_RE = re.compile(r"(```|~~~)")
# Notion's markdown renderer escapes angle brackets: "\\<tool_call\\>".
_ESCAPED_TAG_RE = re.compile(r"\\<(/?)(tool_call|tool_result)\\>")
_ESC_OPEN = "\\<tool_call\\>"


def unescape_tags(text: str) -> str:
    return _ESCAPED_TAG_RE.sub(r"<\1\2>", text) if "\\<" in text else text


# ---------------------------------------------------------------- request side

def normalize_tools(tools: Any) -> List[Dict[str, Any]]:
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if t.get("type", "function") == "function" else None
        if fn is None and "name" in t:  # tolerate flat/legacy `functions` entries
            fn = t
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        out.append({
            "name": str(fn["name"]),
            "description": str(fn.get("description") or "").strip(),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def resolve_tool_choice(tool_choice: Any, tools: List[Dict[str, Any]]) -> Tuple[str, Optional[str]]:
    """Return (mode, forced_name); mode in auto|none|required|specific."""
    if tools == []:
        return "none", None
    if tool_choice is None or tool_choice == "auto":
        return "auto", None
    if tool_choice == "none":
        return "none", None
    if tool_choice in ("required", "any"):
        return "required", None
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name") or tool_choice.get("name")
        if name and any(t["name"] == name for t in tools):
            return "specific", str(name)
    return "auto", None


def build_tool_prompt(tools: List[Dict[str, Any]], mode: str, forced: Optional[str],
                      parallel: bool) -> str:
    """Instructions written in the user's voice and framed as an output *format*.
    Framing it as "my app runs these functions for you" (or claiming operator
    authority) makes the Notion-hosted model call it prompt injection and refuse."""
    if mode == "none" or not tools:
        return ""
    specs = "\n".join(
        json.dumps({"name": t["name"], "description": t["description"], "parameters": t["parameters"]},
                   ensure_ascii=False)
        for t in tools
    )
    if mode == "required":
        rule = "For this reply, use at least one action block."
    elif mode == "specific":
        rule = f"For this reply, use an action block for `{forced}`."
    else:
        rule = ("Use action blocks whenever my request needs one of these operations; otherwise just "
                "answer normally in text.")
    multi = ("Several independent operations may be given as several action blocks in one reply."
             if parallel else "Use at most one action block per reply.")
    return (
        "Formatting request for this conversation: my editor post-processes your replies with a small "
        "script. When answering my request involves one of the operations listed below (for example "
        "creating or editing a file, or looking something up), express that operation as an action block "
        "instead of only showing the content in chat. You don't run anything yourself and don't need access "
        "to these operations: you only describe them in this format and the script does the rest, including "
        "operations that read files or fetch live data (weather, prices, etc.) you otherwise couldn't know. "
        "An action block looks like this (plain text, no code fence around it):\n"
        f"{OPEN_TAG}\n{{\"name\": \"operation_name\", \"arguments\": {{\"param\": \"value\"}}}}\n{CLOSE_TAG}\n"
        "The JSON must be valid and the arguments must follow the operation's parameter schema "
        "(put full file contents in the argument string, JSON-escaped). End your reply right after the "
        "action block(s); I'll paste the script's output back to you inside <tool_result> tags so you can "
        "continue. Never write a <tool_result> yourself. "
        f"{multi} {rule}\n\n"
        f"Operations (one JSON schema per line):\n{specs}"
    )


def render_tool_call_text(name: str, arguments: Any) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            pass
    return f"{OPEN_TAG}\n{json.dumps({'name': name, 'arguments': arguments}, ensure_ascii=False)}\n{CLOSE_TAG}"


def render_assistant_message(msg: Dict[str, Any]) -> str:
    parts = []
    content = msg.get("content") or ""
    if content.strip():
        parts.append(content)
    for tc in msg.get("tool_calls") or []:
        fn = (tc or {}).get("function") or {}
        if fn.get("name"):
            parts.append(render_tool_call_text(fn["name"], fn.get("arguments") or "{}"))
    return "\n".join(parts)


def render_tool_result(msg: Dict[str, Any], names_by_id: Dict[str, str]) -> str:
    cid = msg.get("tool_call_id") or ""
    name = msg.get("name") or names_by_id.get(cid, "")
    return (f"<tool_result name=\"{name}\" tool_call_id=\"{cid}\">\n"
            f"{msg.get('content') or ''}\n</tool_result>")


TOOL_RESULT_REMINDER = ("(Above is the script output for your previous action block(s); that operation is done, "
                        "do not repeat it. Continue: use another action block only if something else is "
                        "still needed, otherwise give me your final answer in plain text.)")


# --------------------------------------------------------------- response side

def _fence_spans(text: str) -> List[Tuple[int, int]]:
    spans, start = [], None
    for m in _FENCE_RE.finditer(text):
        # only fences at line start count
        ls = text.rfind("\n", 0, m.start()) + 1
        if text[ls:m.start()].strip():
            continue
        if start is None:
            start = m.start()
        else:
            spans.append((start, m.end()))
            start = None
    if start is not None:
        spans.append((start, len(text)))
    return spans


def _in_spans(pos: int, spans) -> bool:
    return any(a <= pos < b for a, b in spans)


def _repair_json(raw: str) -> Optional[Any]:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except ValueError:
        pass
    i = s.find("{")
    if i == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(s[i:])
        return obj
    except ValueError:
        pass
    t = re.sub(r",\s*([}\]])", r"\1", s[i:])  # trailing commas
    for extra in ("", "}", "}}", "\"}}", "]}", "}]}"):
        try:
            return json.loads(t + extra)
        except ValueError:
            continue
    return None


def _coerce_call(obj: Any, known: set) -> Optional[Dict[str, Any]]:
    if not isinstance(obj, dict):
        return None
    if "function" in obj and isinstance(obj["function"], dict):
        obj = obj["function"]
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    if not name or (known and name not in known):
        return None
    args = obj.get("arguments", obj.get("parameters", obj.get("input", {})))
    if isinstance(args, str):
        parsed = _repair_json(args) if args.strip() else {}
        args = parsed if isinstance(parsed, dict) else {"_raw": args}
    if not isinstance(args, dict):
        args = {"value": args}
    return {
        "id": "call_" + uuid.uuid4().hex[:24],
        "type": "function",
        "function": {"name": str(name), "arguments": json.dumps(args, ensure_ascii=False)},
    }


def parse_tool_calls(text: str, tools: List[Dict[str, Any]],
                     parallel: bool = True) -> Tuple[str, List[Dict[str, Any]]]:
    """Split model output into (visible_text, tool_calls)."""
    text = unescape_tags(text or "").replace(":\u200b//", "://")
    if text and text.count(CLOSE_TAG) > text.count(OPEN_TAG):
        # salvage a block whose opener was lost upstream: `{"name": ...}</tool_call>`
        text = re.sub(r'(^|</tool_call>)(\s*)(?=\{\s*"name"\s*:[^<]*?</tool_call>)', lambda m: m.group(1) + m.group(2) + OPEN_TAG, text)
    if not text or OPEN_TAG not in text:
        return text, []
    known = {t["name"] for t in tools}
    spans = _fence_spans(text)
    calls, keep, pos = [], [], 0
    for m in re.finditer(re.escape(OPEN_TAG), text):
        start = m.start()
        if start < pos or _in_spans(start, spans):
            continue
        end = text.find(CLOSE_TAG, m.end())
        body_end = end if end != -1 else len(text)
        nxt = text.find(OPEN_TAG, m.end())
        if end == -1 and nxt != -1:
            body_end = nxt
        body = text[m.end():body_end]
        obj = _repair_json(body)
        objs = obj if isinstance(obj, list) else [obj]
        parsed = [c for c in (_coerce_call(o, known) for o in objs) if c]
        if not parsed:
            continue  # leave as text: never emit half-parsed garbage
        calls.extend(parsed)
        keep.append(text[pos:start])
        pos = (end + len(CLOSE_TAG)) if end != -1 else body_end
    if not calls:
        _log.warning("tool_call tag present but nothing parsed (len=%d): %r", len(text), text[text.find(OPEN_TAG):][:300])
        return text, []
    keep.append(text[pos:])
    if not parallel:
        calls = calls[:1]
    visible = re.sub(r"\n{3,}", "\n\n", "".join(keep)).strip()
    return visible, calls


class ToolStreamSplitter:
    """Streams plain text through immediately, but holds anything that could be the
    start of a tool-call block; everything from the first real OPEN_TAG onwards is
    buffered and parsed at the end."""

    def __init__(self):
        self.buf = ""
        self.emitted = ""
        self.capturing = False

    def feed(self, chunk: str) -> str:
        self.buf += chunk or ""
        if self.capturing:
            return ""
        self.buf = unescape_tags(self.buf)
        full = self.emitted + self.buf
        spans = _fence_spans(full)
        idx = full.find(OPEN_TAG, len(self.emitted))
        while idx != -1 and _in_spans(idx, spans):
            idx = full.find(OPEN_TAG, idx + 1)
        if idx != -1:
            self.capturing = True
            out = full[len(self.emitted):idx]
            self.buf = full[idx:]
            self.emitted += out
            return out
        # hold back a possible partial "<tool_call" at the tail
        hold = 0
        for tag in (OPEN_TAG, _ESC_OPEN):
            for k in range(min(len(tag) - 1, len(self.buf)), 0, -1):
                if tag.startswith(self.buf[-k:]):
                    hold = max(hold, k)
                    break
        out = self.buf[:len(self.buf) - hold]
        self.buf = self.buf[len(self.buf) - hold:]
        self.emitted += out
        return out

    def finish(self, tools, parallel=True, final_text: Optional[str] = None):
        """Return (tail_text_to_emit, tool_calls)."""
        full = self.emitted + unescape_tags(self.buf)
        final_text = unescape_tags(final_text or "")
        if final_text and final_text.startswith(self.emitted.strip()):
            full = final_text
        visible, calls = parse_tool_calls(full, tools, parallel)
        if calls:
            e = self.emitted.strip()
            tail = visible[len(e):].strip() if visible.startswith(e) else ""
            return tail, calls
        return full[len(self.emitted):], []
