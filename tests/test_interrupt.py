"""Interrupt-on-abandon (2026-10-20), rescue effort vs NOTION_EFFORT_FLOOR, effort_sent,
lenient shadow remove, hedge wait-for-retry. Live finding: for runInferenceTranscript
(workflow) threads Notion's Stop = saveTransactions update clearing current_inference_id /
current_inference_lease_expiration; sendEventToAgentThread answers 404 'Agent thread not found'."""
import pytest
from app import notion_client as nc
from app.api import chat
from app.notion_client import NotionOpusAPI


class SyncThread:
    def __init__(self, target=None, name=None, daemon=None, args=()):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)


@pytest.fixture(autouse=True)
def sync_threads(monkeypatch):
    monkeypatch.setattr(nc.threading, "Thread", SyncThread)
    NotionOpusAPI._interrupt_seen.clear()
    NotionOpusAPI._dispatch_ts.clear()
    for k in ("NOTION_INTERRUPT_ON_ABANDON", "NOTION_EFFORT_FLOOR", "NOTION_EFFORT_FLOOR_RESCUE",
              "NOTION_HEDGE_WAIT_RETRY", "NOTION_SHADOW_LENIENT_REMOVE"):
        monkeypatch.delenv(k, raising=False)


def fake_api(running=True):
    api = NotionOpusAPI.__new__(NotionOpusAPI)
    api.space_id = "sp"
    api.account_key = "acc1"
    api.calls = []
    def post(ep, body, timeout=20):
        api.calls.append((ep, body))
        if ep == "syncRecordValuesMain":
            tid = body["requests"][0]["pointer"]["id"]
            return 200, {"recordMap": {"thread": {tid: {"value": {"current_inference_id": "inf1" if running else None}}}}}
        return 200, {}
    api.api_post = post
    return api


def writes(api):
    return [b for ep, b in api.calls if ep == "saveTransactions"]


def test_interrupt_clears_current_inference():
    api = fake_api(running=True)
    api.interrupt_thread("t1", "test")
    w = writes(api)
    assert len(w) == 1
    op = w[0]["transactions"][0]["operations"][0]
    assert op["pointer"] == {"table": "thread", "id": "t1", "spaceId": "sp"}
    assert op["command"] == "update" and op["path"] == []
    assert op["args"] == {"current_inference_id": None, "current_inference_lease_expiration": None}


def test_interrupt_skips_when_not_running_and_dedupes():
    api = fake_api(running=False)
    api.interrupt_thread("t1", "test")
    assert writes(api) == []
    api2 = fake_api(running=True)
    api2.interrupt_thread("t1", "again")  # deduped (same thread within NOTION_INTERRUPT_DEDUPE)
    assert api2.calls == []


def test_interrupt_off_switch(monkeypatch):
    monkeypatch.setenv("NOTION_INTERRUPT_ON_ABANDON", "off")
    api = fake_api()
    api.interrupt_thread("t1", "test")
    assert api.calls == []


def test_interrupt_skips_redispatched_thread(monkeypatch):
    api = fake_api()
    t = [100.0]
    monkeypatch.setattr(nc.time, "time", lambda: t[0])
    real = SyncThread.start
    def later(self):  # the client's next request reuses the thread before we get to it
        t[0] += 1
        NotionOpusAPI._note_dispatch("t1")
        real(self)
    monkeypatch.setattr(SyncThread, "start", later)
    api.interrupt_thread("t1", "client_gone")
    assert writes(api) == []


def _raw_api(items, monkeypatch):
    api = fake_api()
    seen = []
    api.interrupt_thread = lambda tid, reason, **kw: seen.append((tid, reason))
    def impl(transcript, thread_id, confirm_step_ids=None, meta=None):
        meta["thread_id"] = "t9"
        for it in items:
            yield it
    api._stream_response_raw_impl = impl
    import app.account_pool as ap
    monkeypatch.setattr(ap, "inflight_start", lambda c: 1)
    monkeypatch.setattr(ap, "inflight_end", lambda c, tok: None)
    return api, seen


