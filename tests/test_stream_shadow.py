"""Notion NDJSON patch stream: in-step retry (previousAttemptValues) and value-block
removes. Fixtures mirror the real shapes (live capture 2026-10-18 12:23 and the thread
records of requests 1592/1595/1608/1610)."""
import json
import pytest
from app.stream_parser import parse_stream, _missing_tail, _ShadowState
from app.api import chat
from app.tool_emulation import normalize_tools

TOOLS = normalize_tools([{"type": "function", "function": {"name": "Bash", "parameters": {}}}])
CALL_TEXT = '<tool_call>\n{"name": "Bash", "arguments": {"command": "ls D:/工作核心", "description": "List"}}\n</tool_call>'


class Resp:
    def __init__(self, lines):
        self.lines = [json.dumps(l, ensure_ascii=False) if not isinstance(l, str) else l for l in lines]
    def iter_lines(self, decode_unicode=True):
        return iter(self.lines)


def P(*ops):
    return {"type": "patch", "v": list(ops)}


def chunks(text, n=7):
    return [text[i:i + n] for i in range(0, len(text), n)]


import os
_LIVE = [json.loads(l) for l in open(os.path.join(os.path.dirname(__file__), "fixtures", "live_normal_haiku.ndjson")) if l.strip()]
_CUT = next(i for i, l in enumerate(_LIVE) if l.get("type") == "patch" and l["v"][0].get("o") == "r")
_END = next(i for i, l in enumerate(_LIVE) if l.get("type") == "patch" and l["v"][0].get("p", "").endswith("/finishedAt"))
PREFIX, SUFFIX = _LIVE[:_CUT + 1], _LIVE[_END:]   # live: <lang …> provisional block created then removed
V = "/s/5/value"


def normal_stream(text):
    """previousAttemptValues=False (live capture shape): text block appended after the
    provisional <lang> block was removed, streamed with x ops at value/0."""
    cs = chunks(text)
    return (PREFIX + [P({"o": "a", "p": V + "/-", "v": {"type": "text", "content": cs[0]}})]
            + [P({"o": "x", "p": V + "/0/content", "v": c}) for c in cs[1:]] + SUFFIX)


def retry_stream(text, mode="remove"):
    """previousAttemptValues=True: attempt 1 = [thinking, tool_use] (a native call the
    harness rejected, cf. thread records of 1592/1595/1608/1610), moved to
    previousAttemptValues; value rebuilt; the new text block sits at index 1."""
    lines = PREFIX + [P({"o": "a", "p": V + "/-", "v": {"type": "thinking", "content": ""}}),
                      P({"o": "a", "p": V + "/-", "v": {"type": "tool_use", "content": "{\"command\": "}}),
                      P({"o": "x", "p": V + "/1/content", "v": "\"R='D:/x'\"}"}),
                      P({"o": "a", "p": "/s/5/previousAttemptValues/-",
                         "v": [{"type": "thinking", "content": ""}, {"type": "tool_use", "content": "{\"command\": \"R='D:/x'\"}"}]})]
    cs = chunks(text)
    if mode == "remove":
        lines += [P({"o": "r", "p": V + "/1"}), P({"o": "r", "p": V + "/0"}),
                  P({"o": "a", "p": V + "/-", "v": {"type": "thinking", "content": ""}}),
                  P({"o": "a", "p": V + "/-", "v": {"type": "text", "content": cs[0]}})]
        lines += [P({"o": "x", "p": V + "/1/content", "v": c}) for c in cs[1:]]
    elif mode == "replace":
        lines += [P({"o": "p", "p": V, "v": [{"type": "thinking", "content": ""}, {"type": "text", "content": cs[0]}]})]
        lines += [P({"o": "x", "p": V + "/1/content", "v": c}) for c in cs[1:]]
    elif mode == "suffix":  # whole new value in one replaceSuffix, no text-carrying op
        lines += [P({"o": "s", "p": V, "r": 2, "v": [{"type": "thinking", "content": ""}, {"type": "text", "content": text}]})]
    return lines + SUFFIX


def collect(lines, monkeypatch=None):
    content, thinking = "", ""
    for it in parse_stream(Resp(lines)):
        if it["type"] == "content":
            content += it["text"]
        elif it["type"] == "thinking":
            thinking += it["text"]
    from app.lang_sanitizer import clean_lang
    return clean_lang(content), thinking


