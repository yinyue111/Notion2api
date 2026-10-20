"""Replay of the 2026-10-19 18:16 (Taipei) "先确" cut: compaction step + native tool_use
rejected (attempt moved to previousAttemptValues) + a shadow op error. The stream only
showed '先确' as text; the rest was routed to thinking. Fix: degraded streams re-read the
inference record by traceId and append the missing tail (incl. the <tool_call>)."""
import json
import pytest
from test_stream_shadow import PREFIX, SUFFIX, P, V, Resp, chunks, _sse
from app.stream_parser import parse_stream, _ShadowState
from app.notion_client import NotionOpusAPI
from app.api import chat
from app.tool_emulation import normalize_tools

TOOLS = normalize_tools([{"type": "function", "function": {"name": "Bash", "parameters": {}}}])
CMD = ('N="/d/工作核心/接单/26.10/200-8条视频剪辑"; ls -la "$N"; for d in "$N"/*/; do echo "== $d"; '
       'ls "$d" | head -50; done; find "$N" -maxdepth 2 -type f \\( -name "*.mp4" -o -name "*.mov" \\) | wc -l')
CALL = '<tool_call>\n' + json.dumps({"name": "Bash", "arguments": {"command": CMD, "description": "List"}}, ensure_ascii=False) + '\n</tool_call>'
TEXT = "先确认新目录结构和本轮素材。\n\n" + CALL


def stream_1816(shadow_error=True, resync=False):
    L = list(PREFIX)
    L.append(P({"o": "a", "p": "/s/-", "v": {"id": "sum-1", "type": "agent-transcript-summary"}}))  # compaction
    if shadow_error:
        L.append(P({"o": "x", "p": "/s/6/summary", "v": "private summary text"}))  # op the shadow can't apply
    if resync:
        import copy
        L.append({"type": "patch-sync", "data": {"s": []}})  # placeholder, replaced below
    L += [P({"o": "a", "p": V + "/-", "v": {"type": "thinking", "content": ""}}),
          P({"o": "a", "p": V + "/-", "v": {"type": "tool_use", "content": "{\"command\": "}}),
          P({"o": "x", "p": V + "/1/content", "v": "\"N='/d/x'\"}"}),
          P({"o": "a", "p": "/s/5/previousAttemptValues/-",
             "v": [{"type": "thinking", "content": ""}, {"type": "tool_use", "content": "{\"command\": \"N='/d/x'\"}"}]}),
          P({"o": "r", "p": V + "/1"}), P({"o": "r", "p": V + "/0"}),
          P({"o": "a", "p": V + "/-", "v": {"type": "thinking", "content": ""}})]
    cs = chunks(TEXT, 2)  # first visible chunk: '先确'
    L.append(P({"o": "a", "p": V + "/-", "v": {"type": "text", "content": cs[0]}}))
    L += [P({"o": "x", "p": V + "/1/content", "v": c}) for c in cs[1:]]
    return L + SUFFIX


def _api(lines, record=TEXT, finished=True, out_tok=385):
    api = NotionOpusAPI.__new__(NotionOpusAPI)
    api.identity_redactor = None
    api.account_key = "a"
    api.current_thread_id = "t1"
    api.fail_streak = 0
    api.reads = []
    def raw(transcript, thread_id=None, confirm_step_ids=None, meta=None):
        meta["thread_id"], meta["trace_id"] = "t1", "tr1"
        for it in parse_stream(Resp(lines), meta=meta):
            if it["type"] == "usage":
                it = {"type": "usage", "usage": {"outputTokens": out_tok}}
            yield it
    api._stream_response_raw = raw
    api._retry_rate_limited = lambda src, *a, **k: src
    api._silent_close_guard = lambda src, meta: src
    def rec(tid, trace, timeout=15):
        api.reads.append(timeout)
        assert (tid, trace) == ("t1", "tr1")
        return record, finished
    api.inference_text_by_trace = rec
    return api


def _text(items, typ="content"):
    return "".join(i.get("text", "") for i in items if i.get("type") == typ)


def test_parser_alone_reproduces_cut():
    meta = {}
    items = list(parse_stream(Resp(stream_1816()), meta=meta))
    from app.lang_sanitizer import clean_lang
    assert clean_lang(_text(items)).strip() == "先确"           # before the fix: the whole visible answer
    assert "Bash" in _text(items, "thinking")                   # rest misrouted to thinking
    assert meta["_stream_shadow"]["errors"] == 1 and meta["_stream_shadow"]["retried_steps"] == 1


