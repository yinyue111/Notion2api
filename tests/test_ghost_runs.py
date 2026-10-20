"""Ghost runs (2026-10-19 c:282144 / c:4628dc): Notion's Opus thinks until its 40k output cap
and never writes text (~430-470 s); our stream sees only the auto 'view' step, Notion closes
the silent stream after ~60 s, the thread stays "running" (Notion auto-retries the inference)
and we used to poll it to NOTION_LIVE_MAX (450 s) and answer an empty 200."""
import json
import logging
import pytest
from app import ghost_guard
from app import notion_client as nc
from app.api import chat
from app.notion_client import NotionOpusAPI
from app.tool_emulation import normalize_tools

TOOLS = normalize_tools([{"type": "function", "function": {"name": "Bash", "parameters": {}}}])
CALL = '<tool_call>\n{"name": "Bash", "arguments": {"command": "ls"}}\n</tool_call>'


class Clock:
    def __init__(self, t=1000.0):
        self.t = t
    def time(self):
        return self.t
    def sleep(self, s):
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(nc.time, "time", c.time)
    monkeypatch.setattr(nc.time, "sleep", c.sleep)
    monkeypatch.setattr(chat.time, "time", c.time)
    monkeypatch.setattr(chat.time, "sleep", c.sleep)
    ghost_guard.reset()
    for k in ("NOTION_LIVE_EXTEND_NEEDS_TEXT", "NOTION_GHOST_GUARD", "NOTION_GHOST_ARCHIVE",
              "NOTION_GHOST_RETRY_MIN_LEFT", "NOTION_THREAD_POLL_MAX", "NOTION_GHOST_WAIT_RETRY",
              "NOTION_POLL_SETTLE_GRACE"):
        monkeypatch.delenv(k, raising=False)
    return c


def bare_api(step=None, running=True, text_after=None, clock=None):
    """NotionOpusAPI whose thread record = [user, (step)] and current_inference_id=running."""
    api = NotionOpusAPI.__new__(NotionOpusAPI)
    api.identity_redactor = None
    api.space_id = "sp"
    api.account_key = "acc1"
    api._pool = None
    api.current_thread_id = None
    api.archived = []
    api.delete_thread = lambda tid: api.archived.append(tid)
    api.polls = 0
    def post(ep, body, timeout=15):
        table = body["requests"][0]["pointer"]["table"]
        if table == "thread":
            api.polls += 1
            st = step
            if text_after is not None and clock.t >= text_after[0]:
                st = text_after[1]
            msgs = ["u1"] + (["m1"] if st else [])
            return 200, {"recordMap": {"thread": {"t1": {"value": {"messages": msgs,
                                                                     "current_inference_id": "i" if running else None}}}}}
        st = step
        if text_after is not None and clock.t >= text_after[0]:
            st = text_after[1]
        recs = {"u1": {"value": {"step": {"type": "user"}}}}
        if st:
            recs["m1"] = {"value": {"step": st}}
        return 200, {"recordMap": {"thread_message": recs}}
    api.api_post = post
    return api


def meta_for(clock, deadline_s=180, budget_s=330, live_s=450):
    t0 = clock.t
    return {"_deadline": t0 + deadline_s, "_budget_end": t0 + budget_s, "_live_end": t0 + live_s, "_rl": {}}


# ---- thread polling ---------------------------------------------------------------------

def test_no_text_ghost_stops_at_budget_not_live_max(clock):
    api = bare_api(step=None, running=True)
    m = meta_for(clock)
    t0 = clock.t
    assert api._poll_thread_inference("t1", m["_deadline"], cancel=m) == ""
    assert 330 <= clock.t - t0 < 340          # was 450 (NOTION_LIVE_MAX) before the fix
    assert m["_ghost"] == "no_text" and m["_rl"]["live_extend"] == 150