@pytest.mark.parametrize("text", [CALL_TEXT, "The music mix didn't update anything because I ran it without -y.\n\n" + CALL_TEXT])
@pytest.mark.parametrize("mode", ["remove", "replace", "suffix"])
def test_retry_format_full_text(text, mode, monkeypatch):
    content, thinking = collect(retry_stream(text, mode))
    assert content.strip() == text.strip()
    assert "<tool_call>" not in thinking


def test_retry_format_old_parser_reproduces_bug(monkeypatch):
    monkeypatch.setenv("NOTION_STREAM_SHADOW", "0")
    content, thinking = collect(retry_stream(CALL_TEXT, "remove"))
    assert content == CALL_TEXT[:7]          # only the first chunk ('<tool_c' here; '<tool_call>\n{"' live)
    assert "Bash" in thinking                 # the rest leaked into "thinking"


@pytest.mark.parametrize("text", ["I don’t have a Bash tool available to run `echo hi`.", CALL_TEXT, "短答。"])
def test_normal_format_unchanged(text, monkeypatch):
    new = collect(normal_stream(text))
    monkeypatch.setenv("NOTION_STREAM_SHADOW", "0")
    old = collect(normal_stream(text))
    assert new == old and new[0] == text


def test_previous_attempt_text_kept_once():
    # attempt 1 streamed visible text before being retried: UI shows prev text + new value
    lines = PREFIX + [P({"o": "a", "p": V + "/-", "v": {"type": "text", "content": "Let me check. "}}),
                      P({"o": "a", "p": V + "/-", "v": {"type": "tool_use", "content": "{}"}}),
                      P({"o": "a", "p": "/s/5/previousAttemptValues/-", "v": [{"type": "text", "content": "Let me check. "},
                                                                             {"type": "tool_use", "content": "{}"}]}),
                      P({"o": "p", "p": V, "v": [{"type": "text", "content": "<tool_c"}]}),
                      P({"o": "x", "p": V + "/0/content", "v": CALL_TEXT[7:]})] + SUFFIX
    content, _ = collect(lines)
    assert content == "Let me check. " + CALL_TEXT


def test_live_capture_unchanged(monkeypatch):
    new = collect(_LIVE)
    monkeypatch.setenv("NOTION_STREAM_SHADOW", "0")
    assert collect(_LIVE) == new and new[0] == "I don’t have a Bash tool available to run `echo hi`."


def test_shadow_errors_fall_back_safely():
    lines = [P({"o": "x", "p": "/s/9/value/0/content", "v": "orphan"})]  # no patch-start, bad index
    content, _ = collect(lines)
    assert content == "orphan"  # old behaviour, no reconcile on an unusable shadow


def test_missing_tail():
    assert _missing_tail("The music", "The music mix didn't") == " mix didn't"
    assert _missing_tail("a b", "a  b c") == " c"
    assert _missing_tail("abc", "xyz") == ""
    assert _missing_tail("abc", "abc") == ""
    assert _missing_tail("", "full") == "full"


def test_shadow_ops():
    sh = _ShadowState()
    sh.reset([{"type": "x", "value": [1, 2, 3]}])
    assert sh.apply({"o": "s", "r": 2, "v": [9]}, "/s/0/value") and sh.doc["s"][0]["value"] == [1, 9]
    assert sh.apply({"o": "m", "f": "/s/0/value/0"}, "/s/0/moved") and sh.doc["s"][0]["moved"] == 1
    assert sh.apply({"o": "c", "f": "/s/0/value"}, "/s/0/copy") and sh.doc["s"][0]["copy"] == [9]
    assert sh.apply({"o": "t", "v": 1}, "/s/0/moved")
    assert not sh.apply({"o": "zz"}, "/s/0") and not sh.usable


# ---- end to end through the tool-mode paths (stream and non-stream) ----
class StreamClient:
    """Fake Notion client whose stream comes from parse_stream over a fixture."""
    def __init__(self, lines):
        self.lines = lines
    def stream_response(self, transcript, thread_id=None, meta=None, **kw):
        def gen():
            yield from parse_stream(Resp(self.lines))
        return gen()
    def inference_text_by_trace(self, *a):
        raise AssertionError("record rescue must not be needed")


def _sse(gen):
    out = []
    for chunk in gen:
        for line in chunk.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                out.append(json.loads(line[6:]))
    return out