@pytest.mark.parametrize("degraded_fix", ["off", "on"])
def test_1816_before_after(monkeypatch, degraded_fix):
    # prod counted every misrouted thinking char (298 for 385 output tokens), so the old
    # threshold 385 <= 3*(2+298)+40 skipped the record read. NOTION_SHORT_RECONCILE_DEGRADED
    # =off is exactly the pre-fix code path.
    monkeypatch.setenv("NOTION_SCRUB_THINKING", "0")
    monkeypatch.setenv("NOTION_SHORT_RECONCILE_DEGRADED", degraded_fix)
    api = _api(stream_1816(), out_tok=385)
    gen = chat._run_tool_attempts(ApiClient(api), [{"id": "x", "type": "user"}], include_usage=True,
                                  account="a", tools=TOOLS, parallel=True, mode="auto", meta={})
    resp = chat._tool_mode_non_stream(gen, "id", "m")
    msg = (json.loads(resp.body) if hasattr(resp, "body") else resp)["choices"][0]["message"]
    if degraded_fix == "off":
        assert msg["content"].strip() == "先确" and not msg.get("tool_calls") and api.reads == []
    else:
        assert msg["content"].strip() == "先确认新目录结构和本轮素材。"
        assert msg["tool_calls"][0]["function"]["name"] == "Bash" and len(api.reads) == 1


def test_degraded_stream_completed_from_record():
    api = _api(stream_1816())
    meta = {"_rl": {}}
    out = list(api._stream_response_once([{"type": "user", "value": [["hi"]]}], meta=meta))
    text = _text(out)
    assert text.strip() == TEXT.strip()                 # full text + tool_call, '先确' not duplicated
    assert text.count("先确") == 1 and text.count("<tool_call>") == 1
    assert len(api.reads) == 1 and api.reads[0] <= 8    # single bounded re-read
    assert meta["_rl"]["short_reconcile"] == "degraded:completed"
    assert out[-1]["type"] == "usage"


def test_degraded_unfinished_or_diverging_record_is_failsafe():
    api = _api(stream_1816(), finished=False)
    out = list(api._stream_response_once([{"type": "user", "value": [["hi"]]}], meta={}))
    assert _text(out).strip() == "先确" and len(api.reads) == 1
    api = _api(stream_1816(), record="另一个回复完全不同")   # e.g. a history turn: never emitted
    out = list(api._stream_response_once([{"type": "user", "value": [["hi"]]}], meta={}))
    assert _text(out).strip() == "先确"
    api = _api(stream_1816())
    def boom(*a, **k):
        raise TimeoutError("read timeout")
    api.inference_text_by_trace = boom
    out = list(api._stream_response_once([{"type": "user", "value": [["hi"]]}], meta={}))
    assert _text(out).strip() == "先确"


def test_record_thinking_never_emitted_as_text():
    # inference_text_by_trace only returns text parts (thinking/tool_use excluded)
    api = NotionOpusAPI.__new__(NotionOpusAPI)
    api.identity_redactor = None
    api.space_id = "sp"
    step = {"type": "agent-inference", "traceId": "tr1", "finishedAt": 1,
            "previousAttemptValues": [[{"type": "thinking", "content": "SECRET THOUGHT"},
                                       {"type": "tool_use", "content": "{}"}]],
            "value": [{"type": "thinking", "content": "MORE THOUGHT"}, {"type": "text", "content": TEXT}]}
    calls = []
    def post(ep, body, timeout=15):
        calls.append(timeout)
        if body["requests"][0]["pointer"]["table"] == "thread":
            return 200, {"recordMap": {"thread": {"t1": {"value": {"messages": ["m1"]}}}}}
        return 200, {"recordMap": {"thread_message": {"m1": {"value": {"step": step}}}}}
    api.api_post = post
    text, fin = api.inference_text_by_trace("t1", "tr1", timeout=5)
    assert fin and text == TEXT and "THOUGHT" not in text and calls == [5, 5]


def test_healthy_stream_unchanged(monkeypatch):
    # no shadow error, no retry: old heuristics apply (no record read for a plausible reply)
    from test_stream_shadow import normal_stream
    api = _api(normal_stream("短答。"), record="短答。", out_tok=20)
    meta = {}
    out = list(api._stream_response_once([{"type": "user", "value": [["hi"]]}], meta=meta))
    assert api.reads == [] and meta["_stream_shadow"] == {"errors": 0, "retried_steps": 0, "usable": True, "disabled": False}


def test_shadow_disabled_marks_degraded(monkeypatch):
    monkeypatch.setenv("NOTION_STREAM_SHADOW", "0")
    api = _api(stream_1816(shadow_error=False))
    meta = {}
    out = list(api._stream_response_once([{"type": "user", "value": [["hi"]]}], meta=meta))
    assert meta["_stream_shadow"]["disabled"] and _text(out).strip() == TEXT.strip()


