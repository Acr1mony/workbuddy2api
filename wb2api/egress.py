"""出口级退避：把"这条链路不行"和"这个账号不行"分开记账。

这是整套防封里最反直觉的一条：**链路抖动不该罚账号**。

如果每一次 502/超时都记到账号头上，一次 CDN 抖动就会让整池账号同时进熔断，
可用性直接塌方；而运维看到"账号全在冷却"，会去重新登录、换号、加号——全是白费。
参考实现里这条是血换来的：四小时内 196 次传输失败关掉了 13 个账号。

所以这里按 ``(host, proxy)`` 记退避：

* 失败一次，等待 ``base * 2^(n-1)``，指数增长、封顶 ``max``，带 ±25% 抖动；
* ``reset_after`` 秒内没有新的失败就清零（链路恢复了就别再躲）；
* 退避期间网关先睡一会儿（最多 ``max``），而不是把这个号踢下线。

抖动很重要：多个线程同时退避时，如果等待时间完全一样，它们会在同一毫秒一起重试，
对上游来说那就是一台机器在整点打卡。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Dict, Optional, Tuple

from . import util

DEFAULT_BASE = 1.0
DEFAULT_MAX = 15.0
DEFAULT_RESET_AFTER = 60.0
DEFAULT_JITTER = 0.25

# 这些 kind 说明"请求可能根本没被上游看到"，属于链路问题
EGRESS_KINDS = ("timeout", "transient")


class Egress:
    """按出口（host + proxy）记的指数退避。"""

    def __init__(
        self,
        base: float = DEFAULT_BASE,
        max_wait: float = DEFAULT_MAX,
        reset_after: float = DEFAULT_RESET_AFTER,
        jitter: float = DEFAULT_JITTER,
    ) -> None:
        self.base = max(0.0, float(base))
        self.max_wait = max(self.base, float(max_wait))
        self.reset_after = max(1.0, float(reset_after))
        self.jitter = min(1.0, max(0.0, float(jitter)))
        self._lock = threading.Lock()
        # key -> (失败次数, 最后一次失败时间, 解禁时间)
        self._state: Dict[str, Tuple[int, float, float]] = {}

    # -- 构造 ---------------------------------------------------------------
    @classmethod
    def from_config(cls, cfg: Any) -> "Egress":
        if cfg is None:
            return cls()

        def num(path: str, default: float) -> float:
            try:
                return float(cfg.get(path, default))
            except (TypeError, ValueError):
                return default

        return cls(
            base=num("upstream.egress_base_seconds", DEFAULT_BASE),
            max_wait=num("upstream.egress_max_seconds", DEFAULT_MAX),
            reset_after=num("upstream.egress_reset_seconds", DEFAULT_RESET_AFTER),
            jitter=num("upstream.egress_jitter", DEFAULT_JITTER),
        )

    # -- 键 -----------------------------------------------------------------
    @staticmethod
    def key_for(url: str, proxy: str = "") -> str:
        try:
            _scheme, host, port, _path = util.split_url(url)
        except Exception:
            host, port = url, 0
        return f"{host}:{port}|{proxy or '-'}"

    # -- 记账 ---------------------------------------------------------------
    def wait_for(self, key: str, now: Optional[float] = None) -> float:
        """这个出口还要等多久（秒，0 表示现在可以用）。"""
        now = util.now() if now is None else now
        with self._lock:
            entry = self._state.get(key)
            if entry is None:
                return 0.0
            _fails, last, until = entry
            if now - last > self.reset_after:
                self._state.pop(key, None)
                return 0.0
            return max(0.0, until - now)

    def note_failure(self, key: str, reason: str = "") -> float:
        """记一次出口失败，返回这次应该等多久。"""
        now = util.now()
        with self._lock:
            fails, last, _until = self._state.get(key, (0, 0.0, 0.0))
            if now - last > self.reset_after:
                fails = 0
            fails += 1
            wait = min(self.max_wait, self.base * (2.0 ** min(fails - 1, 10)))
            wait *= 1 + random.uniform(-self.jitter, self.jitter)
            wait = max(0.0, wait)
            self._state[key] = (fails, now, now + wait)
        return wait

    def note_success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)

    def snapshot(self) -> Dict[str, Any]:
        now = util.now()
        with self._lock:
            items = {
                key: {"fails": fails, "wait": max(0.0, until - now)}
                for key, (fails, last, until) in self._state.items()
                if now - last <= self.reset_after
            }
        return {"count": len(items), "egress": items, "max_wait": self.max_wait}

    def reset(self) -> None:
        with self._lock:
            self._state.clear()
