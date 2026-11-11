"""出口代理池：一个出口只服务一个账号，而不是"一个域一个出口"。

为什么按账号分，而不是按域分
---------------------------
上游看的是"机器 id + 出口 IP"的组合形状。两个号从同一个出口出去，等于自己把
两个号拴在一起：一个号被风控，另一个号跟着倒霉。所以这里不做"请求来了随便挑
一个出口"，而是**给每个账号钉死一个出口**：

* 账号第一次发请求时分配到一个出口，之后一直用它；绑定写回账号（``extra.egress``），
  重启后照旧——"换出口"本身就是一次画像变化，不该随便发生；
* 分配时优先挑**还没被别的账号占用**的出口；只有出口比账号少时才允许共用，
  共用时挑占用最少的那个，面板会把"这个出口上有几个号"直接列出来；
* 出口被删/停用、或者连续失败到 ``STICKY_BREAK_FAILURES`` 次（说明它真坏了，
  不是偶尔抖一下），下次请求才重新分配；
* 出口进冷却只是**暂时**不用它，绑定不动——等冷却过去继续走同一个出口。
* 账号自己配了 ``proxy`` 的永远最高优先（那通常是给某个号单配的家宽专线）。

配置形状（``config.json`` 的 ``pool.proxies``）::

    "proxies": [
        "socks5h://user:pass@1.2.3.4:1080",
        {"url": "http://10.0.0.9:3128", "weight": 2, "note": "美西"}
    ]

字符串是简写，等价于 ``{"url": ...}``。旧配置里的 ``realm`` 字段仍会被忽略地接受，
但已经没有意义了——出口不再按域分组。
"""

from __future__ import annotations

import random
import threading
from typing import Any, Dict, List, Optional, Tuple

from . import util

#: 出口失败后第一档冷却。5 分钟足够让一次 WAF 误判过去，又不会把一个好出口
#: 关太久（池子小的时候，关太久的代价是"没出口可用"反而更危险）。
PROXY_COOLDOWN_BASE = 300.0
#: 冷却上限：一小时。再久就等于把这个出口永久删掉了，那该人工决定。
PROXY_COOLDOWN_MAX = 3600.0
#: 连续失败到这个次数就认为出口真坏了，允许把这批账号换到别的出口。
STICKY_BREAK_FAILURES = 3


class ProxyEntry:
    """池里的一个出口。``url`` 是唯一键（同一个出口重复添加就是同一行）。"""

    __slots__ = ("url", "weight", "note", "enabled", "failures", "cooldown_until", "used")

    def __init__(self, url: str, weight: float = 1.0, note: str = "", enabled: bool = True) -> None:
        self.url = str(url or "").strip()
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
            "weight": round(self.weight, 2),
            "note": self.note,
            "enabled": self.enabled,
            "failures": self.failures,
            "cooling_for": round(self.cooling(now), 1),
            "used": self.used,
        }

    def to_config(self) -> Dict[str, Any]:
        item: Dict[str, Any] = {"url": self.url}
        if abs(self.weight - 1.0) > 1e-9:
            item["weight"] = round(self.weight, 2)
        if self.note:
            item["note"] = self.note
        if not self.enabled:
            item["enabled"] = False
        return item


