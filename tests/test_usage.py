import os
os.environ.setdefault("NOTION_ACCOUNTS", "[{\"token_v2\": \"test\", \"space_id\": \"s\", \"user_id\": \"u\"}]")
import json
import pytest
from app.stream_parser import parse_stream
from app.api.chat import _UsageCollector, _openai_usage_from_raw, _build_usage_chunk
from app.notion_client import NotionUpstreamError
from app.api.chat import _classify_upstream_error


class FakeResp:
    def __init__(self, lines):
        self.lines = [json.dumps(l) if not isinstance(l, str) else l for l in lines]

    def iter_lines(self, decode_unicode=True):
        yield from self.lines


def step_add(sid, typ="agent-inference"):
    return {"type": "patch", "v": [{"o": "a", "p": "/s/-", "v": {"id": sid, "type": typ, "value": [{"type": "text", "content": ""}]}}]}


def usage_patch(idx, i, o, r, c):
    return {"type": "patch", "v": [
        {"o": "a", "p": f"/s/{idx}/inputTokens", "v": i},
        {"o": "a", "p": f"/s/{idx}/outputTokens", "v": o},
        {"o": "a", "p": f"/s/{idx}/cachedTokensRead", "v": r},
        {"o": "a", "p": f"/s/{idx}/cachedTokensCreated", "v": c}]}


def record_map(steps):
    return {"type": "record-map", "recordMap": {"thread_message": {
        s["id"]: {"value": {"value": {"step": s}}} for s in steps}}}


def usage_of(lines):
    ev = [e for e in parse_stream(FakeResp(lines)) if e.get("type") == "usage"]
    return ev[-1]["usage"] if ev else None


def test_patch_usage_multi_step_summed():
    u = usage_of([step_add("a"), usage_patch(4, 100, 10, 80, 5), step_add("b"), usage_patch(6, 200, 20, 150, 0)])
    assert u == {"inputTokens": 300, "outputTokens": 30, "cachedTokensRead": 230, "cachedTokensCreated": 5}


def test_record_map_preferred_and_filters_foreign_steps():
    lines = [step_add("a"), usage_patch(4, 1, 1, 1, 1),
             record_map([{"id": "a", "type": "agent-inference", "inputTokens": 35990, "outputTokens": 12, "cachedTokensRead": 35360},
                         {"id": "old", "type": "agent-inference", "inputTokens": 999, "outputTokens": 9}])]
    assert usage_of(lines) == {"inputTokens": 35990, "outputTokens": 12, "cachedTokensRead": 35360}


def test_no_usage_upstream_emits_nothing():
    assert usage_of([step_add("a")]) is None


def test_openai_mapping_never_invents_cache():
    assert _openai_usage_from_raw(None) == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    u = _openai_usage_from_raw({"inputTokens": 50, "outputTokens": 5})
    assert "prompt_tokens_details" not in u and u["total_tokens"] == 55
    u = _openai_usage_from_raw({"inputTokens": 26345, "outputTokens": 30, "cachedTokensRead": 25858, "cachedTokensCreated": 695})
    assert u == {"prompt_tokens": 27040, "completion_tokens": 30, "total_tokens": 27070,
                 "prompt_tokens_details": {"cached_tokens": 25858}}


def test_collector_swallows_usage_and_chunk():
    c = _UsageCollector(iter([{"type": "content", "text": "hi"}, {"type": "usage", "usage": {"inputTokens": 3, "outputTokens": 1}}]))
    assert list(c) == [{"type": "content", "text": "hi"}]
    chunk = json.loads(_build_usage_chunk("id", "m", c)[6:])
    assert chunk["choices"] == [] and chunk["usage"]["prompt_tokens"] == 3
    c2 = _UsageCollector(iter([]), include_usage=False)
    assert _build_usage_chunk("id", "m", c2) == ""


def test_error_event_passthrough():
    ev = list(parse_stream(FakeResp([{"type": "error", "message": "Model overloaded"}])))
    assert ev[0]["type"] == "upstream_error" and ev[0]["message"] == "Model overloaded"
    info = _classify_upstream_error(NotionUpstreamError("x", notion_error_message="Model overloaded"))
    assert info["status"] == 502 and "Model overloaded" in info["message"]