def test_writing_text_still_gets_live_extension(clock):
    partial = {"type": "agent-inference", "value": [{"type": "text", "content": "partial"}]}
    done = {"type": "agent-inference", "finishedAt": 1, "value": [{"type": "text", "content": "partial and done"}]}
    api = bare_api(step=partial, running=True, text_after=(1000 + 420, done), clock=clock)
    m = meta_for(clock)
    assert api._poll_thread_inference("t1", m["_deadline"], cancel=m) == "partial and done"
    assert m["_rl"]["live_extend"] == 270 and "_ghost" not in m


def test_legit_long_thinker_answering_before_budget_end_is_kept(clock):
    done = {"type": "agent-inference", "finishedAt": 1, "outputTokens": 25000,
            "value": [{"type": "thinking", "content": ""}, {"type": "text", "content": CALL}]}
    api = bare_api(step=None, running=True, text_after=(1000 + 300, done), clock=clock)
    m = meta_for(clock)
    assert api._poll_thread_inference("t1", m["_deadline"], cancel=m) == CALL
    assert "_ghost" not in m


def test_capped_inference_detected_immediately(clock):
    capped = {"type": "agent-inference", "finishedAt": 1, "outputTokens": 40000,
              "value": [{"type": "thinking", "content": ""}]}
    api = bare_api(step=capped, running=True)  # Notion already auto-retrying: still "running"
    m = meta_for(clock)
    t0 = clock.t
    assert api._poll_thread_inference("t1", m["_deadline"], cancel=m) == ""
    assert clock.t - t0 < 10 and m["_ghost"] == "capped"


def test_retried_attempt_counts_as_capped_once_idle(clock):
    st = {"type": "agent-inference", "finishedAt": 1, "outputTokens": 1200,
          "previousAttemptValues": [[{"type": "thinking", "content": ""}]], "value": []}
    api = bare_api(step=st, running=False)
    m = meta_for(clock)
    api._poll_thread_inference("t1", m["_deadline"], cancel=m)
    assert m["_ghost"] == "capped"


def test_retried_attempt_still_running_is_waited_for(clock):
    """2026-10-20 08:01-08:05 (c:4628dc, 112 tools): finished text-less retried step far
    below the cap with the thread still running = Notion re-running it after transcript
    compaction; it was declared capped after ~190 s and stopped. Now we keep polling."""
    st = {"type": "agent-inference", "finishedAt": 1, "outputTokens": 1200,
          "previousAttemptValues": [[{"type": "thinking", "content": ""}]], "value": []}
    done = {"type": "agent-inference", "finishedAt": 2, "outputTokens": 3000,
            "previousAttemptValues": [[{"type": "thinking", "content": ""}]],
            "value": [{"type": "text", "content": CALL}]}
    api = bare_api(step=st, running=True, text_after=(1000 + 200, done), clock=clock)
    # the answer lands while the thread record still says running (lease not cleared yet)
    m = meta_for(clock)
    assert api._poll_thread_inference("t1", m["_deadline"], cancel=m) == CALL
    assert "_ghost" not in m


def test_retried_attempt_wait_switch_off_restores_capped(clock, monkeypatch):
    monkeypatch.setenv("NOTION_GHOST_WAIT_RETRY", "off")
    st = {"type": "agent-inference", "finishedAt": 1, "outputTokens": 1200,
          "previousAttemptValues": [[{"type": "thinking", "content": ""}]], "value": []}
    api = bare_api(step=st, running=True)
    m = meta_for(clock)
    t0 = clock.t
    api._poll_thread_inference("t1", m["_deadline"], cancel=m)
    assert m["_ghost"] == "capped" and clock.t - t0 < 10


def test_retried_running_never_answering_still_ends_at_budget(clock):
    st = {"type": "agent-inference", "finishedAt": 1, "outputTokens": 1200,
          "previousAttemptValues": [[{"type": "thinking", "content": ""}]], "value": []}
    api = bare_api(step=st, running=True)
    m = meta_for(clock)
    t0 = clock.t
    assert api._poll_thread_inference("t1", m["_deadline"], cancel=m) == ""
    assert clock.t - t0 < 340 and m["_ghost"] == "no_text"   # bounded by the request budget


