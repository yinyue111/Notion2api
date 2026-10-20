import os
import time
import threading
from typing import Dict, List

from app.logger import logger
from app.notion_client import NotionOpusAPI
from app import workspaces as ws

def strategy() -> str:
    return os.getenv("ACCOUNT_STRATEGY", "round_robin").strip().lower()


def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def rolling_penalty(rp, max_roll: float) -> float:
    """Soft penalty (0..weight) for a high 6h rolling usage, applied to headroom scores of
    accounts picked for NEW conversations (sticky threads never re-pick). Zero below
    HEADROOM_ROLLING_SOFT_PCT, then rises linearly to HEADROOM_ROLLING_PENALTY at the hard
    exclusion HEADROOM_MAX_ROLLING_PCT. HEADROOM_ROLLING_PENALTY=0 disables it."""
    if rp is None:
        return 0.0
    soft = _envf("HEADROOM_ROLLING_SOFT_PCT", 30.0)
    weight = min(1.0, max(0.0, _envf("HEADROOM_ROLLING_PENALTY", 0.5)))
    try:
        rp = float(rp)
    except (TypeError, ValueError):
        return 0.0
    if weight <= 0 or rp <= soft:
        return 0.0
    span = max(1e-6, max_roll - soft)
    return weight * min(1.0, (rp - soft) / span)


def _per_account(name: str) -> dict:
    """Parse "account_key:value,account_key:value" (env `name`). An entry without '#'
    also matches that email's other workspaces ("a@x.com" matches "a@x.com#1234")."""
    out = {}
    for part in (os.getenv(name, "") or "").split(","):
        k, sep, v = part.strip().rpartition(":")
        if not sep or not k:
            continue
        try:
            out[k.strip()] = float(v)
        except ValueError:
            continue
    return out


def _lookup(table: dict, key: str):
    if not table or not key:
        return None
    if key in table:
        return table[key]
    base = key.split("#", 1)[0]
    return table.get(base) if base != key else None


def account_weight(key: str) -> float:
    """ACCOUNT_SCORE_WEIGHTS (e.g. "a@x.com#3b60:0.4"): multiplier (0..1+) on the headroom
    score of NEW-conversation picks for that account; sticky threads are unaffected.
    Unset / unlisted = 1.0."""
    w = _lookup(_per_account("ACCOUNT_SCORE_WEIGHTS"), key)
    return 1.0 if w is None else max(0.0, w)


def exhaust_seconds() -> float:
    return float(os.getenv("ACCOUNT_EXHAUSTED_RECHECK_SECONDS", "1800") or 1800)


def limit_guard() -> bool:
    """NOTION_LIMIT_GUARD (default on): an account whose last usage lookup says
    rate_limited, or rolling >= POOL_ROLLING_HARD_PCT (100), counts as exhausted on
    EVERY dispatch path (new picks, sticky reuse, hedges, rescue, failover) until its
    resume/reset time, even before/without the monitor's exhausted flag; and the
    no-eligible-account fallback ranks by rolling usage instead of static priority."""
    return os.getenv("NOTION_LIMIT_GUARD", "on").strip().lower() not in ("0", "off", "false", "no")


def usage_blocked_until(client, now: float | None = None) -> float:
    """> now while the cached usage_status says the account is over its limit, else 0."""
    if not limit_guard():
        return 0.0
    st = getattr(client, "usage_status", None)
    if not isinstance(st, dict) or not st:
        return 0.0
    now = time.time() if now is None else now
    chk = float(st.get("checked_at") or 0) or now
    fb = chk + float(os.getenv("USAGE_FALLBACK_EXHAUST_SECONDS", "1800") or 1800)
    rp = st.get("rolling_pct")
    try:
        hard = float(os.getenv("POOL_ROLLING_HARD_PCT", "100") or 100)
    except ValueError:
        hard = 100.0
    until = 0.0
    if st.get("status") == "rate_limited":
        until = float(st.get("resume_at") or st.get("rolling_reset_at") or fb)
    elif isinstance(rp, (int, float)) and rp >= hard:
        until = float(st.get("rolling_reset_at") or fb)
    return until if until > now else 0.0


def blocked_until(client, now: float | None = None) -> float:
    return max(float(getattr(client, "exhausted_until", 0) or 0), usage_blocked_until(client, now))


def is_exhausted(client, now: float | None = None) -> bool:
    now = now or time.time()
    return (getattr(client, "exhausted_until", 0) or 0) > now or usage_blocked_until(client, now) > now


# ---------------- spread for NEW conversations (POOL_SPREAD) ----------------
# Only the first pick of a new conversation goes through _ranked(); sticky threads
# (thread reuse, get_client_by_key + acquire_sticky) never re-pick, so per-thread
# Notion caching is untouched. POOL_SPREAD=p2c (default): among the top
# POOL_SPREAD_TOPK headroom-eligible accounts whose score is >= POOL_SPREAD_MIN_RATIO
# x best and that are under their soft cap, sample two (weighted by score) and take
# the less loaded one (tie: the first sample). POOL_SPREAD=off: old behaviour
# (single headroom primary with HEADROOM_SWITCH_MARGIN hysteresis).
import random as _random

_spread_rng = _random.Random()


