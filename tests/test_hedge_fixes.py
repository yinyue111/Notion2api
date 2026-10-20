"""2026-10-20 audit fixes: hedge winner sticky reuse (E), hedge budget cap / per-attempt
cap (C/D), hedge-loser cancel not logged as failed, usage recovery when Notion's usage
event is missing (H)."""
import logging
import pytest
from app import notion_client as nc
from app import request_log
from app.api import chat
from app.notion_client import NotionOpusAPI


class SyncThread:
    def __init__(self, target=None, name=None, daemon=None, args=()):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)


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
    monkeypatch.setattr(nc.threading, "Thread", SyncThread)
    NotionOpusAPI._interrupt_seen.clear()
    NotionOpusAPI._dispatch_ts.clear()
    for k in ("NOTION_HEDGE_REUSE_WINNER", "NOTION_HEDGE_BUDGET_CAP", "NOTION_HEDGE_ATTEMPT_MAX",
              "NOTION_USAGE_RECOVER", "NOTION_HEDGE_EFFORT", "NOTION_HEDGE_WAIT",
              "NOTION_LIVE_EXTEND_NEEDS_TEXT", "NOTION_CAP_MEMORY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NOTION_CAP_MEMORY", "off")
    return c


TR = [{"type": "config", "value": {"model": "claude-opus5.5", "reasoningEffort": "xhigh"}}]


def hedge_api(clock, hedge=lambda effort, meta: "", orig=("", False, True, None, {})):
    api = NotionOpusAPI.__new__(NotionOpusAPI)
    api.space_id, api.account_key, api._pool = "sp", "acc1", None
    api.seen, api.attempts = [], []
    api.interrupt_thread = lambda tid, reason, **kw: api.seen.append((tid, reason))
    def once(tr, thread_id=None, meta=None):
        n = len(api.attempts)
        meta["thread_id"] = f"h{n}"
        eff = tr[0]["value"]["reasoningEffort"]
        api.attempts.append({"effort": eff, "start": clock.t, "deadline": meta.get("_deadline"),
                             "budget_end": meta.get("_budget_end"), "live_end": meta.get("_live_end")})
        txt = hedge(eff, meta)
        if txt:
            yield {"type": "content", "text": txt}
    api._stream_response_once = once
    api._thread_inference_state = (orig if callable(orig) else (lambda tid, exclude=None: orig))
    return api


def meta_at(clock, budget=330, live=450):
    return {"_budget_end": clock.t + budget, "_live_end": clock.t + live, "_rl": {}}


# ---- E: sticky reuse follows the winning hedge ---------------------------------------

def test_hedge_win_sets_reuse_target(clock):
    api = hedge_api(clock, hedge=lambda e, m: "answer")
    meta = meta_at(clock)
    meta.update(thread_id="orig", account="acc1")
    assert api._hedged_recover(TR, "orig", meta, set(), "xhigh") == "answer"
    assert meta["_reuse_thread_id"] == "h0" and meta["_reuse_account"] == "acc1"
    assert chat._reuse_target(meta) == ("h0", "acc1")
    assert all(t != "h0" for t, _ in api.seen)  # winner's thread is not interrupted


def test_hedge_reuse_switch_off(clock, monkeypatch):
    monkeypatch.setenv("NOTION_HEDGE_REUSE_WINNER", "off")
    api = hedge_api(clock, hedge=lambda e, m: "answer")
    meta = meta_at(clock)
    meta.update(thread_id="orig", account="acc1")
    api._hedged_recover(TR, "orig", meta, set(), "xhigh")
    assert "_reuse_thread_id" not in meta
    assert chat._reuse_target(meta) == ("orig", "acc1")


def test_original_win_keeps_original_reuse(clock):
    api = hedge_api(clock, orig=("orig answer", True, False, 10, {}))
    meta = meta_at(clock)
    meta.update(thread_id="orig", account="acc1")
    assert api._hedged_recover(TR, "orig", meta, set(), "xhigh") == "orig answer"
    assert "_reuse_thread_id" not in meta and chat._reuse_target(meta) == ("orig", "acc1")


def test_reuse_target_plain():
    assert chat._reuse_target({"thread_id": "t", "account": "a"}) == ("t", "a")
    assert chat._reuse_target({"thread_id": "t", "account": "a", "_reuse_thread_id": "h"}) == ("t", "a")


# ---- C/D: budget cap + per-attempt cap ----------------------------------------------

def _thinking_hedge(clock):
    def h(effort, meta):
        clock.t = max(clock.t, meta["_budget_end"])  # thinks silently to its no-text cap
        return ""
    return h


