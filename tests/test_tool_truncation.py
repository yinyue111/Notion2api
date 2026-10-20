"""Tool-call truncation (2026-10-18 MaxReglore incident): a stream that stops at
'<tool_call>\\n{' must never reach the client as text with finish_reason=stop."""
import json
import pytest
from app.api import chat
from app.tool_emulation import normalize_tools, ToolStreamSplitter, strip_tool_markup, has_unparsed_tool_markup
from app.notion_client import _RawTail

TOOLS = normalize_tools([{"type": "function", "function": {"name": "Bash", "parameters": {}}},
                         {"type": "function", "function": {"name": "Read", "parameters": {}}}])
FULL = '<tool_call>\n{"name": "Bash", "arguments": {"command": "ls D:/工作核心"}}\n</tool_call>'
LEAKS = ['<tool_call>\n{', '<tool_call>\n{"']


class FakeClient:
    """Fake Notion: each script = list of stream items; record = what the thread record holds."""
    def __init__(self, scripts, record=None, record_finished=True):
        self.scripts = list(scripts); self.calls = 0; self.record = record; self.fin = record_finished
        self.record_reads = 0; self.thread_ids = []
    def stream_response(self, transcript, thread_id=None, meta=None, **kw):
        self.calls += 1; self.thread_ids.append(thread_id)
        script = self.scripts.pop(0)
        n = self.calls
        if meta is not None:
            meta["thread_id"] = thread_id or f"t{n}"; meta["trace_id"] = f"tr{n}"
        def gen():
            for it in script:
                yield it
            yield {"type": "usage", "usage": {"inputTokens": 100, "outputTokens": 10}}
        return gen()
    def inference_text_by_trace(self, tid, trace):
        self.record_reads += 1
        return (self.record or ""), self.fin


def run(client, thread_id=None, meta=None):
    res, ticks = None, 0
    for ev in chat._run_tool_attempts(client, [{"id": "x", "type": "user"}], include_usage=True, account="a",
                                      tools=TOOLS, parallel=True, mode="auto", thread_id=thread_id,
                                      fallback_transcript=[{"id": "y", "type": "user"}], meta=meta):
        if isinstance(ev, tuple):
            res = ev[1]
        elif ev == "tick":
            ticks += 1
    return res, ticks


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(chat.time, "sleep", lambda s: None)
    monkeypatch.delenv("NOTION_TOOL_TRUNC_FIX", raising=False)
    monkeypatch.delenv("NOTION_TOOL_TRUNC_RETRIES", raising=False)


@pytest.mark.parametrize("leak", LEAKS)
def test_rescued_from_thread_record(leak):
    c = FakeClient([[{"type": "content", "text": leak}]], record=FULL)
    meta = {"_rl": {}}
    r, ticks = run(c, meta=meta)
    assert r["calls"] and r["calls"][0]["function"]["name"] == "Bash" and "error" not in r
    assert c.calls == 1 and c.record_reads == 1  # no extra Notion inference
    assert meta["_rl"]["trunc_rescue"] == "record" and meta["_rl"]["nothing_parsed"] == 1
    assert meta["_rl"]["content_chars"] == len(leak)


@pytest.mark.parametrize("leak", LEAKS)
def test_retry_then_success(leak):
    c = FakeClient([[{"type": "content", "text": leak}], [{"type": "content", "text": "ok " + FULL}]], record="")
    meta = {"_rl": {}}
    r, _ = run(c, thread_id="main", meta=meta)
    assert r["calls"][0]["function"]["name"] == "Bash" and r["text"] == "ok"
    assert c.calls == 2 and c.thread_ids == ["main", None]  # retry on a fresh thread
    assert meta["_rl"]["trunc_retry"] == 1
    # usage convention: sum of every completed attempt (like refusal / hedge retries)
    assert r["collector"].raw["inputTokens"] == 200


@pytest.mark.parametrize("leak", LEAKS)
def test_gives_up_with_error_never_text(leak, monkeypatch):
    c = FakeClient([[{"type": "content", "text": "Next I'll check.\n\n" + leak}]] * 3, record="")
    meta = {"_rl": {}}
    r, _ = run(c, meta=meta)
    assert r["error"] == chat.TRUNC_CODE and r["calls"] == []
    assert "<tool_call" not in r["text"] and r["text"] == "Next I'll check."
    assert c.calls == 3  # 1 + NOTION_TOOL_TRUNC_RETRIES(2)
    assert meta["_rl"]["error_code"] == chat.TRUNC_CODE and meta["_rl"]["trunc_retry"] == 2


def test_retry_budget_env(monkeypatch):
    monkeypatch.setenv("NOTION_TOOL_TRUNC_RETRIES", "0")
    c = FakeClient([[{"type": "content", "text": LEAKS[1]}]], record="")
    r, _ = run(c)
    assert r["error"] == chat.TRUNC_CODE and c.calls == 1


def test_flag_off_keeps_old_behaviour(monkeypatch):
    monkeypatch.setenv("NOTION_TOOL_TRUNC_FIX", "0")
    c = FakeClient([[{"type": "content", "text": LEAKS[1]}]], record=FULL)
    r, _ = run(c)
    assert r["text"] == LEAKS[1] and "error" not in r and c.record_reads == 0