@pytest.mark.parametrize("mode", ["remove", "suffix"])
def test_tool_mode_stream_gets_call(mode):
    gen = chat._run_tool_attempts(StreamClient(retry_stream(CALL_TEXT, mode)), [{"id": "x", "type": "user"}],
                                  include_usage=True, account="a", tools=TOOLS, parallel=True, mode="auto", meta={})
    ev = _sse(chat._tool_mode_stream(gen, "id", "m"))
    calls = [tc for e in ev for ch in (e.get("choices") or []) for tc in (ch.get("delta") or {}).get("tool_calls") or []]
    assert calls and calls[0]["function"]["name"] == "Bash"
    assert not any("error" in e for e in ev)


@pytest.mark.parametrize("mode", ["remove", "replace"])
def test_tool_mode_non_stream_gets_call(mode):
    gen = chat._run_tool_attempts(StreamClient(retry_stream(CALL_TEXT, mode)), [{"id": "x", "type": "user"}],
                                  include_usage=True, account="a", tools=TOOLS, parallel=True, mode="auto", meta={})
    resp = chat._tool_mode_non_stream(gen, "id", "m")
    body = json.loads(resp.body) if hasattr(resp, "body") else resp
    msg = body["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "Bash"
    assert "<tool_call" not in (msg.get("content") or "")


def test_plain_stream_generator_full_text():
    text = "The music mix didn't update anything because I ran it without -y. Re-running now."
    items = parse_stream(Resp(retry_stream(text, "remove")))
    first = next(items)
    out = first["text"] if first["type"] == "content" else ""
    out += "".join(i["text"] for i in items if i["type"] == "content")
    from app.lang_sanitizer import clean_lang
    assert clean_lang(out) == text


# ---- short-reply reconcile against the thread record (traceId) ----
from app.notion_client import NotionOpusAPI


def _api(record_text, finished=True, items=None):
    api = NotionOpusAPI.__new__(NotionOpusAPI)
    api.identity_redactor = None
    api.account_key = "a"
    api.current_thread_id = "t1"
    api.fail_streak = 0
    api.reads = 0
    def raw(transcript, thread_id=None, confirm_step_ids=None, meta=None):
        meta["thread_id"], meta["trace_id"] = "t1", "tr1"
        yield from items
    api._stream_response_raw = raw
    api._retry_rate_limited = lambda src, *a, **k: src
    api._silent_close_guard = lambda src, meta: src
    def rec(tid, trace):
        api.reads += 1
        assert (tid, trace) == ("t1", "tr1")
        return record_text, finished
    api.inference_text_by_trace = rec
    return api


def _run_once(api, meta):
    return list(api._stream_response_once([{"type": "user", "value": [["hi"]]}], meta=meta))


def test_short_reply_completed_from_record(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    full = "The music mix didn't update anything because I ran it without -y."
    api = _api(full, items=[{"type": "content", "text": "The music"},
                            {"type": "usage", "usage": {"outputTokens": 1318}}])
    meta = {"_rl": {}}
    out = _run_once(api, meta)
    text = "".join(i["text"] for i in out if i["type"] == "content")
    assert text == full and api.reads == 1 and meta["_rl"]["short_reconcile"] == "completed"
    assert out[-1]["type"] == "usage"


def test_short_reply_not_checked_when_plausible(monkeypatch):
    api = _api("whatever", items=[{"type": "content", "text": "继续"}, {"type": "usage", "usage": {"outputTokens": 20}}])
    _run_once(api, {})
    assert api.reads == 0
    api = _api("x" * 900, items=[{"type": "content", "text": "y" * 500}, {"type": "usage", "usage": {"outputTokens": 9000}}])
    _run_once(api, {})
    assert api.reads == 0  # not short


def test_short_reply_record_diverges_or_flag_off(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    api = _api("Something else entirely", items=[{"type": "content", "text": "The music"},
                                                 {"type": "usage", "usage": {"outputTokens": 1318}}])
    meta = {"_rl": {}}
    out = _run_once(api, meta)
    assert "".join(i["text"] for i in out if i["type"] == "content") == "The music"
    assert meta["_rl"]["short_reconcile"] == "match"
    monkeypatch.setenv("NOTION_SHORT_RECONCILE", "off")
    api = _api("The music and more", items=[{"type": "content", "text": "The music"},
                                            {"type": "usage", "usage": {"outputTokens": 1318}}])
    _run_once(api, {})
    assert api.reads == 0


def test_short_reply_unfinished_record_ignored(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    api = _api("The music and more", finished=False, items=[{"type": "content", "text": "The music"},
                                                            {"type": "usage", "usage": {"outputTokens": 1318}}])
    out = _run_once(api, {})
    assert "".join(i["text"] for i in out if i["type"] == "content") == "The music" and api.reads == 2