def test_raw_stream_closed_early_interrupts(monkeypatch):
    api, seen = _raw_api([{"type": "content", "text": "a"}, {"type": "content", "text": "b"}], monkeypatch)
    meta = {}
    g = api._stream_response_raw([{"type": "user"}], meta=meta)
    next(g)
    g.close()
    assert seen == [("t9", "closed")]


def test_raw_stream_completed_does_not_interrupt(monkeypatch):
    api, seen = _raw_api([{"type": "content", "text": "a"}], monkeypatch)
    assert list(api._stream_response_raw([{"type": "user"}], meta={})) == [{"type": "content", "text": "a"}]
    assert seen == []


def test_raw_stream_cancelled_meta_interrupts(monkeypatch):
    api, seen = _raw_api([{"type": "content", "text": "a"}], monkeypatch)
    meta = {"_cancelled": True}
    list(api._stream_response_raw([{"type": "user"}], meta=meta))
    assert seen == [("t9", "cancelled")]


def test_abort_upstream_interrupts():
    api = fake_api()
    seen = []
    api.interrupt_thread = lambda tid, reason, **kw: seen.append((tid, reason))
    meta = {"_n_client": api, "thread_id": "t5"}
    chat._abort_upstream(meta)
    assert meta["_cancelled"] and seen == [("t5", "client_gone")]


def test_recover_silent_interrupts_when_nothing_recovered(monkeypatch):
    api = fake_api()
    seen = []
    api.interrupt_thread = lambda tid, reason, **kw: seen.append((tid, reason))
    api._archive_ghost = lambda tid, meta: None
    def poll(tid, deadline, exclude=None, cancel=None):
        cancel["_ghost"] = "no_text"
        return ""
    api._poll_thread_inference = poll
    tr = [{"type": "config", "value": {"model": "claude-sonnet5.5", "reasoningEffort": "medium"}}]
    assert api._recover_silent(tr, "t7", {}, set()) == ""
    assert seen == [("t7", "ghost:no_text")]


# ---- effort floor vs rescue ----
def _cfg(effort, rescue=False):
    b = {"type": "config", "value": {"model": "claude-opus5.5", "reasoningEffort": effort}}
    return nc.mark_rescue_effort(b, effort) if rescue else b


def _sent(api, block):
    out = api._to_notion_transcript([block])
    return out[0]["value"]


def test_floor_applies_to_primary_not_rescue(monkeypatch):
    monkeypatch.setenv("NOTION_EFFORT_FLOOR", "albuquerque-quinn:high")
    api = fake_api()
    api._gpt_answers = lambda t: False
    v = _sent(api, _cfg("medium"))
    assert v["reasoningEffort"] == "high"
    v = _sent(api, _cfg("medium", rescue=True))
    assert v["reasoningEffort"] == "medium"
    assert nc.RESCUE_EFFORT_KEY not in v  # marker never sent to Notion


def test_floor_rescue_switch(monkeypatch):
    monkeypatch.setenv("NOTION_EFFORT_FLOOR", "albuquerque-quinn:high")
    monkeypatch.setenv("NOTION_EFFORT_FLOOR_RESCUE", "on")
    api = fake_api()
    api._gpt_answers = lambda t: False
    assert _sent(api, _cfg("medium", rescue=True))["reasoningEffort"] == "high"


def test_lowered_effort_is_rescue():
    out = chat._lowered_effort([{"type": "config", "value": {"model": "claude-opus5.5", "reasoningEffort": "high"}}])
    assert out[0]["value"]["reasoningEffort"] == "medium"
    assert out[0]["value"][nc.RESCUE_EFFORT_KEY] is True