def test_unfinished_record_not_used():
    c = FakeClient([[{"type": "content", "text": LEAKS[0]}], [{"type": "content", "text": FULL}]],
                   record=FULL[:30], record_finished=False)
    r, ticks = run(c)
    assert r["calls"] and c.calls == 2 and ticks >= 2  # keepalive ticks while waiting on the record


def test_normal_paths_untouched():
    c = FakeClient([[{"type": "content", "text": "plain answer, no tools"}]])
    r, _ = run(c)
    assert r["text"] == "plain answer, no tools" and c.record_reads == 0
    c = FakeClient([[{"type": "content", "text": FULL}]])
    r, _ = run(c)
    assert r["calls"] and c.record_reads == 0
    fenced = "Example:\n```\n<tool_call>{\"name\":\"Bash\"\n```\ndone"
    c = FakeClient([[{"type": "content", "text": fenced}]])
    r, _ = run(c)
    assert r["text"] == fenced and c.record_reads == 0


def _sse(gen):
    out = []
    for chunk in gen:
        for line in chunk.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                out.append(json.loads(line[6:]))
    return out


@pytest.mark.parametrize("leak", LEAKS)
def test_tool_mode_stream_emits_error_not_stop(leak):
    c = FakeClient([[{"type": "content", "text": "Checking.\n" + leak}]] * 3, record="")
    events = _sse(chat._tool_mode_stream(chat._run_tool_attempts(
        c, [{"id": "x", "type": "user"}], include_usage=True, account="a", tools=TOOLS, parallel=True,
        mode="auto", meta={}), "id1", "m"))
    text = "".join((e.get("choices") or [{}])[0].get("delta", {}).get("content") or "" for e in events if e.get("choices"))
    assert "<tool_call" not in text and "Checking." in text
    assert any(e.get("error", {}).get("code") == "UPSTREAM_TRUNCATED" for e in events)
    assert not any((e.get("choices") or [{}])[0].get("finish_reason") == "stop" for e in events if e.get("choices"))


@pytest.mark.parametrize("leak", LEAKS)
def test_non_stream_raises_502(leak):
    c = FakeClient([[{"type": "content", "text": leak}]] * 3, record="")
    gen = chat._run_tool_attempts(c, [{"id": "x", "type": "user"}], include_usage=True, account="a",
                                  tools=TOOLS, parallel=True, mode="auto", meta={})
    with pytest.raises(chat.NotionUpstreamError) as ei:
        chat._tool_mode_non_stream(gen, "id", "m")
    assert ei.value.code == "UPSTREAM_TRUNCATED" and ei.value.retriable is False
    info = chat._classify_upstream_error(ei.value)
    assert info["status"] == 502 and info["code"] == "UPSTREAM_TRUNCATED"


@pytest.mark.parametrize("leak", LEAKS)
def test_stream_splitter_never_emits_partial(leak):
    for step in (1, 2, 5, 50):
        sp = ToolStreamSplitter()
        text = "Plan first.\n" + leak
        out = "".join(sp.feed(text[i:i + step]) for i in range(0, len(text), step))
        tail, calls = sp.finish(TOOLS)
        assert calls == [] and sp.truncated and "<tool" not in out + tail
        assert (out + tail).strip() == "Plan first."


@pytest.mark.parametrize("leak", LEAKS)
def test_create_tool_stream_generator_error(leak):
    items = iter([{"type": "content", "text": "Hi.\n"}, {"type": "content", "text": leak}])
    first = next(items)
    events = _sse(chat._create_tool_stream_generator("id", "m", first, items, TOOLS, True))
    assert any(e.get("error", {}).get("code") == "UPSTREAM_TRUNCATED" for e in events)
    text = "".join((e.get("choices") or [{}])[0].get("delta", {}).get("content") or "" for e in events if e.get("choices"))
    assert "<tool_call" not in text


def test_helpers():
    assert strip_tool_markup("a\n<tool_call>\n{") == "a"
    assert has_unparsed_tool_markup(LEAKS[1], TOOLS) and not has_unparsed_tool_markup(FULL, TOOLS)
    t = _RawTail(); t.max_chars = 100
    for i in range(50):
        t.add("x" * 10)
    assert t.chars <= 100 and len(t) == 10
    t.add("y" * 20000)
    assert len(list(t)[-1]) < 17000


def test_suggestion_mode_skipped(monkeypatch):
    class B:
        model = "claude-opus5.5"; stream = False; stream_options = None
    msgs = [{"role": "user", "content": "do x"}, {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "[SUGGESTION MODE: Suggest what the user might naturally type next into Claude Code.]\n\nOr nothing."}]
    r = chat._suggestion_mode_reply(msgs, B(), "id")
    body = json.loads(r.body)
    assert body["choices"][0]["message"]["content"] == "" and body["choices"][0]["finish_reason"] == "stop"
    assert chat._suggestion_mode_reply(msgs[:2] + [{"role": "user", "content": "继续"}], B(), "id") is None
    monkeypatch.setenv("NOTION_SUGGESTION_MODE", "passthrough")
    assert chat._suggestion_mode_reply(msgs, B(), "id") is None
