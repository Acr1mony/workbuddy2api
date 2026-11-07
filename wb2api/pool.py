"""选号与失败时间窗。

这是整个网关最容易写错的地方，所以先把不变量写清楚：

**一、五类失败各自有独立的时间窗，取最大值**

    throttle_wait = max(cooldown, balance, breaker, degrade, restricted, model) - now

早期只维护一个 ``cooldown_until``，结果是：一次 429 会顺手把 30 分钟的熔断清掉；
反过来 5xx 熔断时又报"限流"，运维照着日志去查 429，查了半天。所以这里坚持
"谁触发谁记账"，并且 ``unavailable_reason()`` 报的是**最长**的那个窗。

**二、"连续失败"必须按时间衰减**

``FAIL_WINDOW=600s``：两次失败间隔超过窗口，计数器就清零。不做这件事的后果是
几小时前一个 502 被当成"第三次连续失败"，一路升到 7200 秒，而运维那边测出来
明明是好的。

**三、无 reset 时钟的裸 429 用更短的阶梯**

真实流量里 229/263 的 429 是 ``usage exceeds frequency limit`` 这种不带 reset
的裸 429，按凭证级阶梯（600s 起、2h 封顶）会白白停掉整个账号；它单独走
60s 起、600s 封顶的阶梯。

**四、11140 绝不换号**

``11140``（request illegal / 账号异常）是风控标记。换号重试只会把池子里的号一个
个点掉（实测一轮请求封了 7 个），所以这个错误直接终止请求；另外 2 个不同账号在
900 秒内都撞上时，整个域暂停 30 分钟。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import util
from .config import realm_value as _realm_value
from .log import get_logger
from .store import Account, ApiKey

log = get_logger("pool")

# --- 失败类别 --------------------------------------------------------------
SOFT_RATE = "soft_rate"  # 凭证级 429（有 reset 时钟时精确冷却）
UNSCOPED_RATE = "unscoped_rate"  # 裸 429，更短的阶梯
HARD_CREDIT = "hard_credit"  # 402 余额不足
BREAKER = "breaker"  # 5xx 熔断
DEGRADE = "degrade"  # 未分类失败的降级
RESTRICTED = "restricted"  # 403 / 11140 风控
SESSION_DEAD = "session_dead"  # 12153 刷新链失效
MODEL_BLOCK = "model_block"  # 11102/11158 模型级不可用
AUTH_DEAD = "auth_dead"  # 需要重新登录

FAIL_WINDOW = 600.0  # 秒；超过它，"连续失败"就重新计数
SOFT_RATE_BASE = 600.0
SOFT_RATE_MAX = 7200.0
UNSCOPED_BASE = 60.0
UNSCOPED_MAX = 600.0
STREAK_SHIFT_MAX = 16
BREAKER_BASE = 1800.0
BREAKER_MAX = 21600.0
DEGRADE_BASE = 600.0
DEGRADE_MAX = 7200.0
MODEL_BLOCK_BASE = 21600.0
MODEL_BLOCK_SHIFT = 4
MODEL_BLOCK_MAX = 86400.0
RESTRICTED_SECONDS = 7200.0
WAF_COOLDOWN = 60.0  # 内容审核/WAF 短避让，不升级、不持久化
SESSION_DEAD_THRESHOLD = 3
MIN_PICK_GAP = 0.1  # 秒；赢家刚刚被用过就换最久未用的，避免两路请求撞同一个号

# realm 级连坐
RESTRICTION_WAVE_MIN = 2
RESTRICTION_WAVE_WINDOW = 900.0
RESTRICTION_HOLD_SECONDS = 1800.0

WINDOW_NAMES = ("cooldown_until", "balance_until", "breaker_until", "degrade_until", "restricted_until")
WINDOW_LABELS = {
    "cooldown_until": "冷却",
    "balance_until": "余额",
    "breaker_until": "熔断",
    "degrade_until": "降级",
    "restricted_until": "风控",
    "model": "模型限流",
}


def next_local_4am(now: Optional[float] = None) -> float:
    """下一次本地 04:00（上游额度每天 04:00 重置）。"""
    now = util.now() if now is None else now
    lt = time.localtime(now)
    today4 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 4, 0, 0, 0, 0, -1))
    return today4 if today4 > now else today4 + 86400.0


def _shift_capped(base: float, streak: int, cap: float) -> float:
    return min(cap, base * (2.0 ** min(max(0, streak - 1), STREAK_SHIFT_MAX)))


def model_cooldown_until(account: Account, model: str) -> float:
    if not model:
        return 0.0
    entry = account.model_cooldowns.get(model) or {}
    return float(entry.get("until") or 0.0)


def throttle_until(account: Account, model: str = "") -> Tuple[float, str]:
    """返回 (解禁时间, 窗口名)。窗口名用于面板展示，避免"只报冷却"。"""
    best, name = 0.0, ""
    for field in WINDOW_NAMES:
        value = float(getattr(account, field, 0.0) or 0.0)
        if value > best:
            best, name = value, field
    mc = model_cooldown_until(account, model)
    if mc > best:
        best, name = mc, "model"
    return best, name


def unavailable_reason(account: Account, model: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    """给面板用的人类可读原因：报最长的那一个，而不是先遍历到的。"""
    now = util.now() if now is None else now
    until, name = throttle_until(account, model)
    remaining = max(0.0, until - now)
    if account.disabled:
        return {"reason": "已禁用", "kind": "disabled", "remaining": 0.0}
    if remaining <= 0:
        return {"reason": "", "kind": "", "remaining": 0.0}
    entry = account.model_cooldowns.get(model) or {}
    detail = entry.get("reason") if name == "model" else getattr(account, "cooldown_reason", "")
    label = WINDOW_LABELS.get(name, name)
    return {
        "reason": f"{label}至 {util.iso(until)}" + (f"（{detail}）" if detail else ""),
        "kind": name,
        "remaining": remaining,
    }


class Pool:
    """账号池：选号、并发槽、失败记账、以及 realm 级连坐。"""

    def __init__(self, store: Any, cfg: Any, affinity: Any = None) -> None:
        self.store = store
        self.cfg = cfg
        self.affinity = affinity
        self._lock = threading.RLock()
        self._used_seq = 0
        self._rr_cursor: Dict[str, int] = {}
        self._restricted: Dict[str, List[Tuple[str, float]]] = {}
        self._holds: Dict[str, float] = {}
        self._dirty = False
        self._last_flush = util.now()

    # -- 配置 ---------------------------------------------------------------
    def set_config(self, cfg: Any) -> None:
        self.cfg = cfg

    def _pool_cfg(self, *path: str, default: Any = None) -> Any:
        node: Any = self.cfg
        for part in path:
            node = node.get(part) if hasattr(node, "get") else getattr(node, part, None)
            if node is None:
                return default
        return node

    def _duration(self, path: tuple, default: float) -> float:
        """配置里时长写成 "30m" 这种人话，读的时候统一换成秒。"""
        raw = self._pool_cfg(*path, default=None)
        if raw is None or raw == "":
            return default
        if isinstance(raw, (int, float)):
            return float(raw)
        try:
            return float(util.parse_duration(str(raw)))
        except (TypeError, ValueError):
            return default

    def strategy_for(self, realm: str) -> str:
        value = _realm_value(self.cfg, "pool.strategy", realm, "")
        return str(value or "weighted_random")

    def max_in_flight(self, realm: str) -> int:
        base = int(self._pool_cfg("pool", "max_in_flight", default=3) or 0)
        if realm == "global":
            return int(self._pool_cfg("pool", "max_in_flight_global", default=2) or 0)
        return base

    def credit_floor(self) -> float:
        try:
            return float(self._pool_cfg("pool", "credit_floor", default=0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # -- 状态查询 -----------------------------------------------------------
    def is_ready(self, account: Account, model: str = "", now: Optional[float] = None,
                 ignore_slots: bool = False) -> bool:
        now = util.now() if now is None else now
        if account.disabled or not (account.access_token or account.static_key):
            return False
        if throttle_until(account, model)[0] > now:
            return False
        if not ignore_slots:
            cap = self.max_in_flight(account.realm)
            if cap and account.in_flight >= cap:
                return False
        return not self._floor_blocked(account, model)

    def _floor_blocked(self, account: Account, model: str) -> bool:
        """余额低于 floor 时只拦"收费"模型。

        未知成本默认放行——这是两个仓库的实际行为，也是线上必须的：模型目录还没
        预热时把所有模型都拦掉，等于整个网关不可用。但"未知"确实曾是烧干账号的
        元凶，所以留了 ``pool.credit_floor_unknown = "block"`` 给保守部署。
        """
        floor = self.credit_floor()
        if floor <= 0 or account.credit is None:
            return False
        if account.credit >= floor:
            return False
        rate = self._model_rate(model)
        if rate is None:
            return str(self._pool_cfg("pool", "credit_floor_unknown", default="allow")) == "block"
        return rate > 0

    def _model_rate(self, model: str) -> Optional[float]:
        """0 = 免费，正数 = 收费，None = 未知。"""
        rates = self._pool_cfg("pool", "model_rates", default={}) or {}
        value = rates.get(model)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # -- 选号 ---------------------------------------------------------------
    def pick(self, realm: str, model: str, *, key: Optional[ApiKey] = None,
             session_key: str = "", exclude: Iterable[str] = (),
             now: Optional[float] = None) -> Optional[Account]:
        """挑一个账号（不占并发槽）。没有可用账号返回 None。"""
        now = util.now() if now is None else now
        if self.realm_hold_remaining(realm, now) > 0:
            log.debug("realm %s 处于风控暂停中，跳过选号", realm)
            return None
        tried = set(exclude)
        with self._lock:
            candidates = [
                a for a in self.store.list_accounts(realm)
                if a.key not in tried and self._allowed_by_key(a, key) and self.is_ready(a, model, now)
            ]
        if not candidates:
            return None

        # 1) 会话粘滞：同一个对话优先回到上次的账号（上游 prefix cache 按账号隔离）
        if session_key and self.affinity is not None:
            by_uid = {a.uid: a for a in candidates if a.uid}
            for uid in self.affinity.candidates(session_key):
                if uid in by_uid:
                    return by_uid[uid]

        # 2) 按策略挑
        strategy = self.strategy_for(realm)
        if strategy in ("round_robin", "sequential_exhaust"):
            return self._pick_ordered(candidates, realm, model)
        chosen = self._pick_weighted(candidates, now)
        return self._avoid_collision(chosen, candidates, now)

    def _avoid_collision(self, chosen: Account, candidates: Sequence[Account], now: float) -> Account:
        """赢家刚刚被用过就换成最久未用的那个：两路并发请求撞同一个账号，
        在上游就是一次裸 429，而且没有 prefix cache 收益。"""
        try:
            gap = float(self._pool_cfg("pool", "min_pick_gap_ms", default=100) or 0) / 1000.0
        except (TypeError, ValueError):
            gap = 0.1
        if gap <= 0 or not chosen.last_used_at or now - chosen.last_used_at >= gap:
            return chosen
        idle = min(candidates, key=lambda a: a.used_seq or 0)
        return idle if idle is not chosen else chosen

    def _allowed_by_key(self, account: Account, key: Optional[ApiKey]) -> bool:
        if key is None:
            return True
        if key.accounts and account.key not in key.accounts and account.uid not in key.accounts:
            return False
        if key.groups and account.group not in key.groups:
            return False
        return True

    def _pick_ordered(self, candidates: Sequence[Account], realm: str, model: str) -> Account:
        ordered = sorted(candidates, key=lambda a: (a.weight or 0), reverse=True)
        cursor_key = f"{realm}|{model}"
        with self._lock:
            cursor = self._rr_cursor.get(cursor_key, 0)
            self._rr_cursor[cursor_key] = cursor + 1
        # 用过一轮就换下一个起始位置，避免某个号长期吃第一单
        return ordered[cursor % len(ordered)]

    def _pick_weighted(self, candidates: Sequence[Account], now: float) -> Account:
        credits = [a.credit for a in candidates if a.credit is not None]
        max_credits = max(credits) if credits else 1.0
        idle_per_hour = float(self._pool_cfg("pool", "idle_weight_per_hour", default=0.5) or 0.5)
        idle_max = float(self._pool_cfg("pool", "idle_weight_max", default=5.0) or 5.0)
        weights: List[float] = []
        for a in candidates:
            weight = 1.0
            if a.credit is not None and max_credits > 0:
                weight += max(0.0, a.credit / max_credits) * 10.0
            if a.last_used_at:
                idle_hours = max(0.0, (now - a.last_used_at) / 3600.0)
                weight += min(idle_max, idle_hours * idle_per_hour)
            else:
                weight += idle_max  # 从没用过的账号给满额空闲奖励
            weights.append(max(0.1, weight))
        total = sum(weights)
        pick = random.random() * total
        for account, weight in zip(candidates, weights):
            pick -= weight
            if pick <= 0:
                return account
        return candidates[-1]

    # -- 并发槽 -------------------------------------------------------------
    def acquire(self, account: Account) -> bool:
        cap = self.max_in_flight(account.realm)
        with self._lock:
            if cap and account.in_flight >= cap:
                return False
            account.in_flight += 1
            return True

    def release(self, account: Account) -> None:
        with self._lock:
            if account.in_flight > 0:
                account.in_flight -= 1

    def select(self, realm: str, model: str, **kwargs: Any) -> Optional[Account]:
        """选号并占槽；返回的账号必须由调用方 ``release()``。"""
        for _ in range(4):
            account = self.pick(realm, model, **kwargs)
            if account is None:
                return None
            if self.acquire(account):
                self._mark_used(account)
                return account
            # 槽满了：把它排除再挑一次，别原地打转
            exclude = set(kwargs.get("exclude") or ())
            exclude.add(account.key)
            kwargs["exclude"] = exclude
        return None

    def model_blocked(self, realm: str, model: str) -> Dict[str, Any]:
        """整个域的所有可用账号都把这个模型标记为不可用时，返回解禁信息。

        "模型不存在/没接入"是请求本身的问题，客户端该换模型；这时回 503 会让
        客户端以为等一会儿就好，白白重试。所以要能和账号冷却区分开。
        """
        if not model:
            return {}
        now = util.now()
        accounts = [a for a in self.store.list_accounts(realm) if not a.disabled]
        if not accounts:
            return {}
        for account in accounts:
            if model_cooldown_until(account, model) <= now:
                return {}
        untils = [model_cooldown_until(a, model) for a in accounts]
        entry = accounts[0].model_cooldowns.get(model) or {}
        return {"until": min(untils), "reason": str(entry.get("reason") or "")}

    def _mark_used(self, account: Account) -> None:
        with self._lock:
            self._used_seq += 1
            account.used_seq = self._used_seq
            account.last_used_at = util.now()

    # -- 请求节奏 -----------------------------------------------------------
    def min_interval(self) -> float:
        """同一账号两次出站请求之间的最小间隔（秒）。

        真人不会在 200ms 内连发十个请求，但一个被 agent 驱动的客户端会。上游
        看到的是"同一个机器 id 的高频调用"，这是最容易被判成脚本的特征。
        默认 200ms 只压掉明显的连发，正常聊天完全感觉不到。
        """
        return max(0.0, self._duration(("pool", "min_request_interval"), 0.2))

    def pace_delay(self, account: Account, now: Optional[float] = None) -> float:
        """距离这个账号"可以再发一条"还差多少秒（0 表示现在就能发）。"""
        interval = self.min_interval()
        if interval <= 0 or not account.last_chat_at:
            return 0.0
        now = util.now() if now is None else now
        return max(0.0, interval - (now - account.last_chat_at))

    def note_chat_sent(self, account: Account) -> None:
        account.last_chat_at = util.now()

    # -- 记账：成功 ---------------------------------------------------------
    def note_success(self, account: Account, *, model: str = "", session_key: str = "") -> None:
        """一次真正跑通的请求，是"账号确实解禁了"的唯一证据。"""
        with self._lock:
            account.failures = 0
            account.consecutive_fails = 0
            account.soft_streak = 0
            account.unscoped_streak = 0
            account.breaker_retries = 0
            account.cooldown_until = 0.0
            account.cooldown_reason = ""
            account.cooldown_level = ""
            account.balance_until = 0.0
            account.breaker_until = 0.0
            account.degrade_until = 0.0
            account.restricted_until = 0.0
            if model:
                account.model_cooldowns.pop(model, None)
            account.last_error = ""
            account.state = "ready"
            self._dirty = True
        if session_key and self.affinity is not None and account.uid:
            self.affinity.bind(session_key, account.uid)

    # -- 记账：失败 ---------------------------------------------------------
    def note_soft_rate(self, account: Account, *, reset_at: Optional[float] = None,
                       reason: str = "429 rate limit", model: str = "") -> None:
        now = util.now()
        with self._lock:
            max_cool = self._duration(("cooldown", "soft_rate_max"), SOFT_RATE_MAX)
            base = self._duration(("cooldown", "soft_rate"), SOFT_RATE_BASE)
            if reset_at and reset_at > now:
                until = min(reset_at, now + max_cool)
            else:
                self._decay_soft(account, now)
                if account.cooldown_until <= now:  # 已经在冷却中的账号不再翻倍
                    account.soft_streak += 1
                    until = min(now + _shift_capped(base, account.soft_streak, max_cool), now + max_cool)
                else:
                    until = account.cooldown_until
            account.cooldown_until = max(account.cooldown_until, until)
            account.cooldown_reason = reason
            account.cooldown_level = "soft"
            account.last_soft_at = now
            account.last_error = reason
            account.state = "cooldown"
            if model:
                self._set_model_cooldown(account, model, account.cooldown_until, reason)
            self._dirty = True
        log.info("账号 %s 软冷却到 %s（%s）", account.label, util.iso(account.cooldown_until), reason)

    def note_unscoped_rate(self, account: Account, reason: str = "429 unscoped") -> None:
        """裸 429（body 不含 reset）：60s 起、600s 封顶，独立计数器。"""
        now = util.now()
        with self._lock:
            self._decay_soft(account, now)
            account.unscoped_streak += 1
            wait = _shift_capped(UNSCOPED_BASE, account.unscoped_streak, UNSCOPED_MAX)
            account.cooldown_until = max(account.cooldown_until, now + wait)
            account.cooldown_reason = reason
            account.cooldown_level = "soft"
            account.last_soft_at = now
            account.last_error = reason
            account.state = "cooldown"
            self._dirty = True
        log.info("账号 %s 裸 429，冷却 %ds（第 %d 次）", account.label, wait, account.unscoped_streak)

    def note_hard_credit(self, account: Account, reason: str = "余额不足") -> None:
        now = util.now()
        with self._lock:
            account.balance_until = max(account.balance_until, next_local_4am(now))
            account.last_error = reason
            account.state = "cooldown"
            self._dirty = True
        log.info("账号 %s 余额不足，挂起到 %s", account.label, util.iso(account.balance_until))

    def note_failure(self, account: Account, reason: str = "upstream error") -> None:
        """5xx / 传输失败：走熔断阶梯。"""
        now = util.now()
        with self._lock:
            if now - (account.last_fail_at or 0) > FAIL_WINDOW:
                account.failures = 0
            account.failures += 1
            account.last_fail_at = now
            account.last_error = reason
            threshold = int(self._pool_cfg("pool", "breaker_threshold", default=3) or 3)
            if account.failures >= threshold:
                account.failures = 0
                account.breaker_retries += 1
                base = self._duration(("pool", "breaker_cooldown"), BREAKER_BASE)
                cap = self._duration(("pool", "breaker_cooldown_max"), BREAKER_MAX)
                wait = min(cap, base * (2.0 ** min(account.breaker_retries - 1, STREAK_SHIFT_MAX)))
                account.breaker_until = max(account.breaker_until, now + wait)
                account.state = "cooldown"
                log.warning("账号 %s 熔断 %ds（连续 %d 次）", account.label, wait, threshold)
            self._dirty = True

    def note_unknown_failure(self, account: Account, reason: str = "unknown failure") -> None:
        """未分类失败：同时喂两条阶梯（降级 + 熔断计数）。"""
        now = util.now()
        with self._lock:
            if now - (account.last_fail_at or 0) > FAIL_WINDOW:
                account.consecutive_fails = 0
            account.consecutive_fails += 1
            account.last_error = reason
            threshold = int(self._pool_cfg("pool", "degrade_threshold", default=5) or 5)
            if account.consecutive_fails >= threshold:
                account.consecutive_fails = 0
                base = self._duration(("pool", "degrade_cooldown"), DEGRADE_BASE)
                cap = self._duration(("pool", "degrade_cooldown_max"), DEGRADE_MAX)
                # 不延长已有窗口：降级是"先歇一会儿"，不是惩罚升级
                if account.degrade_until <= now:
                    account.degrade_until = now + min(cap, base)
            self._dirty = True
        self.note_failure(account, reason)

    def note_waf(self, account: Account, reason: str = "403 waf") -> None:
        """内容审核 / 边缘 WAF 拦截：一分钟软避让。

        这跟账号健康无关：换号、刷令牌都没用，反而是"换号继续打"最容易连坐。
        所以只做短避让、不动任何升级阶梯，也不写 ``restricted_until``。
        """
        now = util.now()
        with self._lock:
            wait = self._duration(("pool", "waf_cooldown"), WAF_COOLDOWN)
            account.cooldown_until = max(account.cooldown_until, now + wait)
            if account.cooldown_until > now:
                account.cooldown_reason = reason
                account.cooldown_level = "waf"
            account.last_error = reason
            self._dirty = True
        log.info("账号 %s 被内容审核/WAF 拦（%s），避让 %ds", account.label, reason, int(wait))

    def note_restricted(self, account: Account, reason: str = "403 restricted") -> None:
        """403 / 11140：固定 2 小时，且触发 realm 级连坐检测。"""
        now = util.now()
        with self._lock:
            account.restricted_until = max(account.restricted_until, now + RESTRICTED_SECONDS)
            account.last_error = reason
            account.state = "cooldown"
            self._dirty = True
            if account.uid:
                bucket = self._restricted.setdefault(account.realm, [])
                bucket.append((account.uid, now))
                cutoff = now - RESTRICTION_WAVE_WINDOW
                self._restricted[account.realm] = [(u, t) for u, t in bucket if t >= cutoff]
                distinct = {u for u, _t in self._restricted[account.realm]}
                if len(distinct) >= RESTRICTION_WAVE_MIN:
                    hold = self._duration(("pool", "restriction_hold_seconds"), RESTRICTION_HOLD_SECONDS)
                    if hold > 0:
                        self._holds[account.realm] = max(self._holds.get(account.realm, 0.0), now + hold)
                        self._restricted[account.realm] = []
                        log.error("realm %s 在 %ds 内有 %d 个账号被风控，整域暂停 %ds",
                                  account.realm, int(RESTRICTION_WAVE_WINDOW), len(distinct), int(hold))
        log.warning("账号 %s 被风控，限制到 %s", account.label, util.iso(account.restricted_until))

    def note_session_dead(self, account: Account) -> None:
        with self._lock:
            account.failures += 1
            account.last_error = "12153 session dead"
            if account.failures >= SESSION_DEAD_THRESHOLD:
                account.disabled = True
                account.state = "auth_dead"
                account.cooldown_until = 0.0
                account.last_error = f"连续 {SESSION_DEAD_THRESHOLD} 次刷新失败，已禁用"
                log.error("账号 %s 刷新链失效，已禁用", account.label)
            self._dirty = True

    def note_auth_dead(self, account: Account, reason: str = "需要重新登录") -> None:
        with self._lock:
            account.disabled = True
            account.state = "auth_dead"
            account.last_error = reason
            self._dirty = True

    def block_model(self, account: Account, model: str, reason: str = "model not available") -> None:
        """11102 / 11158：只封这个模型，不动账号级窗口。"""
        now = util.now()
        with self._lock:
            entry = account.model_cooldowns.get(model) or {}
            hits = int(entry.get("hits") or 0) + 1
            ttl = min(MODEL_BLOCK_MAX, MODEL_BLOCK_BASE * (2.0 ** min(max(0, hits - 1), MODEL_BLOCK_SHIFT)))
            self._set_model_cooldown(account, model, now + ttl, reason, hits=hits)
            self._dirty = True

    def _set_model_cooldown(self, account: Account, model: str, until: float, reason: str,
                            hits: Optional[int] = None) -> None:
        entry = account.model_cooldowns.get(model) or {}
        entry.update({
            "until": max(float(entry.get("until") or 0.0), until),
            "reason": reason,
            "hits": int(hits if hits is not None else entry.get("hits") or 0),
            "at": util.now(),
        })
        account.model_cooldowns[model] = entry

    def _decay_soft(self, account: Account, now: float) -> None:
        if account.last_soft_at and now - account.last_soft_at > FAIL_WINDOW:
            account.soft_streak = 0
            account.unscoped_streak = 0
        if account.last_fail_at and now - account.last_fail_at > FAIL_WINDOW:
            account.failures = 0
            account.consecutive_fails = 0

    # -- realm 连坐 ---------------------------------------------------------
    def realm_hold_remaining(self, realm: str, now: Optional[float] = None) -> float:
        now = util.now() if now is None else now
        with self._lock:
            until = self._holds.get(realm, 0.0)
            if until <= now:
                if until:
                    self._holds.pop(realm, None)
                return 0.0
            return until - now

    def clamp_restriction_holds(self) -> None:
        """运维调小 hold 时长时，把已经在跑的长倒计时压回去。"""
        hold = self._duration(("pool", "restriction_hold_seconds"), RESTRICTION_HOLD_SECONDS)
        now = util.now()
        with self._lock:
            for realm, until in list(self._holds.items()):
                if hold <= 0:
                    self._holds.pop(realm, None)
                elif until - now > hold:
                    self._holds[realm] = now + hold

    def reset_realm_hold(self, realm: str) -> None:
        with self._lock:
            self._holds.pop(realm, None)
            self._restricted.pop(realm, None)

    # -- 账号管理辅助 -------------------------------------------------------
    def revive(self, account: Account, *, balance_changed: bool = False) -> None:
        """面板上的"测活/刷新余额"用。只清余额窗——充值只证明钱回来了，
        不证明 429 阶梯或熔断已经恢复。"""
        with self._lock:
            if balance_changed:
                account.balance_until = 0.0
            account.last_error = ""
            if throttle_until(account)[0] <= util.now():
                account.state = "ready"
            self._dirty = True

    def clear_all(self, account: Account) -> None:
        with self._lock:
            account.cooldown_until = 0.0
            account.cooldown_reason = ""
            account.cooldown_level = ""
            account.balance_until = 0.0
            account.breaker_until = 0.0
            account.degrade_until = 0.0
            account.restricted_until = 0.0
            account.model_cooldowns.clear()
            account.failures = 0
            account.soft_streak = 0
            account.unscoped_streak = 0
            account.consecutive_fails = 0
            account.breaker_retries = 0
            account.last_error = ""
            account.state = "ready" if not account.disabled else "auth_dead"
            self._dirty = True

    # -- 持久化 -------------------------------------------------------------
    def flush(self, force: bool = False) -> None:
        """池状态每 5 秒刷一次盘；进程退出时必须 force。"""
        now = util.now()
        if not self._dirty:
            return
        if not force and now - self._last_flush < 5.0:
            return
        try:
            self.store.save_accounts()
            self._dirty = False
            self._last_flush = now
        except OSError as exc:  # 磁盘满/占用：只记日志，别让请求线程炸掉
            log.error("池状态落盘失败：%s", exc)

    # -- 面板 ---------------------------------------------------------------
    def stats(self, realm: str = "") -> Dict[str, Any]:
        now = util.now()
        accounts = self.store.list_accounts(realm)
        ready = sum(1 for a in accounts if self.is_ready(a, now=now))
        cooling = sum(1 for a in accounts if not a.disabled and not self.is_ready(a, now=now))
        return {
            "realm": realm or "all",
            "total": len(accounts),
            "ready": ready,
            "cooling": cooling,
            "disabled": sum(1 for a in accounts if a.disabled),
            "in_flight": sum(a.in_flight for a in accounts),
            "credits": round(sum(a.credit or 0 for a in accounts), 2),
            "credit_unknown": sum(1 for a in accounts if a.credit is None),
            "hold_cn_remaining": int(self.realm_hold_remaining("cn", now)),
            "hold_global_remaining": int(self.realm_hold_remaining("global", now)),
            "strategy": {r: self.strategy_for(r) for r in ("cn", "global")},
        }
