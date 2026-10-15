import os
import time
import threading
from typing import Dict, List

from app.logger import logger
from app.notion_client import NotionOpusAPI

def strategy() -> str:
    return os.getenv("ACCOUNT_STRATEGY", "round_robin").strip().lower()


def exhaust_seconds() -> float:
    return float(os.getenv("ACCOUNT_EXHAUSTED_RECHECK_SECONDS", "1800") or 1800)


def is_exhausted(client, now: float | None = None) -> bool:
    return (getattr(client, "exhausted_until", 0) or 0) > (now or time.time())


class AccountPool:
    def __init__(self, accounts: List[dict]):
        """
        从配置列表初始化，每个 dict 对应一组凭据。
        同时初始化客户端实例和它们的状态。
        """
        self.clients = [NotionOpusAPI(acc) for acc in (accounts or [])]
        # 记录每个客户端的冷却释放时间戳（0 表示可用）
        self.cooldown_until = [0.0 for _ in self.clients]
        self._fingerprints = [self._fp(acc) for acc in (accounts or [])]

        # 轮询索引
        self._current_index = 0
        self._lock = threading.Lock()
        self._last_primary = None
        for c in self.clients:
            c._pool = self

    @staticmethod
    def _fp(acc: dict) -> tuple:
        import hashlib
        tok = hashlib.sha256(str(acc.get("token_v2", "")).encode()).hexdigest()
        return (tok, acc.get("space_id"), acc.get("user_id"), acc.get("_label"))

    def reload(self, accounts: List[dict]) -> None:
        """热更新账号列表：未变化的账号复用原客户端（保留 Cloudflare 会话与冷却状态）。"""
        accounts = accounts or []
        with self._lock:
            old = {fp: (c, cd) for fp, c, cd in zip(self._fingerprints, self.clients, self.cooldown_until)}
            clients, cds, fps = [], [], []
            for acc in accounts:
                fp = self._fp(acc)
                if fp in old:
                    c, cd = old[fp]
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
        
    def get_client(self, wait_if_cooling: bool = True, avoid=None) -> NotionOpusAPI:
        """
        轮询（Round-Robin）返回下一个可用客户端。
        过滤掉正处于冷却期中的客户端。

        如果 wait_if_cooling=True（默认），当所有账号都在冷却时，
        会等待最近的冷却结束后返回，而不是抛异常。
        """
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
            return {
                "total": len(self.clients),
                "active": active,
                "cooling": cooling,
            }
                    
    def other_free(self, client) -> bool:
        """Another account (not `client`) is usable right now."""
        now = time.time()
        with self._lock:
            return any(c is not client and cd <= now and not is_exhausted(c, now)
                       for c, cd in zip(self.clients, self.cooldown_until))

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
                    cands = sorted(order, key=lambda i: self.clients[i].exhausted_until)
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

    def get_client_by_key(self, key: str):
        """The client with this account_key if it is usable right now, else None."""
        now = time.time()
        with self._lock:
            for c, cd in zip(self.clients, self.cooldown_until):
                if getattr(c, "account_key", "") == key:
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
                    "exhausted_until": int(c.exhausted_until) if ex else None,
                    "exhausted_reason": getattr(c, "exhausted_reason", "") if ex else "",
                    "usage": getattr(c, "usage_status", None),
                    "is_primary": strategy() == "priority" and getattr(c, "account_key", "") == self._last_primary,
                }
            return out