def test_hedge_attempts_capped_at_budget_and_per_attempt(clock):
    t0 = clock.t
    api = hedge_api(clock, hedge=_thinking_hedge(clock))
    meta = meta_at(clock)
    assert api._hedged_recover(TR, "orig", meta, set(), "xhigh") == ""
    efforts = [a["effort"] for a in api.attempts]
    assert efforts[0] == "high" and "medium" in efforts
    hi = api.attempts[0]
    assert hi["budget_end"] == pytest.approx(t0 + 150)       # non-last effort: 150 s attempt cap
    assert hi["live_end"] == pytest.approx(t0 + 450)        # text-writing may still run long
    med = api.attempts[efforts.index("medium")]
    assert med["budget_end"] == pytest.approx(t0 + 330)     # last effort: request budget
    assert clock.t < t0 + 340                                # gave up at budget, not 450 s


def test_hedge_budget_cap_off_waits_to_live_end(clock, monkeypatch):
    monkeypatch.setenv("NOTION_HEDGE_BUDGET_CAP", "off")
    monkeypatch.setenv("NOTION_HEDGE_ATTEMPT_MAX", "0")
    t0 = clock.t
    api = hedge_api(clock, hedge=lambda e, m: "")
    api._hedged_recover(TR, "orig", meta_at(clock), set(), "xhigh")
    assert api.attempts[0]["budget_end"] is None and api.attempts[0]["deadline"] == pytest.approx(t0 + 450)
    assert clock.t >= t0 + 449


def test_original_writing_text_may_pass_budget(clock):
    t0 = clock.t
    def orig(tid, exclude=None):
        if clock.t < t0 + 400:
            return ("partial", False, True, None, {})
        return ("full answer", True, False, 50, {})
    api = hedge_api(clock, hedge=_thinking_hedge(clock), orig=orig)
    assert api._hedged_recover(TR, "orig", meta_at(clock), set(), "xhigh") == "full answer"
    assert clock.t >= t0 + 400


def test_hedge_writing_text_may_pass_budget(clock):
    t0 = clock.t
    def h(effort, meta):
        meta["_seen_text"] = True  # its poll saw text; finishes after the budget
        clock.t = t0 + 380
        return "late hedge answer"
    api = hedge_api(clock, hedge=h)
    assert api._hedged_recover(TR, "orig", meta_at(clock), set(), "xhigh") == "late hedge answer"


# ---- cosmetic: cancelled hedge attempt is not a failure -----------------------------

def test_cancelled_hedge_attempt_not_logged_failed(clock, caplog):
    def h(effort, meta):
        meta["_cancelled"] = True
        return ""
    api = hedge_api(clock, hedge=h, orig=("orig answer", True, False, 10, {}))
    with caplog.at_level(logging.INFO):
        api._hedged_recover(TR, "orig", meta_at(clock), set(), "xhigh")
    evs = [getattr(r, "request_info", {}).get("event") for r in caplog.records]
    assert "effort_hedge_attempt_cancelled" in evs and "effort_hedge_attempt_failed" not in evs
    assert len(api.attempts) == 1


# ---- H: usage recovery --------------------------------------------------------------

def rec_api(step):
    api = NotionOpusAPI.__new__(NotionOpusAPI)
    api.space_id = "sp"
    def post(ep, body, timeout=8):
        p = body["requests"][0]["pointer"]
        if p["table"] == "thread":
            return 200, {"recordMap": {"thread": {p["id"]: {"value": {"messages": ["u", "m"]}}}}}
        return 200, {"recordMap": {"thread_message": {"u": {"value": {"step": {"type": "user"}}},
                                                      "m": {"value": {"step": step}}}}}
    api.api_post = post
    return api


def test_usage_by_trace_reads_real_numbers():
    api = rec_api({"type": "agent-inference", "traceId": "tr1", "inputTokens": 100, "outputTokens": 7,
                   "cachedTokensRead": 50})
    assert api.usage_by_trace("t", "tr1") == {"inputTokens": 100, "outputTokens": 7, "cachedTokensRead": 50}
    assert api.usage_by_trace("t", "other") == {}


def test_usage_by_trace_no_numbers_is_empty():
    api = rec_api({"type": "agent-inference", "traceId": "tr1"})
    assert api.usage_by_trace("t", "tr1") == {}


def test_request_log_warns_usage_missing(caplog):
    class Col:
        raw = None
        first_content_ts = 1001.0
        error = None
    with caplog.at_level(logging.WARNING):
        rec = request_log._finalize({"_collector": Col(), "ts": 1000.0, "status": 200, "model": "m"})
    assert rec["usage_reported"] == 0
    assert any(getattr(r, "request_info", {}).get("event") == "usage_missing_on_reply" for r in caplog.records)


def test_request_log_no_warn_when_usage_present(caplog):
    class Col:
        raw = {"inputTokens": 1, "outputTokens": 1}
        first_content_ts = 1001.0
        error = None
    with caplog.at_level(logging.WARNING):
        request_log._finalize({"_collector": Col(), "ts": 1000.0, "status": 200})
    assert not any(getattr(r, "request_info", {}).get("event") == "usage_missing_on_reply" for r in caplog.records)
