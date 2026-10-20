"""2026-10-20 (3): limit guard - over-limit accounts get no dispatch on any path."""
import time
import pytest

from app import account_pool as ap
from app import usage_monitor as um
from app import notion_client as nc
from app.notion_client import NotionOpusAPI, NotionUpstreamError


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k in ("NOTION_LIMIT_GUARD", "POOL_ROLLING_HARD_PCT", "POOL_SOFT_AVOID_ROLLING_PCT",
              "USAGE_HOT_POLL_SECONDS", "USAGE_HOT_PCT", "ACCOUNT_SCORE_WEIGHTS",
              "ACCOUNT_SOFT_INFLIGHT_OVERRIDES", "HEADROOM_MAX_ROLLING_PCT"):
        monkeypatch.delenv(k, raising=False)
    yield


def _pool(monkeypatch, rolling, status=None):
    monkeypatch.setenv("ACCOUNT_STRATEGY", "headroom")
    monkeypatch.setenv("POOL_LOAD_AWARE", "on")
    monkeypatch.setenv("ACCOUNT_PRIORITY", "")
    monkeypatch.setenv("ACCOUNT_SOFT_INFLIGHT", "2")
    monkeypatch.setenv("POOL_SPREAD", "p2c")
    keys = ["a%d@x" % i for i in range(len(rolling))]
    accs = [{"token_v2": "t%d" % i, "space_id": "s%d" % i, "user_id": "u%d" % i,
             "user_email": k, "_account_key": k} for i, k in enumerate(keys)]
    pool = ap.AccountPool(accs)
    now = time.time()
    for i, (c, rp) in enumerate(zip(pool.clients, rolling)):
        st = (status or {}).get(i, "within_limit")
        c.usage_status = {"status": st, "monthly_pct": 40.0, "rolling_pct": rp, "checked_at": int(now),
                          "monthly_reset_at": int(now + 10 * 86400), "rolling_reset_at": int(now + 1800),
                          "resume_at": int(now + 1800) if st == "rate_limited" else None}
    return pool


def test_rate_limited_status_blocks_without_flag(monkeypatch):
    pool = _pool(monkeypatch, [50, 104.8], {1: "rate_limited"})
    c = pool.clients[1]
    assert c.exhausted_until == 0
    assert ap.is_exhausted(c)
    assert pool.get_client_by_key("a1@x") is None          # sticky reuse
    assert pool.key_cooldown_left("a1@x") == -1.0          # no waiting for it
    assert not pool.other_free(pool.clients[0])            # failover sees nobody else
    for _ in range(50):
        assert pool.get_client(wait_if_cooling=False).account_key == "a0@x"
        ap.unreserve(pool.clients[0])


def test_rolling_over_hard_blocks_until_reset(monkeypatch):
    pool = _pool(monkeypatch, [50, 100.5])
    c = pool.clients[1]
    assert ap.is_exhausted(c)
    c.usage_status["rolling_reset_at"] = int(time.time() - 5)  # window rolled over
    assert not ap.is_exhausted(c)


def test_guard_off_switch(monkeypatch):
    monkeypatch.setenv("NOTION_LIMIT_GUARD", "off")
    pool = _pool(monkeypatch, [50, 104.8], {1: "rate_limited"})
    assert not ap.is_exhausted(pool.clients[1])
    assert pool.get_client_by_key("a1@x") is pool.clients[1]


def test_resume_passed_unblocks(monkeypatch):
    pool = _pool(monkeypatch, [50, 104.8], {1: "rate_limited"})
    pool.clients[1].usage_status["resume_at"] = int(time.time() - 1)
    assert not ap.is_exhausted(pool.clients[1])


def test_fallback_ranks_by_rolling_not_priority(monkeypatch):
    # every account >= HEADROOM_MAX_ROLLING_PCT (70): no eligible rows -> fallback
    pool = _pool(monkeypatch, [92.8, 83.5, 75.2, 94.8])
    idx, why, ranked, _ = pool._choose_la(list(range(4)), None, time.time())
    assert why.startswith("rolling_fallback")
    assert [pool.clients[i].account_key for i in ranked] == ["a2@x", "a1@x", "a0@x", "a3@x"]
    assert pool.clients[idx].account_key == "a2@x"


def test_fallback_with_failover_avoid(monkeypatch):
    # the 13:45 case: the one eligible account is the one being failed over from
    pool = _pool(monkeypatch, [53.1, 83.5, 75.5, 90.2])
    idx, why, ranked, _ = pool._choose_la(list(range(4)), {"a0@x"}, time.time())
    assert why.startswith("rolling_fallback")
    assert pool.clients[idx].account_key == "a2@x"
    assert pool.clients[ranked[-1]].account_key == "a3@x"   # >= 90 soft-avoided


def test_fallback_off_keeps_priority(monkeypatch):
    monkeypatch.setenv("NOTION_LIMIT_GUARD", "off")
    pool = _pool(monkeypatch, [92.8, 83.5, 75.2, 94.8])
    idx, why, ranked, _ = pool._choose_la(list(range(4)), None, time.time())
    assert why.startswith("priority")