def spread_mode() -> str:
    v = os.getenv("POOL_SPREAD", "p2c").strip().lower()
    return "off" if v in ("0", "off", "false", "no", "none", "") else "p2c"


# ---------------- in-flight tracking (POOL_LOAD_AWARE) ----------------
# A request counts on an account from the moment it is picked (a short-lived
# reservation, so simultaneous picks see each other) and, once dispatched, for the
# lifetime of its Notion stream (NotionOpusAPI._stream_response_raw wraps the body in
# try/finally: success, error, cancel, client disconnect, timeout, hedge and retries
# all release). Entries older than NOTION_INFLIGHT_MAX_AGE are ignored (leak guard).
import itertools as _it

_track_lock = threading.Lock()
_tok = _it.count(1)


def _on(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off", "")


def load_aware() -> bool:
    return _on("POOL_LOAD_AWARE", "off")


def _envi(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, "") or default))
    except ValueError:
        return default


def inflight_start(client) -> int:
    """Register one dispatched Notion stream; consumes one pick reservation."""
    t = next(_tok)
    with _track_lock:
        d = client.__dict__.setdefault("_inflight", {})
        d[t] = time.time()
        p = client.__dict__.get("_pending")
        if p:
            p.pop(0)
    return t


def inflight_end(client, tok: int) -> None:
    with _track_lock:
        (client.__dict__.get("_inflight") or {}).pop(tok, None)


def reserve(client) -> None:
    ttl = _envf("POOL_RESERVE_TTL", 30.0)
    with _track_lock:
        client.__dict__.setdefault("_pending", []).append(time.time() + ttl)


def unreserve(client) -> None:
    with _track_lock:
        p = client.__dict__.get("_pending")
        if p:
            p.pop()