# ---- hedge: finished-without-text while Notion re-runs the step ----
def test_orig_finished_dead():
    assert nc._orig_finished_dead(True, "", False, 7562) is True       # finished, idle: dead
    assert nc._orig_finished_dead(True, "", True, 7562) is False       # Notion retrying: wait
    assert nc._orig_finished_dead(True, "", True, 39500) is True       # output cap: ghost
    assert nc._orig_finished_dead(True, "hi", True, 100) is False
    assert nc._orig_finished_dead(False, "", True, None) is False


def test_orig_finished_dead_switch(monkeypatch):
    monkeypatch.setenv("NOTION_HEDGE_WAIT_RETRY", "off")
    assert nc._orig_finished_dead(True, "", True, 7562) is True


# ---- stream shadow: remove of a missing key ----
def test_shadow_remove_missing_key_is_noop():
    from app.stream_parser import _ShadowState
    sh = _ShadowState()
    sh.reset([{"id": "a", "type": "agent-inference", "value": [{"type": "text", "content": "x"}]}])
    assert sh.apply({"o": "r"}, "/s/0/value/0/finishedAt") is not False
    assert sh.errors == 0
    sh.apply({"o": "x", "v": "y"}, "/s/0/value/0/content")
    assert sh.doc["s"][0]["value"][0]["content"] == "xy"


def test_shadow_remove_missing_key_strict(monkeypatch):
    monkeypatch.setenv("NOTION_SHADOW_LENIENT_REMOVE", "off")
    from app.stream_parser import _ShadowState
    sh = _ShadowState()
    sh.reset([{"id": "a", "type": "agent-inference", "value": [{"type": "text", "content": "x"}]}])
    sh.apply({"o": "r"}, "/s/0/value/0/finishedAt")
    assert sh.errors == 1


def test_request_log_has_effort_sent_column():
    from app.admin.store import AdminStore
    assert "effort_sent" in AdminStore._LOG_COLS


def _hedge_api(hedge_text, orig_state):
    api = fake_api()
    api._pool = None
    seen = []
    api.interrupt_thread = lambda tid, reason, **kw: seen.append((tid, reason))
    def once(tr, thread_id=None, meta=None):
        meta["thread_id"] = "th"
        if hedge_text:
            yield {"type": "content", "text": hedge_text}
    api._stream_response_once = once
    api._thread_inference_state = lambda tid, exclude=None: orig_state
    return api, seen


def test_hedge_win_interrupts_original(monkeypatch):
    monkeypatch.setattr(nc.time, "sleep", lambda s: None)
    api, seen = _hedge_api("hedge answer", ("", False, True, None, {}))
    tr = [{"type": "config", "value": {"model": "claude-opus5.5", "reasoningEffort": "max"}}]
    assert api._hedged_recover(tr, "orig", {}, set(), "max") == "hedge answer"
    assert ("orig", "hedge_loser:original") in seen and all(t != "th" for t, _ in seen)


def test_hedge_original_win_interrupts_hedge(monkeypatch):
    monkeypatch.setattr(nc.time, "sleep", lambda s: None)
    api, seen = _hedge_api("", ("orig answer", True, False, 10, {}))
    tr = [{"type": "config", "value": {"model": "claude-opus5.5", "reasoningEffort": "max"}}]
    assert api._hedged_recover(tr, "orig", {}, set(), "max") == "orig answer"
    assert ("th", "hedge_loser:hedge") in seen and all(t != "orig" for t, _ in seen)


def test_hedge_fresh_attempts_are_rescue_marked(monkeypatch):
    monkeypatch.setattr(nc.time, "sleep", lambda s: None)
    api, seen = _hedge_api("x", ("", False, True, None, {}))
    got = []
    inner = api._stream_response_once
    def once(tr, thread_id=None, meta=None):
        got.append(tr[0]["value"])
        yield from inner(tr, thread_id, meta)
    api._stream_response_once = once
    tr = [{"type": "config", "value": {"model": "claude-opus5.5", "reasoningEffort": "max"}}]
    api._hedged_recover(tr, "orig", {}, set(), "max")
    assert got and got[0]["reasoningEffort"] == "high" and got[0][nc.RESCUE_EFFORT_KEY] is True