class ProxyPool:
    """出口分配 + 健康度。

    绑定表（``uid -> url``）由 :meth:`observe` 从账号上灌进来、由 :meth:`assign`
    写回账号，所以它是"账号的镜像"而不是独立的真相来源：账号删了绑定就没了。
    线程安全性靠的是"只在持锁的选取里改动计数器"这个约定，真正的并发保护来自
    :class:`wb2api.pool.Pool`（同一个进程里只有它在请求路径上调这里）。
    """

    def __init__(self, cfg: Any = None) -> None:
        self.cfg = cfg
        self._entries: List[ProxyEntry] = []
        self._bindings: Dict[str, str] = {}
        self._lock_guard = threading.RLock()
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
            # 出口被删掉后，指向它的绑定要一起清掉，否则那个号会永远拿到一个
            # 已经不存在的出口。
            known = {entry.url for entry in entries}
            self._bindings = {uid: url for uid, url in self._bindings.items() if url in known}

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

    # -- 绑定 ---------------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self._entries)

    def size(self) -> int:
        return sum(1 for entry in self._entries if entry.enabled)

    def bindings(self) -> Dict[str, str]:
        with self._lock_guard:
            return dict(self._bindings)

    def binding_of(self, uid: str) -> str:
        with self._lock_guard:
            return self._bindings.get(str(uid or ""), "")

    def observe(self, account: Any) -> str:
        """启动时把账号上已有的绑定灌回内存（不写任何东西）。"""
        uid = str(getattr(account, "uid", "") or "")
        url = str((getattr(account, "extra", None) or {}).get("egress") or "")
        if not uid or not url:
            return ""
        with self._lock_guard:
            if self._find(url) is not None:
                self._bindings[uid] = url
                return url
        return ""

    def release(self, uid: str) -> bool:
        with self._lock_guard:
            return self._bindings.pop(str(uid or ""), None) is not None

    def rebind_all(self) -> None:
        with self._lock_guard:
            self._bindings.clear()
            for entry in self._entries:
                entry.failures = 0
                entry.cooldown_until = 0.0

    def _holders(self, url: str) -> List[str]:
        return [uid for uid, bound in self._bindings.items() if bound == url]

    def assign(self, account: Any, now: Optional[float] = None) -> str:
        """给这个账号定一个出口并返回。

        顺序：账号自己的 ``proxy`` → 已绑定且还能用 → 重新分配。
        返回 ``""`` 表示直连（池子空，或者所有出口都停用了）。
        """
        now = util.now() if now is None else now
        manual = str(getattr(account, "proxy", "") or "")
        if manual:
            return manual
        if not self._entries:
            return ""
        uid = str(getattr(account, "uid", "") or getattr(account, "id", "") or "")
        if not uid:
            return self._pick(now)

        with self._lock_guard:
            bound = self._bindings.get(uid, "")
            if bound:
                entry = self._find(bound)
                # 出口还在、还启用、且没有坏到该放弃的程度 → 继续用它。
                if entry is not None and entry.enabled and entry.failures < STICKY_BREAK_FAILURES:
                    entry.used += 1
                    return entry.url
                self._bindings.pop(uid, None)

            url = self._pick(now, exclude_uid=uid)
            if url:
                self._bindings[uid] = url
                entry = self._find(url)
                if entry is not None:
                    entry.used += 1
            return url

    def _pick(self, now: float, exclude_uid: str = "") -> str:
        """挑一个"占用最少"的出口；数量够时保证不共用。

        冷却中的出口不参与挑新账号（新号没必要接到一个正在风头上的 IP 上），
        但如果全都在冷却，就挑最早解禁的那个——**绝不退回直连**，直连等于把
        真实 IP 交出去。
        """
        pool = [entry for entry in self._entries if entry.enabled]
        if not pool:
            return ""
        ready = [entry for entry in pool if entry.cooling(now) <= 0] or pool
        load = {entry.url: len(self._holders(entry.url)) for entry in ready}
        if exclude_uid:
            for entry in ready:
                held = self._holders(entry.url)
                load[entry.url] = sum(1 for uid in held if uid != exclude_uid)
        fewest = min(load.values()) if load else 0
        candidates = [entry for entry in ready if load.get(entry.url, 0) == fewest]
        total = sum(entry.weight for entry in candidates) or 1.0
        picker = random.random() * total
        chosen = candidates[-1]
        for entry in candidates:
            picker -= entry.weight
            if picker <= 0:
                chosen = entry
                break
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

    def stats(self) -> Dict[str, Any]:
        now = util.now()
        with self._lock_guard:
            rows = []
            for entry in self._entries:
                row = entry.masked(now)
                holders = self._holders(entry.url)
                row["accounts"] = holders
                row["account_count"] = len(holders)
                rows.append(row)
            bound = len(self._bindings)
            shared = sum(1 for entry in self._entries if len(self._holders(entry.url)) > 1)
            ready = sum(1 for entry in self._entries if entry.enabled and entry.cooling(now) <= 0)
            cooling = sum(1 for entry in self._entries if entry.enabled and entry.cooling(now) > 0)
            free = sum(1 for entry in self._entries if entry.enabled and not self._holders(entry.url))
        return {
            "total": len(self._entries),
            "ready": ready,
            "cooling": cooling,
            "bound": bound,
            "free": free,
            "shared": shared,
            "entries": rows,
        }

    # -- 增删改（面板） -----------------------------------------------------
    def add(self, url: str, weight: float = 1.0, note: str = "") -> bool:
        entry = self._parse({"url": url, "weight": weight, "note": note})
        if entry is None:
            return False
        with self._lock_guard:
            if self._find(entry.url) is not None:
                return False
            self._entries.append(entry)
        return True

    def remove(self, url: str) -> bool:
        text = str(url or "").strip()
        with self._lock_guard:
            entry = self._find(text)
            if entry is None:
                return False
            self._entries.remove(entry)
            self._bindings = {uid: bound for uid, bound in self._bindings.items() if bound != text}
        return True

    def update(self, url: str, **fields: Any) -> bool:
        with self._lock_guard:
            entry = self._find(str(url or "").strip())
            if entry is None:
                return False
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


def describe_binding(pool: ProxyPool, uid: str) -> Tuple[str, int]:
    """面板/详情页用：这个账号用的出口（掩码）和它上面挂了几个号。"""
    url = pool.binding_of(uid)
    if not url:
        return "", 0
    return util.mask_secret(url, 10), len(pool._holders(url))