class ApiClient:
    def __init__(self, api):
        self.api = api
    def stream_response(self, transcript, thread_id=None, meta=None, **kw):
        return self.api._stream_response_once(transcript, meta=meta)


def test_tool_mode_stream_gets_real_tool_call():
    gen = chat._run_tool_attempts(ApiClient(_api(stream_1816())), [{"id": "x", "type": "user"}],
                                  include_usage=True, account="a", tools=TOOLS, parallel=True, mode="auto", meta={})
    ev = _sse(chat._tool_mode_stream(gen, "id", "m"))
    calls = [tc for e in ev for ch in (e.get("choices") or []) for tc in (ch.get("delta") or {}).get("tool_calls") or []]
    assert calls and calls[0]["function"]["name"] == "Bash"
    args = "".join(tc["function"].get("arguments") or "" for tc in calls)
    assert json.loads(args)["command"] == CMD
    content = "".join((ch.get("delta") or {}).get("content") or "" for e in ev for ch in (e.get("choices") or []))
    assert "先确认新目录结构和本轮素材" in content and content.count("先确") == 1 and "<tool_call" not in content


def test_tool_mode_non_stream_gets_real_tool_call():
    gen = chat._run_tool_attempts(ApiClient(_api(stream_1816())), [{"id": "x", "type": "user"}],
                                  include_usage=True, account="a", tools=TOOLS, parallel=True, mode="auto", meta={})
    resp = chat._tool_mode_non_stream(gen, "id", "m")
    body = json.loads(resp.body) if hasattr(resp, "body") else resp
    msg = body["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "Bash"
    assert "<tool_call" not in (msg.get("content") or "") and (msg.get("content") or "").count("先确") == 1


def test_live_splitter_stream_gets_real_tool_call():
    api = _api(stream_1816())
    g = api._stream_response_once([{"type": "user", "value": [["hi"]]}], meta={})
    first = next(g)
    ev = _sse(chat._create_tool_stream_generator("id", "m", first, g, TOOLS, True))
    calls = [tc for e in ev for ch in (e.get("choices") or []) for tc in (ch.get("delta") or {}).get("tool_calls") or []]
    assert calls and calls[0]["function"]["name"] == "Bash"


# ---- Fix 2: diagnostics + resync ----
def test_first_failing_op_logged_without_values(caplog):
    import logging
    sh = _ShadowState()
    sh.reset([{"id": "step-a", "type": "agent-transcript-summary"}])
    recs = []
    from app import stream_parser as sp
    orig = sp.logger.warning
    sp.logger.warning = lambda msg, *a, **k: recs.append((msg, k.get("extra", {}).get("request_info")))
    try:
        assert not sh.apply({"o": "x", "v": "TOP SECRET USER TEXT"}, "/s/0/summary")
        assert not sh.apply({"o": "x", "v": "again"}, "/s/0/summary")
    finally:
        sp.logger.warning = orig
    assert len(recs) == 1  # first failure only
    info = recs[0][1]
    assert info["event"] == "stream_shadow_op_failed" and info["op"] == "x" and info["path"] == "/s/0/summary"
    assert info["step_id"] == "step-a" and info["step_type"] == "agent-transcript-summary" and info["error"]
    assert "SECRET" not in json.dumps(info, ensure_ascii=False)
    assert sh.total_errors == 2


def test_resync_from_full_snapshot(monkeypatch):
    sh = _ShadowState()
    sh.reset([{"type": "agent-inference", "value": []}])
    assert not sh.apply({"o": "x", "v": "a"}, "/s/3/value")
    assert not sh.usable
    sh.reset([{"type": "agent-inference", "value": [{"type": "text", "content": "hi"}]}])
    assert sh.usable and sh.resyncs == 1 and sh.total_errors == 1
    assert sh.apply({"o": "x", "v": "!"}, "/s/0/value/0/content") and sh.doc["s"][0]["value"][0]["content"] == "hi!"
    monkeypatch.setenv("NOTION_STREAM_SHADOW_RESYNC", "off")
    sh2 = _ShadowState()
    sh2.reset([])
    sh2.apply({"o": "x", "v": "a"}, "/s/3/value")
    sh2.reset([])
    assert not sh2.usable


def test_resync_in_stream_still_flags_degraded():
    # error, then a patch-sync with the full state -> shadow usable again, but the request
    # is still flagged (total_errors) so the record re-read runs
    lines = stream_1816()
    i = next(k for k, l in enumerate(lines) if l.get("type") == "patch" and l["v"][0].get("p") == "/s/6/summary")
    meta = {}
    list(parse_stream(Resp(lines[:i + 1] + [{"type": "patch-sync", "data": {"s": "bad"}}] + lines[i + 1:]), meta=meta))
    assert meta["_stream_shadow"]["errors"] >= 1