def test_finished_partial_while_running_is_not_returned_early(clock):
    """07:56-08:00 (c:4628dc): an 18-char partial ('...复用 `r') of a finished step was
    returned as the final reply while Notion kept running; the client got no tool call."""
    part = {"type": "agent-inference", "finishedAt": 1, "outputTokens": 900,
            "previousAttemptValues": [[{"type": "text", "content": "Core read. Reuse `r"}]], "value": []}
    full = {"type": "agent-inference", "finishedAt": 2, "outputTokens": 4000,
            "previousAttemptValues": [[{"type": "text", "content": "Core read. Reuse `r"}]],
            "value": [{"type": "text", "content": "Core read. Reuse `run` here.\n" + CALL}]}
    api = bare_api(step=part, running=True, clock=clock)
    m = meta_for(clock)
    # switch the record to the finished retry and idle thread after 120 s
    orig = api.api_post
    def post(ep, body, timeout=15):
        if clock.t >= 1120:
            api_done = bare_api(step=full, running=False)
            return api_done.api_post(ep, body, timeout)
        return orig(ep, body, timeout)
    api.api_post = post
    out = api._poll_thread_inference("t1", m["_deadline"], cancel=m)
    assert out.endswith(CALL) and out.count("Core read.") == 1


def test_finished_text_running_without_retry_marker_waits_only_grace(clock):
    done = {"type": "agent-inference", "finishedAt": 1, "value": [{"type": "text", "content": CALL}]}
    api = bare_api(step=done, running=True)
    m = meta_for(clock)
    t0 = clock.t
    assert api._poll_thread_inference("t1", m["_deadline"], cancel=m) == CALL
    assert 20 <= clock.t - t0 <= 30


def test_held_text_returned_at_deadline(clock):
    part = {"type": "agent-inference", "finishedAt": 1,
            "previousAttemptValues": [[{"type": "thinking", "content": ""}]],
            "value": [{"type": "text", "content": "answer"}]}
    api = bare_api(step=part, running=True)
    m = meta_for(clock)
    assert api._poll_thread_inference("t1", m["_deadline"], cancel=m) == "answer"


def test_step_text_joins_attempts_without_doubling(monkeypatch):
    monkeypatch.delenv("NOTION_GHOST_WAIT_RETRY", raising=False)
    a = [{"type": "text", "content": "Hello "}]
    assert nc._step_text({"previousAttemptValues": [a], "value": [{"type": "text", "content": "world"}]}) == "Hello world"
    assert nc._step_text({"previousAttemptValues": [a], "value": [{"type": "text", "content": "Hello again"}]}) == "Hello again"
    assert nc._step_text({"value": [{"type": "text", "content": "x"}, {"type": "thinking", "content": "t"}]}) == "x"
    monkeypatch.setenv("NOTION_GHOST_WAIT_RETRY", "off")
    assert nc._step_text({"previousAttemptValues": [a], "value": [{"type": "text", "content": "world"}]}) == "world"


def test_same_thread_502_retry_skipped_after_ghost_verdict(clock):
    class C:
        account_key = "acc1"
        def __init__(self):
            self.calls = []
        def stream_response(self, transcript, thread_id=None, meta=None, **kw):
            self.calls.append(thread_id)
            def gen():
                if len(self.calls) == 1:
                    meta["thread_id"] = thread_id
                    meta["_ghost"] = "capped"
                    raise nc.NotionUpstreamError("Notion upstream returned an empty stream", status_code=502)
                yield {"type": "content", "text": "ok " + CALL}
            return gen()
    c = C()
    meta = {"_rl": {}, "_conv_keys": ["c:abc"]}
    res = None
    for ev in chat._run_tool_attempts(c, [{"id": "x", "type": "user"}], include_usage=True, account="a",
                                      tools=TOOLS, parallel=True, mode="auto", thread_id="reuse1",
                                      fallback_transcript=[{"id": "y", "type": "user"}], meta=meta):
        if isinstance(ev, tuple):
            res = ev[1]
    assert c.calls[0] == "reuse1" and c.calls[1] != "reuse1"   # not re-run on the given-up thread
    assert res and res["calls"]


