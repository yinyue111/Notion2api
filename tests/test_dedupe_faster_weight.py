"""2026-10-20 (2): resend dedupe, hedge-faster streak -> forced high, per-account pick weight."""
import collections
import time
import pytest

from app import resend_dedupe as dd
from app import cap_memory as cm
from app import request_log
from app import account_pool as ap
from app import notion_client as nc
from app.api import chat
from app.notion_client import NotionOpusAPI, NotionUpstreamError

MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "do x"},
        {"role": "assistant", "content": "ok"}, {"role": "user", "content": "go"}]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k in ("NOTION_RESEND_DEDUPE", "NOTION_RESEND_DEDUPE_MIN_AGE", "NOTION_RESEND_DEDUPE_MIN_MSGS",
              "NOTION_CAP_MEMORY", "NOTION_CAP_MEMORY_FASTER_N", "NOTION_CAP_MEMORY_PROBE",
              "ACCOUNT_SCORE_WEIGHTS", "ACCOUNT_SOFT_INFLIGHT_OVERRIDES"):
        monkeypatch.delenv(k, raising=False)
    dd.clear()
    cm.clear()
    yield
    dd.clear()
    cm.clear()


# ---- resend dedupe -------------------------------------------------------------------

def fp(msgs=MSGS, ident="id1", model="m", tp="tools"):
    return dd.fingerprint(ident, model, tp, msgs)


def test_resend_after_min_age_supersedes_older():
    aborted = []
    rec_old = {}
    old, v = dd.begin(fp(), rec_old, "c:1", aborted.append, now=1000)
    assert v == []
    m1 = {"thread_id": "t-old"}
    dd.attach(old, m1)
    new, v = dd.begin(fp(), {}, "c:1", aborted.append, now=1000 + 86)
    assert v == [old] and aborted == [m1] and m1["_superseded"] is True
    assert rec_old["error_code"] == "superseded_duplicate"
    assert new is not old and not new["superseded"]
    # a later retry attempt of the superseded request is cancelled at once
    m2 = {}
    dd.attach(old, m2)
    assert m2["_cancelled"] is True


def test_parallel_fanout_not_superseded():
    aborted = []
    a, _ = dd.begin(fp(), {}, "", aborted.append, now=1000)
    dd.attach(a, {"thread_id": "x"})
    b, v = dd.begin(fp(), {}, "", aborted.append, now=1003)  # identical, 3 s apart: fan-out
    assert v == [] and aborted == [] and not a["superseded"]


def test_different_last_message_not_superseded():
    aborted = []
    a, _ = dd.begin(fp(), {}, "", aborted.append, now=1000)
    other = MSGS[:-1] + [{"role": "user", "content": "subagent task B"}]
    b, v = dd.begin(fp(other), {}, "", aborted.append, now=1100)
    assert v == [] and not a["superseded"]
    assert fp(other) != fp()


def test_different_caller_or_tools_not_superseded():
    assert fp(ident="id2") != fp()
    assert fp(tp="other tools") != fp()
    assert fp(model="m2") != fp()


def test_finished_request_not_superseded():
    aborted = []
    a, _ = dd.begin(fp(), {}, "", aborted.append, now=1000)
    dd.release(a)
    b, v = dd.begin(fp(), {}, "", aborted.append, now=1100)
    assert v == [] and dd.live_count() == 1


def test_short_prompts_and_off_switch(monkeypatch):
    assert fp(MSGS[:2]) is None  # < 3 messages: never deduped
    monkeypatch.setenv("NOTION_RESEND_DEDUPE", "off")
    assert fp() is None
    assert dd.begin(None) == (None, [])


def test_min_age_env(monkeypatch):
    monkeypatch.setenv("NOTION_RESEND_DEDUPE_MIN_AGE", "120")
    a, _ = dd.begin(fp(), {}, "", None, now=1000)
    b, v = dd.begin(fp(), {}, "", None, now=1090)
    assert v == []


