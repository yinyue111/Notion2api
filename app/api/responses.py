"""OpenAI Responses API (/v1/responses) on top of the chat-completions pipeline.

Request items are converted to chat messages (+ emulated function tools), the chat
handler is run, and its result / SSE chunks are translated back into Responses
objects and typed SSE events. Stateless: previous_response_id is rejected, store is
ignored (always reported as false), hosted/built-in tools are ignored.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.logger import logger
from app.schemas import ChatCompletionRequest

router = APIRouter()

_TEXT_PART_TYPES = ("input_text", "output_text", "text", "summary_text")


def _err(status: int, message: str, param: Optional[str] = None, code: str = "unsupported_parameter"):
    return JSONResponse({"error": {"message": message, "type": "invalid_request_error", "param": param, "code": code}},
                        status_code=status)


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict) and p.get("type") in _TEXT_PART_TYPES:
                out.append(p.get("text") or "")
            elif isinstance(p, dict) and p.get("type") in ("input_image", "input_file"):
                out.append("[attachment omitted: not supported by this endpoint]")
        return "".join(out)
    return str(content)


def convert_tools(tools: Any) -> Tuple[List[Dict[str, Any]], set, List[str]]:
    """Responses tools -> chat tools. Returns (chat_tools, custom_tool_names, ignored_types)."""
    chat, custom, ignored = [], set(), []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        typ = t.get("type")
        if typ == "function" and t.get("name"):
            chat.append({"type": "function", "function": {
                "name": t["name"], "description": t.get("description") or "",
                "parameters": t.get("parameters") or {"type": "object", "properties": {}}}})
        elif typ == "custom" and t.get("name"):
            # free-form tools (e.g. Codex apply_patch): emulate as one string argument
            fmt = t.get("format") or {}
            desc = t.get("description") or ""
            if isinstance(fmt, dict) and fmt.get("type") == "grammar" and fmt.get("definition"):
                desc += f"\nThe input must follow this {fmt.get('syntax', '')} grammar:\n{fmt['definition']}"
            custom.add(t["name"])
            chat.append({"type": "function", "function": {
                "name": t["name"], "description": desc,
                "parameters": {"type": "object", "properties": {"input": {"type": "string", "description": "Raw tool input text"}},
                               "required": ["input"]}}})
        else:
            ignored.append(str(typ))
    return chat, custom, ignored


def convert_tool_choice(tc: Any) -> Any:
    if isinstance(tc, dict):
        if tc.get("type") in ("function", "custom") and tc.get("name"):
            return {"type": "function", "function": {"name": tc["name"]}}
        if tc.get("type") == "allowed_tools":
            return tc.get("mode") or "auto"
        return "auto"
    return tc


def convert_input(instructions: Optional[str], inp: Any, custom: set) -> List[Dict[str, Any]]:
    msgs: List[Dict[str, Any]] = []
    if instructions:
        msgs.append({"role": "system", "content": instructions})
    if isinstance(inp, str):
        msgs.append({"role": "user", "content": inp})
        return msgs
    for item in inp or []:
        if not isinstance(item, dict):
            continue
        typ = item.get("type") or ("message" if "role" in item else None)
        if typ == "message":
            role = item.get("role") or "user"
            if role == "developer":
                role = "system"
            msgs.append({"role": role, "content": _content_text(item.get("content"))})
        elif typ in ("function_call", "custom_tool_call"):
            args = item.get("arguments")
            if typ == "custom_tool_call":
                args = json.dumps({"input": item.get("input") or ""}, ensure_ascii=False)
            call = {"id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function", "function": {"name": item.get("name") or "", "arguments": args or "{}"}}
            if msgs and msgs[-1]["role"] == "assistant":
                # text + calls of one turn = one assistant message (as chat/completions returns it)
                msgs[-1].setdefault("tool_calls", []).append(call)
            else:
                msgs.append({"role": "assistant", "content": "", "tool_calls": [call]})
        elif typ in ("function_call_output", "custom_tool_call_output"):
            out = item.get("output")
            msgs.append({"role": "tool", "tool_call_id": item.get("call_id") or "",
                         "content": out if isinstance(out, str) else _content_text(out) or json.dumps(out, ensure_ascii=False)})
        # reasoning / item_reference / other item types: dropped (stateless bridge)
    return msgs


def _usage(chat_usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    u = chat_usage or {}
    pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    return {"input_tokens": pt, "input_tokens_details": {"cached_tokens": cached},
            "output_tokens": ct, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": pt + ct}


def _call_item(call: Dict[str, Any], custom: set, status: str = "completed") -> Dict[str, Any]:
    fn = call["function"]
    cid = call["id"]
    if fn["name"] in custom:
        try:
            inp = json.loads(fn["arguments"]).get("input", "")
        except (ValueError, AttributeError):
            inp = fn["arguments"]
        return {"type": "custom_tool_call", "id": "ctc_" + uuid.uuid4().hex[:24], "call_id": cid,
                "name": fn["name"], "input": inp if isinstance(inp, str) else json.dumps(inp), "status": status}
    return {"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:24], "call_id": cid,
            "name": fn["name"], "arguments": fn["arguments"], "status": status}


def _message_item(text: str, item_id: Optional[str] = None, status: str = "completed") -> Dict[str, Any]:
    return {"type": "message", "id": item_id or "msg_" + uuid.uuid4().hex[:24], "status": status, "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": [], "logprobs": []}] if status == "completed" or text else []}


def _response_obj(rid: str, body: Dict[str, Any], output: List[Dict[str, Any]], usage: Optional[Dict[str, Any]],
                  status: str, created: int) -> Dict[str, Any]:
    return {
        "id": rid, "object": "response", "created_at": created, "status": status,
        "background": False, "error": None, "incomplete_details": None,
        "instructions": body.get("instructions"), "max_output_tokens": body.get("max_output_tokens"),
        "model": body.get("model"), "output": output,
        "parallel_tool_calls": body.get("parallel_tool_calls", True), "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None}, "store": False,
        "temperature": body.get("temperature", 1.0), "text": {"format": {"type": "text"}},
        "tool_choice": body.get("tool_choice", "auto"), "tools": body.get("tools") or [],
        "top_p": body.get("top_p", 1.0), "truncation": "disabled",
        "usage": usage, "user": None, "metadata": body.get("metadata") or {},
    }


def _text_format(text: Any) -> Optional[Dict[str, Any]]:
    """Responses `text.format` -> chat `response_format`."""
    fmt = (text or {}).get("format") if isinstance(text, dict) else None
    if not isinstance(fmt, dict):
        return None
    if fmt.get("type") == "json_object":
        return {"type": "json_object"}
    if fmt.get("type") == "json_schema":
        return {"type": "json_schema", "json_schema": {"name": fmt.get("name") or "schema",
                                                       "schema": fmt.get("schema") or {}, "strict": fmt.get("strict")}}
    return None


@router.post("/responses", tags=["responses"])
async def create_response(request: Request, response: Response):
    from app.api.chat import create_chat_completion  # late import: avoid cycles

    try:
        body = await request.json()
    except ValueError:
        return _err(400, "Invalid JSON body", code="invalid_json")
    if not isinstance(body, dict):
        return _err(400, "Body must be a JSON object", code="invalid_json")
    if body.get("previous_response_id"):
        return _err(400, "previous_response_id is not supported: this endpoint is stateless (store is not "
                         "available). Send the full conversation in `input` instead.", "previous_response_id")
    if body.get("background"):
        return _err(400, "background responses are not supported", "background")

    chat_tools, custom, ignored = convert_tools(body.get("tools"))
    if ignored:
        logger.info("Responses: ignoring unsupported tool types",
                    extra={"request_info": {"event": "responses_tools_ignored", "types": ignored}})
    messages = convert_input(body.get("instructions"), body.get("input"), custom)
    if not messages:
        return _err(400, "`input` is required", "input", code="missing_required_parameter")
    stream = bool(body.get("stream"))
    try:
        chat_req = ChatCompletionRequest(
            model=body.get("model") or "claude-opus4.6",
            messages=messages, stream=stream,
            tools=chat_tools or None, tool_choice=convert_tool_choice(body.get("tool_choice")),
            parallel_tool_calls=body.get("parallel_tool_calls"),
            reasoning=body.get("reasoning") if isinstance(body.get("reasoning"), dict) else None,
            stream_options={"include_usage": True} if stream else None,
            user=str(body.get("user") or body.get("safety_identifier") or body.get("prompt_cache_key") or "") or None,
            max_tokens=body.get("max_output_tokens") if isinstance(body.get("max_output_tokens"), int) else None,
            response_format=_text_format(body.get("text")),
        )
    except Exception as exc:  # pydantic validation
        return _err(400, f"Invalid input: {str(exc)[:300]}", "input", code="invalid_value")

    rid = "resp_" + uuid.uuid4().hex
    created = int(time.time())
    result = await create_chat_completion(request, chat_req, BackgroundTasks(), response)

    if not stream:
        if isinstance(result, JSONResponse) or isinstance(result, Response):
            if result.status_code != 200:
                return result
            data = json.loads(result.body)
        elif hasattr(result, "model_dump"):
            data = result.model_dump()
        else:
            data = result
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        output: List[Dict[str, Any]] = []
        if msg.get("content"):
            output.append(_message_item(msg["content"]))
        for call in msg.get("tool_calls") or []:
            output.append(_call_item(call, custom))
        if not output:
            output.append(_message_item(""))
        fin = (data.get("choices") or [{}])[0].get("finish_reason")
        robj = _response_obj(rid, body, output, _usage(data.get("usage")), "incomplete" if fin == "length" else "completed", created)
        if fin == "length":
            robj["incomplete_details"] = {"reason": "max_output_tokens"}
        if isinstance(body.get("text"), dict):
            robj["text"] = body["text"]
        return JSONResponse(robj)

    if not isinstance(result, StreamingResponse):
        return result  # error before streaming started
    return StreamingResponse(_translate_stream(result, rid, body, custom, created), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _translate_stream(chat_stream: StreamingResponse, rid: str, body: Dict[str, Any], custom: set, created: int):
    seq = 0

    def ev(typ: str, **data) -> str:
        nonlocal seq
        payload = {"type": typ, "sequence_number": seq, **data}
        seq += 1
        return f"event: {typ}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    output: List[Dict[str, Any]] = []
    usage = None
    msg = None          # open message item {id, text, index}
    calls: Dict[int, Dict[str, Any]] = {}  # chat tool index -> state
    finished = False
    truncated = False

    yield ev("response.created", response=_response_obj(rid, body, [], None, "in_progress", created))
    yield ev("response.in_progress", response=_response_obj(rid, body, [], None, "in_progress", created))

    def close_msg():
        nonlocal msg
        if msg is None:
            return []
        out = [ev("response.output_text.done", item_id=msg["id"], output_index=msg["index"], content_index=0,
                  text=msg["text"], logprobs=[]),
               ev("response.content_part.done", item_id=msg["id"], output_index=msg["index"], content_index=0,
                  part={"type": "output_text", "text": msg["text"], "annotations": [], "logprobs": []})]
        item = _message_item(msg["text"], msg["id"])
        output[msg["index"]] = item
        out.append(ev("response.output_item.done", output_index=msg["index"], item=item))
        msg = None
        return out

    def close_call(st):
        if st.get("closed"):
            return []
        st["closed"] = True
        item = _call_item({"id": st["call_id"], "function": {"name": st["name"], "arguments": st["args"]}}, custom)
        item["id"] = st["item_id"]
        output[st["index"]] = item
        if item["type"] == "custom_tool_call":
            res = [ev("response.custom_tool_call_input.delta", item_id=st["item_id"], output_index=st["index"], delta=item["input"]),
                   ev("response.custom_tool_call_input.done", item_id=st["item_id"], output_index=st["index"], input=item["input"])]
        else:
            res = [ev("response.function_call_arguments.done", item_id=st["item_id"], output_index=st["index"],
                      arguments=st["args"], name=st["name"])]
        res.append(ev("response.output_item.done", output_index=st["index"], item=item))
        return res

    buf = ""
    try:
        async for raw in chat_stream.body_iterator:
            buf += raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                line = next((l[5:].strip() for l in block.splitlines() if l.startswith("data:")), "")
                if not line or line == "[DONE]":
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                if chunk.get("usage"):
                    usage = _usage(chunk["usage"])
                for ch in chunk.get("choices") or []:
                    delta = ch.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        if msg is None:
                            msg = {"id": "msg_" + uuid.uuid4().hex[:24], "text": "", "index": len(output)}
                            output.append(_message_item("", msg["id"], "in_progress"))
                            yield ev("response.output_item.added", output_index=msg["index"], item=output[msg["index"]])
                            yield ev("response.content_part.added", item_id=msg["id"], output_index=msg["index"],
                                     content_index=0, part={"type": "output_text", "text": "", "annotations": [], "logprobs": []})
                        msg["text"] += text
                        yield ev("response.output_text.delta", item_id=msg["id"], output_index=msg["index"],
                                 content_index=0, delta=text, logprobs=[])
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        st = calls.get(idx)
                        if st is None:
                            for x in close_msg():
                                yield x
                            name = (tc.get("function") or {}).get("name") or ""
                            st = {"call_id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}", "name": name, "args": "",
                                  "index": len(output),
                                  "item_id": ("ctc_" if name in custom else "fc_") + uuid.uuid4().hex[:24]}
                            calls[idx] = st
                            if name in custom:
                                added = {"type": "custom_tool_call", "id": st["item_id"], "call_id": st["call_id"],
                                         "name": name, "input": "", "status": "in_progress"}
                            else:
                                added = {"type": "function_call", "id": st["item_id"], "call_id": st["call_id"],
                                         "name": name, "arguments": "", "status": "in_progress"}
                            output.append(added)
                            yield ev("response.output_item.added", output_index=st["index"], item=added)
                        d = (tc.get("function") or {}).get("arguments") or ""
                        if d:
                            st["args"] += d
                            if st["name"] not in custom:
                                yield ev("response.function_call_arguments.delta", item_id=st["item_id"],
                                         output_index=st["index"], delta=d)
                    if ch.get("finish_reason"):
                        finished = True
                        if ch.get("finish_reason") == "length":
                            truncated = True
    except Exception:
        logger.error("Responses stream translation failed", exc_info=True,
                     extra={"request_info": {"event": "responses_stream_error"}})
        yield ev("response.failed", response={**_response_obj(rid, body, output, usage, "failed", created),
                                                "error": {"code": "server_error", "message": "upstream stream interrupted"}})
        return
    for x in close_msg():
        yield x
    for st in calls.values():
        for x in close_call(st):
            yield x
    status = "completed" if finished and not truncated else "incomplete"
    final = _response_obj(rid, body, output, usage, status, created)
    if isinstance(body.get("text"), dict):
        final["text"] = body["text"]
    if status == "incomplete":
        final["incomplete_details"] = {"reason": "max_output_tokens" if truncated else "interrupted"}
    yield ev("response.completed" if status == "completed" else "response.incomplete", response=final)
