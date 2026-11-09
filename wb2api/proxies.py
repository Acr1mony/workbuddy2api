"""出口代理池：让"同一台机器、同一个 IP 打同一家上游"这件事不再成立。

为什么需要它
------------
上游按"机器 id + 出口 IP"看请求形状。一个号每天几千次请求、永远从家里那条宽带
出去，光这一点就够可疑了；而一旦某个出口被 WAF 盯上，连换号都没用——因为出口没换。

有了池子之后：

* 每个请求从池里挑一个出口（按域、按权重、按健康度）；
* 某个出口被拦/超时就进冷却，别让它继续把号送上去记一笔；
* 冷却期过了自动回到池里，不需要人手动放回来；
* 账号自己配了 ``proxy`` 的，永远用自己的（那通常是给某个号单配的家宽）。

配置形状（``config.json`` 的 ``pool.proxies``）::

    "proxies": [
        "socks5h://user:pass@1.2.3.4:1080",
        {"url": "http://10.0.0.9:3128", "realm": "global", "weight": 2, "note": "美西"}
    ]

字符串是简写，等价于 ``{"url": ...}``。``realm`` 为空表示两个域都能用。
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional

from . import util

#: 出口失败后第一档冷却。5 分钟足够让一次 WAF 误判过去，又不会把一个好出口
#: 关太久（池子小的时候，关太久的代价是"没出口可用"反而更危险）。
PROXY_COOLDOWN_BASE = 300.0
#: 冷却上限：一小时。再久就等于把这个出口永久删掉了，那该人工决定。
PROXY_COOLDOWN_MAX = 3600.0


def _realm_of(value: str) -> str:
    """把域写法归一。``""`` 表示两个域都能用，所以不能粗暴地 default 成 cn。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return "global" if text in ("global", "international", "intl", "海外", "国外") else "cn"