def test_supersede_without_abort_sets_cancelled():
    a, _ = dd.begin(fp(), {}, "", None, now=1000)
    m = {}
    dd.attach(a, m)
    dd.begin(fp(), {}, "", None, now=1050)
    assert m["_cancelled"] is True


def test_abort_upstream_used_as_abort(monkeypatch):
    seen = []
    monkeypatch.setattr(nc, "interrupt_meta", lambda meta, reason: seen.append(reason))
    a, _ = dd.begin(fp(), {}, "", chat._abort_upstream, now=1000)
    m = {"thread_id": "t"}
    dd.attach(a, m)
    dd.begin(fp(), {}, "", chat._abort_upstream, now=1030)
    assert m["_cancelled"] is True and seen == ["client_gone"]


def test_superseded_error_is_409():
    e = NotionUpstreamError("x", status_code=409, retriable=False)
    e.code = "SUPERSEDED_DUPLICATE"
    assert chat._classify_upstream_error(e)["status"] == 409


def test_request_log_finish_hook_releases(monkeypatch):
    a, _ = dd.begin(fp(), {}, "", None, now=time.time())
    rec = {"_on_finish": lambda: dd.release(a)}
    cb = rec.pop("_on_finish")
    cb()
    assert a["done"] and dd.live_count() == 0


# ---- hedge-faster streak -> forced high ----------------------------------------------

K = ["c:abc"]


def test_two_faster_wins_force_high():
    assert cm.decide(K, "xhigh") == ("xhigh", None)
    assert cm.note_hedge_faster(K) == 1
    assert cm.decide(K, "xhigh") == ("xhigh", None)
    assert cm.note_hedge_faster(K) == 2
    assert cm.decide(K, "xhigh") == ("high", "forced_high")
    assert cm.state(K)["reason"] == "hedge_faster_x2"


def test_streak_reset_breaks_consecutive():
    cm.note_hedge_faster(K)
    cm.reset_faster(K)
    cm.note_hedge_faster(K)
    assert cm.decide(K, "xhigh") == ("xhigh", None)


def test_faster_switch_off(monkeypatch):
    monkeypatch.setenv("NOTION_CAP_MEMORY_FASTER_N", "0")
    cm.note_hedge_faster(K)
    cm.note_hedge_faster(K)
    assert cm.decide(K, "xhigh") == ("xhigh", None)


def test_probe_after_forced_turns_clears():
    cm.note_hedge_faster(K); cm.note_hedge_faster(K)
    assert [cm.decide(K, "xhigh")[1] for _ in range(4)] == ["forced_high"] * 3 + ["probe"]
    cm.probe_ok(K)
    assert cm.decide(K, "xhigh") == ("xhigh", None)


def test_clean_xhigh_turn_resets_streak():
    cm.note_hedge_faster(K)
    request_log._finalize({"_conv_keys": K, "effort_final": "xhigh", "status": 200, "silent_close": 0})
    cm.note_hedge_faster(K)
    assert cm.decide(K, "xhigh") == ("xhigh", None)


class SyncThread:
    def __init__(self, target=None, name=None, daemon=None, args=()):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)


def _hedge_api(monkeypatch, orig, hedge_text="hedge answer"):
    monkeypatch.setattr(nc.threading, "Thread", SyncThread)
    monkeypatch.setattr(nc.time, "sleep", lambda s: None)
    NotionOpusAPI._interrupt_seen.clear()
    api = NotionOpusAPI.__new__(NotionOpusAPI)
    api.space_id, api.account_key, api._pool = "sp", "acc1", None
    api.interrupt_thread = lambda tid, reason, **kw: None
    def once(tr, thread_id=None, meta=None):
        meta["thread_id"] = "h"
        if hedge_text:
            yield {"type": "content", "text": hedge_text}
    api._stream_response_once = once
    api._thread_inference_state = lambda tid, exclude=None: orig
    return api


TR = [{"type": "config", "value": {"model": "claude-opus5.5", "reasoningEffort": "xhigh"}}]


