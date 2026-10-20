import collections
import time

import pytest

from app import account_pool as ap


def _pool(monkeypatch, n=6, spread="p2c", usage=None):
    monkeypatch.setenv("ACCOUNT_STRATEGY", "headroom")
    monkeypatch.setenv("POOL_LOAD_AWARE", "on")
    monkeypatch.setenv("ACCOUNT_PRIORITY", "")
    monkeypatch.setenv("ACCOUNT_SOFT_INFLIGHT", "2")
    if spread is None:
        monkeypatch.delenv("POOL_SPREAD", raising=False)
    else:
        monkeypatch.setenv("POOL_SPREAD", spread)
    accs = [{"token_v2": "t%d" % i, "space_id": "s%d" % i, "user_id": "u%d" % i,
             "user_email": "a%d@x" % i, "_account_key": "a%d@x" % i} for i in range(n)]
    pool = ap.AccountPool(accs)
    now = time.time()
    usage = usage or [(10.0, 5.0)] * n
    for c, (mp, rp) in zip(pool.clients, usage):
        c.usage_status = {"status": "within_limit", "monthly_pct": mp, "rolling_pct": rp,
                          "checked_at": int(now), "monthly_reset_at": int(now + 10 * 86400)}
    return pool


def _picks(pool, k=400):
    out = collections.Counter()
    for _ in range(k):
        idx, why, _r, _s = pool._choose_la(list(range(len(pool.clients))), None, time.time())
        out[pool.clients[idx].account_key] += 1
    return out


def test_default_is_spread(monkeypatch):
    pool = _pool(monkeypatch, spread=None)
    assert ap.spread_mode() == "p2c"
    c = _picks(pool)
    assert len(c) >= 3
    assert max(c.values()) / sum(c.values()) < 0.5


def test_off_keeps_single_primary(monkeypatch):
    pool = _pool(monkeypatch, spread="off")
    c = _picks(pool)
    assert len(c) == 1


def test_spread_limited_to_topk_and_ratio(monkeypatch):
    monkeypatch.setenv("POOL_SPREAD_TOPK", "3")
    # a0..a2 strong, a3 weak (score < 0.5 x best), a4 rolling>=70 ineligible, a5 strong but 4th
    usage = [(10, 5), (12, 5), (14, 5), (90, 5), (10, 80), (16, 5)]
    pool = _pool(monkeypatch, usage=usage)
    c = _picks(pool)
    assert set(c) <= {"a0@x", "a1@x", "a2@x"}
    assert len(c) == 3


def test_ineligible_never_picked(monkeypatch):
    usage = [(10, 5), (10, 75), (96, 5), (10, 5)]
    pool = _pool(monkeypatch, n=4, usage=usage)
    ap.AccountPool.mark_exhausted(pool, pool.clients[3], 600, "test")
    c = _picks(pool, 200)
    # only a0 eligible -> fewer than 2 candidates -> old primary logic, still a0
    assert set(c) == {"a0@x"}


def test_prefers_less_loaded(monkeypatch):
    pool = _pool(monkeypatch, n=2)
    ap.reserve(pool.clients[0])  # a0 load 1, a1 load 0
    c = _picks(pool, 300)
    assert c["a1@x"] > c.get("a0@x", 0)


def test_full_account_excluded(monkeypatch):
    pool = _pool(monkeypatch, n=3)
    ap.reserve(pool.clients[0]); ap.reserve(pool.clients[0])  # at soft cap 2
    c = _picks(pool, 200)
    assert "a0@x" not in c


def test_sticky_lookup_unaffected(monkeypatch):
    pool = _pool(monkeypatch)
    for _ in range(20):
        assert pool.get_client_by_key("a4@x") is pool.clients[4]