class ProxyEntry:
    """池里的一个出口。``url`` 是唯一键（同一个出口重复添加就是同一行）。"""

    __slots__ = ("url", "realm", "weight", "note", "enabled", "failures", "cooldown_until", "used")

    def __init__(
        self,
        url: str,
        realm: str = "",
        weight: float = 1.0,
        note: str = "",
        enabled: bool = True,
    ) -> None:
        self.url = str(url or "").strip()
        self.realm = _realm_of(realm)
        raw = 1.0 if weight is None else float(weight)
        self.weight = max(0.1, raw)
        self.note = str(note or "")
        self.enabled = bool(enabled)
        self.failures = 0
        self.cooldown_until = 0.0
        self.used = 0

    def cooling(self, now: float) -> float:
        return max(0.0, self.cooldown_until - now)

    def masked(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = util.now() if now is None else now
        return {
            "url": util.mask_secret(self.url, 10) if self.url else "",
            "realm": self.realm or "both",
            "weight": round(self.weight, 2),
            "note": self.note,
            "enabled": self.enabled,
            "failures": self.failures,
            "cooling_for": round(self.cooling(now), 1),
            "used": self.used,
        }

    def to_config(self) -> Dict[str, Any]:
        item: Dict[str, Any] = {"url": self.url}
        if self.realm:
            item["realm"] = self.realm
        if abs(self.weight - 1.0) > 1e-9:
            item["weight"] = round(self.weight, 2)
        if self.note:
            item["note"] = self.note
        if not self.enabled:
            item["enabled"] = False
        return item


class ProxyPool:
    """出口选择 + 健康度。线程安全性靠的是"只在持锁的选取里改动计数器"这个约定，
    而真正的并发保护由 :class:`wb2api.pool.Pool` 的锁负责（同一个进程里只有它调用）。
    """

    def __init__(self, cfg: Any = None) -> None:
        self.cfg = cfg
        self._entries: List[ProxyEntry] = []
        self._lock_guard = __import__("threading").RLock()
        self.reload()

    # -- 配置 ---------------------------------------------------------------
    def _raw(self) -> List[Any]:
        if not self.cfg:
            return []
        value = self.cfg.get("pool.proxies") if hasattr(self.cfg, "get") else None
        return list(value or []) if isinstance(value, (list, tuple)) else []

    def reload(self) -> None:
        entries: List[ProxyEntry] = []
        seen = set()
        for item in self._raw():
            entry = self._parse(item)
            if entry is None or entry.url in seen:
                continue
            seen.add(entry.url)
            # 重新加载配置时保留运行期的健康度，别因为"点了一下保存"就把
            # 正在冷却的坏出口又放回池子。
            old = self._find(entry.url)
            if old is not None:
                entry.failures = old.failures
                entry.cooldown_until = old.cooldown_until
                entry.used = old.used
            entries.append(entry)
        with self._lock_guard:
            self._entries = entries

    @staticmethod
    def _parse(item: Any) -> Optional[ProxyEntry]:
        if isinstance(item, str):
            url = item.strip()
            if not url:
                return None
            return ProxyEntry(url)
        if isinstance(item, dict):
            url = str(item.get("url") or item.get("proxy") or "").strip()
            if not url:
                return None
            return ProxyEntry(
                url,
                realm=_realm_of(item.get("realm")),
                weight=item.get("weight", 1.0),
                note=item.get("note", ""),
                enabled=item.get("enabled", True),
            )
        return None

    def _find(self, url: str) -> Optional[ProxyEntry]:
        for entry in self._entries:
            if entry.url == url:
                return entry
        return None

    def _cooldown_base(self) -> float:
        if not self.cfg:
            return PROXY_COOLDOWN_BASE
        return max(1.0, util.parse_duration(self.cfg.get("pool.proxy_cooldown", "5m"), PROXY_COOLDOWN_BASE))

    def _cooldown_cap(self) -> float:
        if not self.cfg:
            return PROXY_COOLDOWN_MAX
        return max(self._cooldown_base(), util.parse_duration(self.cfg.get("pool.proxy_cooldown_max", "1h"), PROXY_COOLDOWN_MAX))

    # -- 选取 ---------------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self._entries)

    def size(self, realm: str = "") -> int:
        return len(self._candidates(realm, util.now(), ignore_cooldown=True))

    def _candidates(self, realm: str, now: float, *, ignore_cooldown: bool = False) -> List[ProxyEntry]:
        want = _realm_of(realm)
        out = []
        for entry in self._entries:
            if not entry.enabled:
                continue
            if want and entry.realm and entry.realm != want:
                continue
            if not ignore_cooldown and entry.cooling(now) > 0:
                continue
            out.append(entry)
        return out

    def pick(self, realm: str = "", now: Optional[float] = None) -> str:
        """挑一个出口；池子为空返回 ``""``（直连）。

        全部都在冷却时**不退回直连**：直连等于把真实 IP 暴露给上游，而那正是我们
        想避免的事。这里改成挑"最早解禁"的那个继续用——它至少还是个代理。
        """
        now = util.now() if now is None else now
        with self._lock_guard:
            if not self._entries:
                return ""
            candidates = self._candidates(realm, now)
            if not candidates:
                fallback = self._candidates(realm, now, ignore_cooldown=True)
                if not fallback:
                    return ""
                candidates = [min(fallback, key=lambda item: item.cooling(now) or 0.0)]
            total = sum(entry.weight for entry in candidates)
            picker = random.random() * total
            chosen = candidates[-1]
            for entry in candidates:
                picker -= entry.weight
                if picker <= 0:
                    chosen = entry
                    break
            chosen.used += 1
            return chosen.url

    # -- 健康度 -------------------------------------------------------------
    def note_success(self, url: str) -> None:
        if not url:
            return
        with self._lock_guard:
            entry = self._find(url)
            if entry is not None:
                entry.failures = 0
                entry.cooldown_until = 0.0

    def note_failure(self, url: str, reason: str = "") -> float:
        """记一次失败并返回冷却秒数；冷却按失败次数翻倍，封顶 ``proxy_cooldown_max``。"""
        if not url:
            return 0.0
        with self._lock_guard:
            entry = self._find(url)
            if entry is None:
                return 0.0
            entry.failures += 1
            shift = min(entry.failures - 1, 8)
            wait = min(self._cooldown_cap(), self._cooldown_base() * (2 ** shift))
            entry.cooldown_until = util.now() + wait
            return wait

    def clamp(self) -> None:
        """把超过上限的冷却压回上限（改了配置以后立刻生效）。"""
        cap = self._cooldown_cap()
        now = util.now()
        with self._lock_guard:
            for entry in self._entries:
                if entry.cooling(now) > cap:
                    entry.cooldown_until = now + cap

    def stats(self, realm: str = "") -> Dict[str, Any]:
        now = util.now()
        with self._lock_guard:
            want = _realm_of(realm)
            rows = [
                entry.masked(now)
                for entry in self._entries
                if not want or not entry.realm or entry.realm == want
            ]
            ready = sum(1 for entry in self._entries
                        if entry.enabled and entry.cooling(now) <= 0
                        and (not want or not entry.realm or entry.realm == want))
            cooling = sum(1 for entry in self._entries if entry.enabled and entry.cooling(now) > 0)
        return {"total": len(rows), "ready": ready, "cooling": cooling, "entries": rows}

    # -- 增删改（面板） -----------------------------------------------------
    def add(self, url: str, realm: str = "", weight: float = 1.0, note: str = "") -> bool:
        entry = self._parse({"url": url, "realm": realm, "weight": weight, "note": note})
        if entry is None:
            return False
        with self._lock_guard:
            if self._find(entry.url) is not None:
                return False
            self._entries.append(entry)
        return True

    def remove(self, url: str) -> bool:
        with self._lock_guard:
            entry = self._find(str(url or "").strip())
            if entry is None:
                return False
            self._entries.remove(entry)
        return True

    def update(self, url: str, **fields: Any) -> bool:
        with self._lock_guard:
            entry = self._find(str(url or "").strip())
            if entry is None:
                return False
            if "realm" in fields:
                entry.realm = _realm_of(fields["realm"])
            if "weight" in fields:
                try:
                    entry.weight = max(0.1, float(fields["weight"]))
                except (TypeError, ValueError):
                    pass
            if "note" in fields:
                entry.note = str(fields["note"] or "")
            if "enabled" in fields:
                entry.enabled = bool(fields["enabled"])
                if entry.enabled:
                    entry.cooldown_until = 0.0
                    entry.failures = 0
        return True

    def reset_health(self) -> None:
        with self._lock_guard:
            for entry in self._entries:
                entry.failures = 0
                entry.cooldown_until = 0.0

    def to_config(self) -> List[Dict[str, Any]]:
        with self._lock_guard:
            return [entry.to_config() for entry in self._entries]