def test_hedge_recover_faster_twice_marks(monkeypatch):
    api = _hedge_api(monkeypatch, ("", False, True, None, {}))  # original still running: faster
    for _ in range(2):
        assert api._hedged_recover(TR, "orig", {"_conv_keys": K, "_rl": {}}, set(), "xhigh") == "hedge answer"
    assert cm.decide(K, "xhigh") == ("high", "forced_high")


def test_hedge_recover_original_win_resets(monkeypatch):
    api = _hedge_api(monkeypatch, ("", False, True, None, {}))
    api._hedged_recover(TR, "orig", {"_conv_keys": K, "_rl": {}}, set(), "xhigh")
    api2 = _hedge_api(monkeypatch, ("orig answer", True, False, 10, {}), hedge_text="")
    assert api2._hedged_recover(TR, "orig", {"_conv_keys": K, "_rl": {}}, set(), "xhigh") == "orig answer"
    api._hedged_recover(TR, "orig", {"_conv_keys": K, "_rl": {}}, set(), "xhigh")
    assert cm.decide(K, "xhigh") == ("xhigh", None)


# ---- per-account pick weight / soft cap ----------------------------------------------

def _pool(monkeypatch, n=4):
    monkeypatch.setenv("ACCOUNT_STRATEGY", "headroom")
    monkeypatch.setenv("POOL_LOAD_AWARE", "on")
    monkeypatch.setenv("ACCOUNT_PRIORITY", "")
    monkeypatch.setenv("ACCOUNT_SOFT_INFLIGHT", "2")
    monkeypatch.setenv("POOL_SPREAD", "p2c")
    keys = ["a0@x#ws1", "a1@x", "a2@x", "a3@x"][:n]
    accs = [{"token_v2": "t%d" % i, "space_id": "s%d" % i, "user_id": "u%d" % i,
             "user_email": k.split("#")[0], "_account_key": k} for i, k in enumerate(keys)]
    pool = ap.AccountPool(accs)
    now = time.time()
    usage = [(10.0, 5.0), (40.0, 5.0), (40.0, 5.0), (40.0, 5.0)]
    for c, (mp, rp) in zip(pool.clients, usage):
        c.account_key = c.account_key  # keep
        c.usage_status = {"status": "within_limit", "monthly_pct": mp, "rolling_pct": rp,
                          "checked_at": int(now), "monthly_reset_at": int(now + 10 * 86400)}
    return pool


def _picks(pool, k=600):
    out = collections.Counter()
    for _ in range(k):
        idx, why, _r, _s = pool._choose_la(list(range(len(pool.clients))), None, time.time())
        out[pool.clients[idx].account_key] += 1
    return out


def test_weight_parse_and_lookup(monkeypatch):
    monkeypatch.setenv("ACCOUNT_SCORE_WEIGHTS", "a0@x#ws1:0.4, b@y:0.5,bad,c@z:x")
    assert ap.account_weight("a0@x#ws1") == 0.4
    assert ap.account_weight("b@y#123") == 0.5  # email entry covers its workspaces
    assert ap.account_weight("a0@x") == 1.0     # workspace entry does not cover the base
    assert ap.account_weight("c@z") == 1.0


def test_weight_lowers_share(monkeypatch):
    pool = _pool(monkeypatch)
    keys = [c.account_key for c in pool.clients]
    base = _picks(pool)
    monkeypatch.setenv("ACCOUNT_SCORE_WEIGHTS", keys[0] + ":0.4")
    low = _picks(pool)
    assert low[keys[0]] < base[keys[0]] * 0.6
    row = next(r for r in pool.headroom_scores() if r["account"] == keys[0])
    assert row["weight"] == 0.4 and row["score"] == pytest.approx(row["raw_score"] * 0.4, rel=0.01)


def test_soft_cap_override(monkeypatch):
    pool = _pool(monkeypatch)
    c = pool.clients[0]
    assert ap.soft_cap(c) == 2
    monkeypatch.setenv("ACCOUNT_SOFT_INFLIGHT_OVERRIDES", c.account_key + ":1")
    assert ap.soft_cap(c) == 1 and ap.soft_cap(pool.clients[1]) == 2