def load_of(client, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    max_age = _envf("NOTION_INFLIGHT_MAX_AGE", 900.0)
    with _track_lock:
        d = client.__dict__.get("_inflight") or {}
        for k in [k for k, ts in d.items() if now - ts > max_age]:
            d.pop(k, None)  # leaked / stuck: stop counting it
        p = client.__dict__.get("_pending")
        if p:
            p[:] = [x for x in p if x > now]
        return {"active": len(d), "pending": len(p or [])}


def load(client, now: float | None = None) -> int:
    l = load_of(client, now)
    return l["active"] + l["pending"]


def soft_cap(client, now: float | None = None) -> int:
    """ACCOUNT_SOFT_INFLIGHT minus active 429 penalties (each expires on its own after
    ACCOUNT_429_CAP_PENALTY_SECS, so the cap is restored one step at a time), >= 1."""
    now = time.time() if now is None else now
    base = max(1, _envi("ACCOUNT_SOFT_INFLIGHT", 2))
    ov = _lookup(_per_account("ACCOUNT_SOFT_INFLIGHT_OVERRIDES"), getattr(client, "account_key", ""))
    if ov is not None:
        base = max(1, int(ov))  # per-account soft cap (e.g. a workspace that 503s under load)
    with _track_lock:
        pen = client.__dict__.get("_cap_pen")
        if pen:
            pen[:] = [x for x in pen if x > now]
        n = len(pen or [])
    return max(1, base - n * max(0, _envi("ACCOUNT_429_CAP_PENALTY", 1)))


def hard_cap() -> int:
    return max(1, _envi("ACCOUNT_HARD_INFLIGHT", 6))


def note_rate_limited(client) -> None:
    """A fair-use 429 on this account: lower its soft cap by ACCOUNT_429_CAP_PENALTY for
    ACCOUNT_429_CAP_PENALTY_SECS (stacking, each step expiring separately)."""
    if not load_aware():
        return
    secs = _envf("ACCOUNT_429_CAP_PENALTY_SECS", 600.0)
    with _track_lock:
        client.__dict__.setdefault("_cap_pen", []).append(time.time() + secs)
    logger.info("Account soft cap lowered (429)", extra={"request_info": {
        "event": "account_cap_penalty", "account": getattr(client, "account_key", ""),
        "soft_cap": soft_cap(client), "penalty_secs": secs}})


class AccountPool:
    def __init__(self, accounts: List[dict]):
        """
        从配置列表初始化，每个 dict 对应一组凭据。
        同时初始化客户端实例和它们的状态。
        """
        accounts = list(accounts or [])
        ws.assign_keys(accounts)
        self.clients = [NotionOpusAPI(acc) for acc in accounts]
        # 记录每个客户端的冷却释放时间戳（0 表示可用）
        self.cooldown_until = [0.0 for _ in self.clients]
        self._fingerprints = [self._fp(acc) for acc in (accounts or [])]

        # 轮询索引
        self._current_index = 0
        self._lock = threading.Lock()
        self._last_primary = None
        self._last_primary_by: dict = {}
        for c in self.clients:
            c._pool = self

    @staticmethod
    def _fp(acc: dict) -> tuple:
        import hashlib
        tok = hashlib.sha256(str(acc.get("token_v2", "")).encode()).hexdigest()
        return (tok, acc.get("space_id"), acc.get("user_id"), acc.get("_label"))

    def reload(self, accounts: List[dict]) -> None:
        """热更新账号列表：未变化的账号复用原客户端（保留 Cloudflare 会话与冷却状态）。"""
        accounts = list(accounts or [])
        ws.assign_keys(accounts)
        with self._lock:
            old = {fp: (c, cd) for fp, c, cd in zip(self._fingerprints, self.clients, self.cooldown_until)}
            clients, cds, fps = [], [], []
            for acc in accounts:
                fp = self._fp(acc)
                if fp in old:
                    c, cd = old[fp]
                    c.account_key = acc.get("_account_key") or c.account_key
                    if c.capabilities is None and acc.get("_capabilities"):
                        c.capabilities = acc.get("_capabilities")
                else:
                    c, cd = NotionOpusAPI(acc), 0.0
                clients.append(c); cds.append(cd); fps.append(fp)
            self.clients, self.cooldown_until, self._fingerprints = clients, cds, fps
            for c in clients:
                c._pool = self
            self._current_index = 0
        logger.info("Account pool reloaded", extra={"request_info": {"event": "account_pool_reloaded", "accounts": len(clients)}})

    def cooling_map(self) -> Dict[int, int]:
        now = time.time()
        with self._lock:
            return {getattr(c, "admin_id", None): max(0, int(cd - now)) for c, cd in zip(self.clients, self.cooldown_until)}
        
    def get_client(self, wait_if_cooling: bool = True, avoid=None, model=None) -> NotionOpusAPI:
        """
        轮询（Round-Robin）返回下一个可用客户端。
        过滤掉正处于冷却期中的客户端。

        如果 wait_if_cooling=True（默认），当所有账号都在冷却时，
        会等待最近的冷却结束后返回，而不是抛异常。
        """
        if load_aware() or (model and ws.multi_enabled()) or any(ws.is_gpt_only(c) for c in self.clients):
            # a GPT-only workspace never takes a request that is not explicitly a
            # GPT-only model (no model given, or NOTION_MULTI_WORKSPACE off)
            return self._get_for_model(wait_if_cooling, avoid, model)
        if strategy() == "headroom":
            return self._get_headroom(wait_if_cooling, avoid)
        if strategy() == "priority":
            return self._get_priority(wait_if_cooling, avoid)
        now = time.time()
        with self._lock:
            if not self.clients:
                raise RuntimeError("没有可用的 Notion 账号，请在管理面板中添加或启用账号。")
            start_index = self._current_index
            
            while True:
                if not self.clients:
                    raise RuntimeError("没有可用的 Notion 账号，请在管理面板中添加或启用账号。")
                self._current_index %= len(self.clients)
                idx = self._current_index
                # 如果过了冷却时间，视为可用（额度耗尽的账号在还有其他账号时跳过）
                if self.cooldown_until[idx] <= now and not (is_exhausted(self.clients[idx], now)
                                                           and any(not is_exhausted(c, now) for c in self.clients)):
                    # 轮询步进
                    self._current_index = (self._current_index + 1) % len(self.clients)
                    return self.clients[idx]
                    
                # 不可用则顺延
                self._current_index = (self._current_index + 1) % len(self.clients)
                
                # 如果转了一圈都没找到可用的
                if self._current_index == start_index:
                    next_available = min(self.cooldown_until)
                    wait_seconds = max(0.5, next_available - now)

                    if wait_if_cooling and wait_seconds <= 15:
                        # 等待冷却结束后重新尝试
                        logger.info(
                            f"All accounts cooling, waiting {wait_seconds:.1f}s",
                            extra={
                                "request_info": {
                                    "event": "account_pool_wait_cooling",
                                    "wait_seconds": round(wait_seconds, 1),
                                }
                            },
                        )
                        # 释放锁再 sleep，避免阻塞其他线程
                        self._lock.release()
                        try:
                            time.sleep(wait_seconds)
                        finally:
                            self._lock.acquire()
                        # 更新时间后重新扫描
                        now = time.time()
                        continue

                    raise RuntimeError(
                        f"所有账号冷却中，请在 {max(1, int(wait_seconds))} 秒后重试。"
                    )

    def get_status_summary(self) -> Dict[str, int]:
        """返回账号池简要状态，供健康检查和日志使用。"""
        now = time.time()
        with self._lock:
            active = sum(1 for ts in self.cooldown_until if ts <= now)
            cooling = len(self.cooldown_until) - active
            out = {
                "total": len(self.clients),
                "active": active,
                "cooling": cooling,
            }
        out["inflight"] = sum(load_of(c, now)["active"] for c in list(self.clients))
        out["reserved"] = sum(load_of(c, now)["pending"] for c in list(self.clients))
        return out
                    
    def other_free(self, client, model=None) -> bool:
        """Another account (not `client`) that can serve `model` is usable right now."""
        now = time.time()
        with self._lock:
            allowed = self._allowed(model)
            return any(c is not client and cd <= now and not is_exhausted(c, now) and (allowed is None or i in allowed)
                       for i, (c, cd) in enumerate(zip(self.clients, self.cooldown_until)))

    # ---------------- model-aware routing (NOTION_MULTI_WORKSPACE) ----------------
    def _allowed(self, model) -> set | None:
        t = ws.tiers(self.clients, model) if model else None
        if t is None:
            if not any(ws.is_gpt_only(c) for c in self.clients):
                return None
            return {i for i, c in enumerate(self.clients) if not ws.is_gpt_only(c)}
        return {i for tier in t for i in tier}

    def _get_for_model(self, wait_if_cooling: bool, avoid, model) -> NotionOpusAPI:
        """Tiered pick: GPT-only models go to GPT-only workspaces first, then (fallback)
        to Claude-capable ones; Claude models never go to a GPT-only workspace. Within a
        tier the configured strategy picks; a tier is skipped when none of it is free."""
        if load_aware():
            return self._get_load_aware(wait_if_cooling, avoid, model)
        while True:
            now = time.time()
            with self._lock:
                tiers = ws.tiers(self.clients, model) or [[i for i, c in enumerate(self.clients) if not ws.is_gpt_only(c)]]
                if not any(tiers):
                    raise RuntimeError(f"没有可服务模型 {model} 的 Notion 工作区（Claude 请求不会路由到仅 GPT 工作区）。")
                waits = []
                for ti, tier in enumerate(tiers):
                    usable = [i for i in tier if self.cooldown_until[i] <= now and not is_exhausted(self.clients[i], now)]
                    if not usable:
                        cd = [self.cooldown_until[i] for i in tier if not is_exhausted(self.clients[i], now)]
                        if cd:
                            waits.append(min(cd) - now)
                        continue
                    c = self._pick_in(usable, avoid, now)
                    c.last_pick_reason = (getattr(c, "last_pick_reason", "") or "") + ("|tier%d" % (ti + 1))
                    logger.info("Account pick (model-aware)", extra={"request_info": {
                        "event": "account_pick_model", "model": model, "tier": ti + 1,
                        "tier_sizes": [len(t) for t in tiers], "to": getattr(c, "account_key", ""),
                        "capability": ws.capability_label(getattr(c, "capabilities", None))}})
                    return c
                # every tier busy: if all are exhausted, try the one whose recheck comes first
                if not waits:
                    flat = [i for t in tiers for i in t]
                    i = min(flat, key=lambda i: blocked_until(self.clients[i]))
                    return self.clients[i]
                wait = max(0.5, min(waits))
            if not wait_if_cooling or wait > 15:
                raise RuntimeError(f"所有可用账号冷却中，请在 {max(1, int(wait))} 秒后重试。")
            time.sleep(wait)

    # ---------------- headroom + load-aware overflow (POOL_LOAD_AWARE) ----------------
    def _ranked(self, idxs: list, avoid, now: float):
        """(primary_idx, ranked idxs best first, scores) among usable idxs (lock held).
        Headroom-eligible accounts by score (ties: priority order), with the
        HEADROOM_SWITCH_MARGIN hysteresis deciding the primary; none eligible -> priority order."""
        if avoid:
            fresh = [i for i in idxs if getattr(self.clients[i], "account_key", "") not in avoid]
            idxs = fresh or idxs
        scores = {}
        rows = []
        if strategy() == "headroom":
            rows = [r for r in self.headroom_scores(now) if r["idx"] in idxs and r["eligible"]]
        if rows:
            pos = {r["idx"]: k for k, r in enumerate(rows)}  # priority order
            rows.sort(key=lambda r: (-r["score"], pos[r["idx"]]))
            scores = {r["idx"]: r["score"] for r in rows}
            best = rows[0]
            label = ws.capability_label(getattr(self.clients[best["idx"]], "capabilities", None))
            if spread_mode() == "p2c":
                pick = self._spread_pick(rows, now)
                if pick is not None:
                    self._last_primary_by[label] = pick["account"]
                    ranked = [pick["idx"]] + [r["idx"] for r in rows if r is not pick]
                    return pick["idx"], ranked, scores, "headroom_spread"
            prev = next((r for r in rows if r["account"] == self._last_primary_by.get(label)), None)
            if prev is not None and prev is not best and best["score"] <= prev["score"] * (1 + _envf("HEADROOM_SWITCH_MARGIN", 0.15)):
                best = prev
            self._last_primary_by[label] = best["account"]
            ranked = [best["idx"]] + [r["idx"] for r in rows if r is not best]
            return best["idx"], ranked, scores, "headroom"
        ranked = [i for i in self.ordered_indices() if i in idxs]
        if limit_guard() and len(ranked) > 1:
            # nobody headroom-eligible (e.g. every account >= HEADROOM_MAX_ROLLING_PCT): least
            # rolling usage first, accounts >= POOL_SOFT_AVOID_ROLLING_PCT (90) last, instead
            # of static priority (which kept feeding an 83% account until it hit 104%)
            soft = _envf("POOL_SOFT_AVOID_ROLLING_PCT", 90.0)
            pos = {i: k for k, i in enumerate(ranked)}

            def _rk(i):
                st = getattr(self.clients[i], "usage_status", None) or {}
                rp = st.get("rolling_pct")
                if not isinstance(rp, (int, float)):
                    return (1, 0.0, pos[i])  # unknown: after the known-cool ones
                return (0 if rp < soft else 2, float(rp), pos[i])
            ranked.sort(key=_rk)
            return ranked[0], ranked, scores, "rolling_fallback"
        return ranked[0], ranked, scores, "priority"

    def _spread_pick(self, rows: list, now: float):
        """Power-of-two-choices among the strongest headroom rows (sorted best first).
        None when fewer than two candidates qualify (caller keeps the old primary logic)."""
        k = max(2, _envi("POOL_SPREAD_TOPK", 4))
        ratio = min(1.0, max(0.0, _envf("POOL_SPREAD_MIN_RATIO", 0.5)))
        top = float(rows[0].get("score") or 0.0)
        cl = self.clients
        cands = [r for r in rows[:k]
                 if float(r.get("score") or 0.0) >= top * ratio
                 and load(cl[r["idx"]], now) < soft_cap(cl[r["idx"]], now)]
        if len(cands) < 2:
            return None
        w = [max(1e-6, float(r.get("score") or 0.0)) for r in cands]
        a = _spread_rng.choices(range(len(cands)), weights=w)[0]
        rest = [i for i in range(len(cands)) if i != a]
        b = _spread_rng.choices(rest, weights=[w[i] for i in rest])[0]
        la, lb = load(cl[cands[a]["idx"]], now), load(cl[cands[b]["idx"]], now)
        return cands[b] if lb < la else cands[a]

    def _choose_la(self, idxs: list, avoid, now: float):
        """-> (idx, why, ranked, scores) or (None, 'full', ranked, scores) when every
        candidate is at its soft cap."""
        primary, ranked, scores, basis = self._ranked(idxs, avoid, now)
        cl = self.clients
        if load(cl[primary], now) < soft_cap(cl[primary], now):
            return primary, basis + ":primary_under_cap", ranked, scores
        n = max(1, _envi("HEADROOM_TOPN", 3))
        key = lambda i: (load(cl[i], now), -(scores.get(i) or 0), ranked.index(i))
        under = [i for i in ranked[1:n] if load(cl[i], now) < soft_cap(cl[i], now)]
        if under:
            return min(under, key=key), basis + ":overflow_least_loaded", ranked, scores
        under = [i for i in ranked[n:] if load(cl[i], now) < soft_cap(cl[i], now)]
        if under:
            return min(under, key=key), basis + ":overflow_beyond_topn", ranked, scores
        return None, "full", ranked, scores

    def _get_load_aware(self, wait_if_cooling: bool, avoid, model) -> NotionOpusAPI:
        """Per pool (tiers from app.workspaces): the primary account while its in-flight
        load < soft cap, else the least-loaded of the top HEADROOM_TOPN (tie: score), else
        any other under cap; GPT tier 1 full -> tier 2 (fallback) under cap. Everything at
        cap: queue up to POOL_QUEUE_TIMEOUT s, then the least-loaded anyway (never reject
        for load). Reserves the pick so simultaneous picks see it."""
        t0 = time.time()
        qt = _envf("POOL_QUEUE_TIMEOUT", 20.0) if wait_if_cooling else 0.0
        while True:
            now = time.time()
            with self._lock:
                tiers = ws.tiers(self.clients, model) or [[i for i, c in enumerate(self.clients) if not ws.is_gpt_only(c)]]
                if not any(tiers):
                    raise RuntimeError(f"没有可服务模型 {model} 的 Notion 工作区（Claude 请求不会路由到仅 GPT 工作区）。")
                usable_tiers = [[i for i in t if self.cooldown_until[i] <= now and not is_exhausted(self.clients[i], now)]
                                for t in tiers]
                if any(usable_tiers):
                    first = None
                    for ti, usable in enumerate(usable_tiers):
                        if not usable:
                            continue
                        idx, why, ranked, scores = self._choose_la(usable, avoid, now)
                        if first is None:
                            first = (ti, ranked, scores)
                        if idx is not None:
                            return self._take_la(idx, why, ti, tiers, ranked, scores, model, now, t0)
                    if now - t0 >= qt:
                        ti, ranked, scores = first
                        idx = min(ranked, key=lambda i: (load(self.clients[i], now), ranked.index(i)))
                        return self._take_la(idx, "forced_least_loaded", ti, tiers, ranked, scores, model, now, t0)
                    wait = 0.25
                    cooling = False
                else:
                    flat = [i for t in tiers for i in t]
                    cds = [self.cooldown_until[i] for i in flat if not is_exhausted(self.clients[i], now)]
                    if not cds:  # all exhausted: the one whose recheck comes first
                        i = min(flat, key=lambda i: blocked_until(self.clients[i]))
                        reserve(self.clients[i])
                        return self.clients[i]
                    wait = max(0.5, min(cds) - now)
                    cooling = True
            if cooling and (not wait_if_cooling or wait > 15):
                raise RuntimeError(f"所有可用账号冷却中，请在 {max(1, int(wait))} 秒后重试。")
            time.sleep(wait)

    def _take_la(self, idx, why, ti, tiers, ranked, scores, model, now, t0) -> NotionOpusAPI:
        c = self.clients[idx]
        reserve(c)
        n = max(1, _envi("HEADROOM_TOPN", 3))
        c.last_pick_reason = "la:%s|tier%d" % (why, ti + 1)
        logger.info("Account pick (load-aware)", extra={"request_info": {
            "event": "account_pick_la", "model": model, "pool": ws.capability_label(getattr(c, "capabilities", None)),
            "tier": ti + 1, "tier_sizes": [len(t) for t in tiers], "to": getattr(c, "account_key", ""), "why": why,
            "queued_ms": int((time.time() - t0) * 1000),
            "load": {getattr(self.clients[i], "account_key", ""): "%d/%d%s" % (
                load(self.clients[i], now) - (1 if i == idx else 0), soft_cap(self.clients[i], now),
                "" if scores.get(i) is None else " s=%s" % scores.get(i)) for i in ranked[:max(n, 1) + 2]}}})
        return c

    def acquire_sticky(self, client, timeout: float | None = None) -> bool:
        """A sticky thread may run on its account up to ACCOUNT_HARD_INFLIGHT; above that
        wait up to THREAD_REUSE_WAIT_INFLIGHT s, then give up (caller starts a new thread
        elsewhere). Reserves on success. Always True when POOL_LOAD_AWARE is off."""
        if not load_aware():
            return True
        timeout = _envf("THREAD_REUSE_WAIT_INFLIGHT", 10.0) if timeout is None else timeout
        t0 = time.time()
        while True:
            with self._lock:
                l = load(client)
                if l < hard_cap():
                    reserve(client)
                    if time.time() - t0 > 0.3:
                        logger.info("Sticky account had capacity after wait", extra={"request_info": {
                            "event": "sticky_wait_ok", "account": getattr(client, "account_key", ""),
                            "waited_ms": int((time.time() - t0) * 1000), "load": l}})
                    return True
            if time.time() - t0 >= timeout:
                logger.info("Sticky account over hard cap", extra={"request_info": {
                    "event": "sticky_over_cap", "account": getattr(client, "account_key", ""), "load": l,
                    "hard_cap": hard_cap(), "waited_ms": int((time.time() - t0) * 1000)}})
                return False
            time.sleep(0.25)

    def inflight_snapshot(self) -> dict:
        now = time.time()
        return {getattr(c, "account_key", ""): {**load_of(c, now), "soft_cap": soft_cap(c, now)} for c in list(self.clients)}

    def _pick_in(self, idxs: list, avoid, now: float) -> NotionOpusAPI:
        """Pick among usable indices (lock held) with the configured strategy."""
        if avoid:
            fresh = [i for i in idxs if getattr(self.clients[i], "account_key", "") not in avoid]
            idxs = fresh or idxs
        if strategy() == "headroom":
            rows = [r for r in self.headroom_scores(now) if r["idx"] in idxs and r["eligible"]]
            if rows:
                best = rows[0]
                for r in rows:  # max score, ties -> priority order
                    if r["score"] > best["score"]:
                        best = r
                margin = _envf("HEADROOM_SWITCH_MARGIN", 0.15)
                prev_key = self._last_primary_by.get(ws.capability_label(getattr(self.clients[best["idx"]], "capabilities", None)))
                prev = next((r for r in rows if r["account"] == prev_key), None)
                why = "best_score"
                if prev is not None and prev is not best and best["score"] <= prev["score"] * (1 + margin):
                    best, why = prev, "kept_within_margin"
                c = self.clients[best["idx"]]
                self._last_primary_by[ws.capability_label(getattr(c, "capabilities", None))] = best["account"]
                c.last_pick_reason = "headroom:%s:%s" % (why, best["score"])
                return c
            order = [i for i in self.ordered_indices() if i in idxs]
            c = self.clients[order[0]]
            c.last_pick_reason = "headroom:fallback_priority"
            return c
        if strategy() == "priority":
            order = [i for i in self.ordered_indices() if i in idxs]
            c = self.clients[order[0]]
            c.last_pick_reason = "priority"
            return c
        # round robin within the tier
        n = len(self.clients)
        for k in range(n):
            i = (self._current_index + k) % n
            if i in idxs:
                self._current_index = (i + 1) % n
                return self.clients[i]
        return self.clients[idxs[0]]

    @staticmethod
    def mark_ok(client) -> None:
        """A request on this account produced an answer: reset its failure streak."""
        if getattr(client, "fail_streak", 0):
            logger.info("Account recovered", extra={"request_info": {
                "event": "account_recovered", "account": getattr(client, "account_key", ""),
                "after_failures": client.fail_streak}})
        client.fail_streak = 0

    def mark_failed(self, client: NotionOpusAPI, cooldown_seconds: int = 3):
        """
        标记某个客户端为临时不可用（默认冷却 3 秒后恢复）。
        Consecutive failures (no successful answer in between) escalate the cooldown
        (ACCOUNT_FAIL_BACKOFF_BASE * 2^(n-2), capped at ACCOUNT_FAIL_BACKOFF_MAX) so a
        priority account stuck in Notion's fair-use throttle is not retried, at the cost
        of a failed round trip, on every request.
        """
        with self._lock:
            try:
                idx = self.clients.index(client)
                streak = int(getattr(client, "fail_streak", 0) or 0) + 1
                client.fail_streak = streak
                if streak >= 2:
                    base = float(os.getenv("ACCOUNT_FAIL_BACKOFF_BASE", "15") or 15)
                    cap = float(os.getenv("ACCOUNT_FAIL_BACKOFF_MAX", "600") or 600)
                    cooldown_seconds = int(max(cooldown_seconds, min(cap, base * 2 ** (streak - 2))))
                # 记录未来的冷却解封时间（never shorten an existing longer cooldown）
                self.cooldown_until[idx] = max(self.cooldown_until[idx], time.time() + cooldown_seconds)
                logger.warning(
                    "Account marked as failed",
                    extra={
                        "request_info": {
                            "event": "account_failed",
                            "account": client.account_key,
                            "space_id": client.space_id,
                            "cooldown_seconds": cooldown_seconds,
                            "fail_streak": streak,
                        }
                    },
                )
                failed_ok = True
            except ValueError:
                failed_ok = False
                logger.warning(
                    "Attempted to mark unknown account as failed",
                    extra={"request_info": {"event": "account_failed_unknown"}},
                )
        if failed_ok:
            # a failure may be Notion's rolling/monthly usage limit (empty stream, 503
            # temporarily-unavailable, fair-use 429): one cheap status lookup (debounced)
            # marks the account exhausted until its exact reset time. Never a model call.
            try:
                from app import usage_monitor
                usage_monitor.trigger_check(self, client, "failure")
            except Exception:
                pass

    # ---------------- priority / failover ----------------
    def ordered_indices(self) -> List[int]:
        """Priority order: ACCOUNT_PRIORITY (comma list of admin ids, labels or emails),
        remaining accounts in admin-panel order."""
        prio = [x.strip().lower() for x in os.getenv("ACCOUNT_PRIORITY", "").split(",") if x.strip()]

        def rank(i: int):
            c = self.clients[i]
            names = {str(getattr(c, "admin_id", "")).lower(), str(getattr(c, "account_label", "")).lower(),
                     str(getattr(c, "account_key", "")).lower()}
            for r, p in enumerate(prio):
                if p in names:
                    return (r, i)
            return (len(prio), i)
        return sorted(range(len(self.clients)), key=rank)

    def _get_priority(self, wait_if_cooling: bool, avoid=None) -> NotionOpusAPI:
        while True:
            now = time.time()
            with self._lock:
                if not self.clients:
                    raise RuntimeError("没有可用的 Notion 账号，请在管理面板中添加或启用账号。")
                order = self.ordered_indices()
                cands = [i for i in order if not is_exhausted(self.clients[i], now)]
                if not cands:  # all exhausted: try the one whose recheck comes first
                    cands = sorted(order, key=lambda i: blocked_until(self.clients[i]))
                free = [i for i in cands if self.cooldown_until[i] <= now]
                if avoid:  # accounts this request already failed on go last
                    fresh = [i for i in free if getattr(self.clients[i], "account_key", "") not in avoid]
                    free = fresh or free
                if free:
                    c = self.clients[free[0]]
                    key = getattr(c, "account_key", "")
                    if key != self._last_primary:
                        logger.info("Account switch", extra={"request_info": {
                            "event": "account_switch", "from": self._last_primary, "to": key,
                            "strategy": "priority"}})
                        self._last_primary = key
                    return c
                wait = max(0.5, min(self.cooldown_until[i] for i in cands) - now)
            if not wait_if_cooling or wait > 15:
                raise RuntimeError(f"所有账号冷却中，请在 {max(1, int(wait))} 秒后重试。")
            time.sleep(wait)

    # ---------------- headroom (new threads only) ----------------
    def headroom_scores(self, now: float | None = None) -> List[dict]:
        """score = monthly_remaining_pct / days_to_monthly_reset per account, from the cached
        usage_status (usage_monitor). eligible=False with a reason when excluded."""
        now = time.time() if now is None else now
        max_roll = _envf("HEADROOM_MAX_ROLLING_PCT", 70.0)
        max_mon = _envf("HEADROOM_MAX_MONTHLY_PCT", _envf("USAGE_DEMOTE_PCT", 95.0))
        stale = _envf("HEADROOM_STALE_SECONDS", 1800.0)
        min_days = _envf("HEADROOM_MIN_DAYS", 0.5)
        out = []
        for i in self.ordered_indices():
            c = self.clients[i]
            st = getattr(c, "usage_status", None) or {}
            row = {"idx": i, "account": getattr(c, "account_key", ""), "score": None, "eligible": False}
            mp, rp, chk = st.get("monthly_pct"), st.get("rolling_pct"), st.get("checked_at") or 0
            row.update(monthly_pct=mp, rolling_pct=rp)
            if is_exhausted(c, now):
                row["why"] = "exhausted"
            elif self.cooldown_until[i] > now:
                row["why"] = "cooling"
            elif not st or mp is None or now - chk > stale:
                row["why"] = "no_fresh_usage"
            elif st.get("status") != "within_limit":
                row["why"] = "status:" + str(st.get("status"))
            elif mp >= max_mon:
                row["why"] = "monthly>=%g" % max_mon
            elif rp is not None and rp >= max_roll:
                row["why"] = "rolling>=%g" % max_roll
            else:
                reset = st.get("monthly_reset_at")
                days = max(min_days, (reset - now) / 86400.0) if reset else 30.0
                raw = max(0.0, 100.0 - mp) / days
                pen = rolling_penalty(rp, max_roll)
                wgt = account_weight(row["account"])
                row.update(eligible=True, days=round(days, 2), raw_score=round(raw, 3),
                           penalty=round(pen, 3), score=round(raw * (1.0 - pen) * wgt, 3), why="ok")
                if wgt != 1.0:
                    row["weight"] = wgt
            out.append(row)
        return out

    def _get_headroom(self, wait_if_cooling: bool, avoid=None) -> NotionOpusAPI:
        now = time.time()
        with self._lock:
            rows = self.headroom_scores(now) if self.clients else []
            ok = [r for r in rows if r["eligible"]]
            if avoid:
                fresh = [r for r in ok if r["account"] not in avoid]
                ok = fresh or ok
            if ok:
                best = max(ok, key=lambda r: r["score"])  # ties: priority order (stable max)
                for r in ok:
                    if r["score"] == best["score"]:
                        best = r
                        break
                # hysteresis: keep the previous pick unless beaten by a clear margin
                margin = _envf("HEADROOM_SWITCH_MARGIN", 0.15)
                prev = next((r for r in ok if r["account"] == self._last_primary), None)
                why = "best_score"
                if prev is not None and prev is not best and best["score"] <= prev["score"] * (1 + margin):
                    best, why = prev, "kept_within_margin"
                c = self.clients[best["idx"]]
                key = best["account"]
                logger.info("Account pick", extra={"request_info": {
                    "event": "account_pick", "strategy": "headroom", "to": key, "why": why,
                    "score": best["score"], "avoid": sorted(avoid) if avoid else None,
                    "scores": {r["account"]: (r["score"] if r["eligible"] else r["why"]) for r in rows},
                    "rolling_penalty": {r["account"]: {"rolling_pct": r.get("rolling_pct"), "raw": r.get("raw_score"),
                                                       "penalty": r.get("penalty")}
                                        for r in rows if r.get("penalty")} or None}})
                if key != self._last_primary:
                    logger.info("Account switch", extra={"request_info": {
                        "event": "account_switch", "from": self._last_primary, "to": key,
                        "strategy": "headroom"}})
                    self._last_primary = key
                c.last_pick_reason = "headroom:%s:%s" % (why, best["score"])
                return c
            logger.info("Account pick fallback", extra={"request_info": {
                "event": "account_pick", "strategy": "headroom", "why": "fallback_priority",
                "scores": {r["account"]: r["why"] for r in rows}}})
        c = self._get_priority(wait_if_cooling, avoid)
        c.last_pick_reason = "headroom:fallback_priority"
        return c

    def get_client_by_key(self, key: str, model=None):
        """The client with this account_key if it is usable right now (and, with
        NOTION_MULTI_WORKSPACE, can serve `model`), else None."""
        now = time.time()
        with self._lock:
            for c, cd in zip(self.clients, self.cooldown_until):
                if getattr(c, "account_key", "") == key:
                    if model and not ws.serves(c, model):
                        return None
                    return c if cd <= now and not is_exhausted(c, now) else None
        return None

    def key_cooldown_left(self, key: str) -> float:
        """Seconds until the account with this key leaves a (non-exhausted) cooldown;
        0 if usable now, -1 if unknown or exhausted."""
        now = time.time()
        with self._lock:
            for c, cd in zip(self.clients, self.cooldown_until):
                if getattr(c, "account_key", "") == key:
                    return -1.0 if is_exhausted(c, now) else max(0.0, cd - now)
        return -1.0

    def mark_exhausted(self, client, seconds: float | None = None, reason: str = "") -> None:
        secs = exhaust_seconds() if seconds is None else seconds
        client.exhausted_until = time.time() + secs
        client.exhausted_reason = reason[:200]
        logger.warning("Account quota exhausted", extra={"request_info": {
            "event": "account_exhausted", "account": getattr(client, "account_key", ""),
            "recheck_in": int(secs), "reason": reason[:200]}})

    def clear_exhausted(self, client) -> None:
        client.exhausted_until = 0
        client.exhausted_reason = ""
        logger.info("Account quota cleared", extra={"request_info": {
            "event": "account_exhausted_cleared", "account": getattr(client, "account_key", "")}})

    def status_map(self) -> Dict[int, dict]:
        now = time.time()
        with self._lock:
            order = self.ordered_indices()
            out = {}
            for rank, i in enumerate(order):
                c = self.clients[i]
                ex = is_exhausted(c, now)
                out[getattr(c, "admin_id", None)] = {
                    "state": "exhausted" if ex else ("cooling" if self.cooldown_until[i] > now else "active"),
                    "priority": rank + 1,
                    "exhausted_until": int(blocked_until(c, now)) if ex else None,
                    "exhausted_reason": (getattr(c, "exhausted_reason", "") or ("usage: over limit (limit guard)" if usage_blocked_until(c, now) else "")) if ex else "",
                    "usage": getattr(c, "usage_status", None),
                    "account_key": getattr(c, "account_key", ""),
                    "capability": ws.capability_label(getattr(c, "capabilities", None)),
                    "capability_checked_at": int((getattr(c, "capabilities", None) or {}).get("checked_at") or 0) or None,
                    "workspace_name": getattr(c, "workspace_name", ""),
                    "inflight": load_of(c, now),
                    "soft_cap": soft_cap(c, now),
                    "is_primary": strategy() == "priority" and getattr(c, "account_key", "") == self._last_primary,
                }
            return out