def test_switch_off_restores_old_extension(clock, monkeypatch):
    monkeypatch.setenv("NOTION_LIVE_EXTEND_NEEDS_TEXT", "off")
    api = bare_api(step=None, running=True)
    m = meta_for(clock)
    t0 = clock.t
    api._poll_thread_inference("t1", m["_deadline"], cancel=m)
    assert clock.t - t0 >= 450 and m["_rl"]["live_extend"] == 270


# ---- whole client path: stream with only the 'view' step, silent, closes ----------------

def _client_with_silent_stream(api):
    def raw(transcript, thread_id=None, confirm_step_ids=None, meta=None):
        if meta is not None:
            meta["thread_id"] = "t1"
            meta["account"] = api.account_key
        yield {"type": "search", "queries": []}   # Notion's auto 'view' of the context page
        nc.time.sleep(66)                         # Notion closes the silent stream ~60 s later
    api._stream_response_raw = raw
    return api


@pytest.mark.parametrize("effort", ["medium", "high"])
def test_stream_once_ghost_ends_within_budget_and_archives(clock, effort):
    api = _client_with_silent_stream(bare_api(step=None, running=True))
    t0 = clock.t
    m = meta_for(clock)
    tr = [{"type": "config", "value": {"model": "claude-opus5.5", "reasoningEffort": effort}},
          {"type": "user", "value": [["hi"]]}]
    items = list(api._stream_response_once(tr, thread_id=None, meta=m))
    assert not [i for i in items if i.get("type") == "content"]
    assert clock.t - t0 <= 340            # the 2026-10-19 runs took 450-454 s
    assert m["_ghost"] == "no_text" and api.archived == ["t1"]


def test_archive_switch_off(clock, monkeypatch):
    monkeypatch.setenv("NOTION_GHOST_ARCHIVE", "off")
    api = _client_with_silent_stream(bare_api(step=None, running=True))
    m = meta_for(clock)
    list(api._stream_response_once([{"type": "config", "value": {"reasoningEffort": "medium"}},
                                    {"type": "user", "value": [["hi"]]}], thread_id=None, meta=m))
    assert api.archived == []


# ---- tool mode: never an empty 200 ------------------------------------------------------

class GhostClient:
    """stream_response like NotionOpusAPI: the ghost scripts take `secs` and set meta['_ghost']."""
    def __init__(self, clock, scripts):
        self.clock, self.scripts, self.calls = clock, list(scripts), 0
        self.account_key = "acc1"
    def stream_response(self, transcript, thread_id=None, meta=None, **kw):
        self.calls += 1
        kind, secs, items = self.scripts.pop(0)
        def gen():
            self.clock.t += secs
            if meta is not None:
                meta["thread_id"] = f"t{self.calls}"
                if kind:
                    meta["_ghost"] = kind
            for it in items:
                yield it
            yield {"type": "usage", "usage": {"inputTokens": 100, "outputTokens": 40000 if kind else 10}}
        return gen()


def run_tool(client, meta):
    res = None
    for ev in chat._run_tool_attempts(client, [{"id": "x", "type": "user"}], include_usage=True, account="a",
                                      tools=TOOLS, parallel=True, mode="auto", meta=meta):
        if isinstance(ev, tuple):
            res = ev[1]
    return res


def test_tool_mode_ghost_becomes_error_not_empty_200(clock):
    c = GhostClient(clock, [("no_text", 330, [])])
    meta = {"_rl": {}, "_conv_keys": ["c:abc"]}
    r = run_tool(c, meta)
    assert c.calls == 1                         # no hopeless second run past the budget
    assert r["error"] == chat.EMPTY_CODE
    assert meta["_rl"]["error_code"] == "empty_stream"
    with pytest.raises(chat.NotionUpstreamError) as ei:
        chat._tool_mode_non_stream(iter([("done", r)]), "id", "m")
    assert ei.value.code == "EMPTY_STREAM" and chat._classify_upstream_error(ei.value)["status"] == 502