def test_eligible_rows_unchanged(monkeypatch):
    pool = _pool(monkeypatch, [10, 20, 95, 30])
    idx, why, ranked, _ = pool._choose_la(list(range(4)), None, time.time())
    assert why.startswith("headroom")
    assert "a2@x" not in [pool.clients[i].account_key for i in ranked[:3]]


def test_all_blocked_picks_earliest_reset(monkeypatch):
    pool = _pool(monkeypatch, [104, 104], {0: "rate_limited", 1: "rate_limited"})
    pool.clients[0].usage_status["resume_at"] = int(time.time() + 3000)
    pool.clients[1].usage_status["resume_at"] = int(time.time() + 600)
    assert pool.get_client(wait_if_cooling=False).account_key == "a1@x"


def test_admin_status_shows_block(monkeypatch):
    pool = _pool(monkeypatch, [50, 104.8], {1: "rate_limited"})
    fn = getattr(pool, "status_map", None)
    if fn is None:
        pytest.skip("no admin status helper")
    out = fn()
    row = [v for v in out.values() if v.get("account_key") == "a1@x"][0]
    assert row["state"] == "exhausted" and row["exhausted_until"] > time.time()


def test_hot_poll_due(monkeypatch):
    calls = []
    monkeypatch.setattr(um, "check_account", lambda pool, c, src: calls.append(c.account_key))
    pool = _pool(monkeypatch, [83.5, 40.0])
    now = time.time()
    um._last_check.clear()
    um._last_check.update({"a0@x": now - 130, "a1@x": now - 130})
    sleeps = {"n": 0}

    def fake_sleep(s):
        sleeps["n"] += 1
        if sleeps["n"] > 2:
            raise SystemExit
    monkeypatch.setattr(um.time, "sleep", fake_sleep)
    with pytest.raises(SystemExit):
        um._poll_loop(lambda: pool)
    assert calls == ["a0@x"]  # hot account polled at 2 min, cool one waits for 10 min
    um._last_check.clear()


def _client(key="a1@x"):
    c = NotionOpusAPI.__new__(NotionOpusAPI)
    c.account_key = key
    c.exhausted_until = 0
    c.usage_status = {"status": "rate_limited", "rolling_pct": 104.8, "checked_at": int(time.time()),
                      "resume_at": int(time.time() + 1800)}
    return c


def test_dead_retry_skipped_when_limited():
    c = _client()
    meta = {}
    assert c._dead_retry([{"type": "user"}], meta, "xhigh", time.time() + 400) == ""
    assert not meta.get("_dead_retried")


def test_persona_retry_raises_for_failover(monkeypatch):
    c = _client()
    c.usage_status = {}
    calls = []

    def once(transcript, thread_id=None, meta=None):
        calls.append(1)
        # the account goes over its limit during the first (persona-leaking) attempt
        c.usage_status = {"status": "rate_limited", "rolling_pct": 104.8, "checked_at": int(time.time()),
                          "resume_at": int(time.time() + 1800)}
        yield {"type": "content", "text": "I'm Notion AI, an assistant built into Notion. " * 3}
    monkeypatch.setattr(c, "_stream_response_once", once)
    monkeypatch.setattr(c, "interrupt_thread", lambda *a, **k: None, raising=False)
    monkeypatch.setenv("NOTION_PERSONA_RETRIES", "2")
    tr = [{"type": "user", "value": [["hello"]]}]
    with pytest.raises(NotionUpstreamError) as ei:
        list(c._stream_guarded(tr, thread_id=None, meta={}))
    assert ei.value.status_code == 429 and ei.value.retriable
    assert len(calls) == 1


def test_dispatch_blocked_when_over_limit(monkeypatch):
    c = NotionOpusAPI({"token_v2": "t", "space_id": "s", "user_id": "u", "space_view_id": "v",
                       "user_name": "n", "user_email": "e@x"})
    c.usage_status = {"status": "rate_limited", "rolling_pct": 104.8, "checked_at": int(time.time()),
                      "resume_at": int(time.time() + 1800)}
    posted = []
    monkeypatch.setattr(c, "api_post", lambda *a, **k: posted.append(a) or (200, {}), raising=False)
    import requests
    monkeypatch.setattr(requests.Session, "post", lambda *a, **k: posted.append(a) or (_ for _ in ()).throw(AssertionError("sent")))
    with pytest.raises(NotionUpstreamError) as ei:
        list(c._stream_response_raw_impl([{"type": "user", "value": [["hi"]]}], meta={}))
    assert ei.value.status_code == 429 and not posted


def test_dispatch_not_blocked_by_preemptive_flag(monkeypatch):
    c = NotionOpusAPI({"token_v2": "t", "space_id": "s", "user_id": "u", "space_view_id": "v",
                       "user_name": "n", "user_email": "e@x"})
    c.usage_status = {"status": "within_limit", "rolling_pct": 96.0, "checked_at": int(time.time())}
    c.exhausted_until = time.time() + 600   # USAGE_DEMOTE_PCT pre-emptive flag only
    assert not nc._usage_over_limit(c)
    assert ap.is_exhausted(c)