def test_tool_mode_stream_emits_empty_stream_error(clock):
    c = GhostClient(clock, [("no_text", 330, [])])
    out = list(chat._tool_mode_stream(chat._run_tool_attempts(
        c, [{"id": "x", "type": "user"}], include_usage=True, account="a", tools=TOOLS, parallel=True,
        mode="auto", meta={"_rl": {}}), "id1", "m"))
    evs = [json.loads(l[6:]) for l in out if l.startswith("data: {")]
    assert any(e.get("error", {}).get("code") == "EMPTY_STREAM" for e in evs)
    assert not any((e.get("choices") or [{}])[0].get("finish_reason") == "stop" for e in evs if e.get("choices"))


def test_early_capped_ghost_retries_once_then_answers(clock):
    c = GhostClient(clock, [("capped", 60, []), (None, 10, [{"type": "content", "text": "ok " + CALL}])])
    meta = {"_rl": {}, "_conv_keys": ["c:abc"]}
    ghost_guard.record_empty(["c:abc"])
    r = run_tool(c, meta)
    assert c.calls == 2 and r["calls"] and "error" not in r
    assert ghost_guard.state(["c:abc"]) == {}   # a real answer resets the conversation


def test_breaker_opens_after_two_consecutive_ghosts(clock):
    for i in range(2):
        meta = {"_rl": {}, "_conv_keys": ["c:abc"]}
        run_tool(GhostClient(clock, [("no_text", 330, [])]), meta)
    assert 290 < ghost_guard.blocked(["c:abc"]) <= 300
    assert ghost_guard.blocked(["c:other"]) == 0


# ---- ghost_guard unit -------------------------------------------------------------------

def test_guard_backoff_window_and_switch(monkeypatch):
    ghost_guard.reset()
    k = ["c:x", "t:th"]
    assert ghost_guard.record_empty(k, now=0) == 1 and ghost_guard.blocked(k, now=1) == 0
    assert ghost_guard.record_empty(["t:th"], now=100) == 2          # any shared key chains
    assert ghost_guard.blocked(["t:th"], now=101) == pytest.approx(299)
    assert ghost_guard.record_empty(k, now=500) == 3 and ghost_guard.blocked(k, now=500) == 600
    assert ghost_guard.record_empty(k, now=5000) == 1                 # outside the window: new chain
    monkeypatch.setenv("NOTION_GHOST_GUARD", "off")
    ghost_guard.record_empty(k, now=5001)
    assert ghost_guard.blocked(k, now=5002) == 0
    monkeypatch.delenv("NOTION_GHOST_GUARD")
    ghost_guard.record_ok(k)
    assert ghost_guard.state(k) == {}


# ---- persistent log file ----------------------------------------------------------------

def test_file_log_handler(tmp_path, monkeypatch):
    from app import logger as lg
    path = tmp_path / "n2a.log"
    monkeypatch.setenv("NOTION_LOG_FILE", str(path))
    monkeypatch.setenv("NOTION_LOG_MAX_MB", "0.001")
    log = logging.getLogger("n2a_test_file")
    lg._add_file_handler(log)
    log.setLevel(logging.INFO)
    for i in range(50):
        log.info("x" * 100, extra={"request_info": {"event": "t", "i": i}})
    assert path.exists() and json.loads(path.read_text().splitlines()[-1])["event"] == "t"
    assert (tmp_path / "n2a.log.1").exists()                         # rotated, size-capped
    monkeypatch.setenv("NOTION_LOG_FILE", str(tmp_path / "missing" / "x.log"))
    log2 = logging.getLogger("n2a_test_file2")
    lg._add_file_handler(log2)
    assert not log2.handlers                                          # dir absent: no-op
