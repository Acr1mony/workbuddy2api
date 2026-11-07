"""会话粘滞：同一个对话尽量落在同一个账号上。

为什么值得单独一个模块：上游的 prompt cache 是**按账号**隔离的。一旦同一个对话
在账号之间跳，前面的 prompt 全部作废，首字延迟从 ~1.5s 掉到 5-6s，缓存命中率
直接归零。所以选号的第一优先级不是"均衡"，而是"这个对话上次用的谁"。

键的取法（依次尝试）：

1. 请求头 ``X-Conversation-Id`` / ``Conversation-Id`` / ``X-Session-Id`` / ``Session-Id``
2. 请求体 ``conversation_id`` / ``session_id`` / ``metadata.conversation_id``
3. 前缀哈希：``sha256(json(messages[:2]))`` —— 客户端什么都不给时的兜底

第 3 条只在"开头两三条消息完全相同"时才会撞键，而真正不同的对话开头几乎不可能
完全一致；反过来，如果不做这一步，逐轮重发的客户端每轮都算新会话，粘滞等于没开。

每个会话记住最近 N 个账号（默认 3），失败时只把坏的那个降级，其余保留——它们上游
可能还留着这个对话的 prefix。
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Dict, List, Optional, Tuple

from . import util
from .log import get_logger

log = get_logger("sticky")

HEADER_KEYS = ("x-conversation-id", "conversation-id", "x-session-id", "session-id")
BODY_KEYS = ("conversation_id", "conversationId", "session_id", "sessionId")


def session_key(headers: Dict[str, str], body: Optional[Dict[str, Any]], prefix_messages: int = 2) -> str:
    """从请求里提取会话键；提取不到就返回空串。"""
    for name in HEADER_KEYS:
        value = (headers or {}).get(name) or (headers or {}).get(name.title())
        if value:
            return str(value)[:200]
    if isinstance(body, dict):
        meta = body.get("metadata")
        if isinstance(meta, dict):
            for name in BODY_KEYS:
                if meta.get(name):
                    return str(meta[name])[:200]
        for name in BODY_KEYS:
            if body.get(name):
                return str(body[name])[:200]
        messages = body.get("messages")
        if isinstance(messages, list) and messages:
            head = messages[: max(1, prefix_messages)]
            try:
                digest = hashlib.sha256(
                    json.dumps(head, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()
            except (TypeError, ValueError):
                return ""
            return "pfx-" + digest[:32]
    return ""


class SessionAffinity:
    """会话 -> 账号 MRU。纯内存，重启即失效（重启后上游缓存本来就凉了）。"""

    def __init__(
        self,
        ttl: float = 1800.0,
        max_entries: int = 5000,
        history: int = 3,
        enabled: bool = True,
    ) -> None:
        self.ttl = float(ttl)
        self.max_entries = int(max_entries)
        self.history = max(1, int(history))
        self.enabled = bool(enabled)
        self._map: Dict[str, Tuple[List[str], float]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    # -- 查询 ---------------------------------------------------------------
    def candidates(self, key: str) -> List[str]:
        """返回该会话偏好的账号 uid 列表（最近的在前）。"""
        if not key or not self.enabled:
            return []
        with self._lock:
            entry = self._map.get(key)
            if entry is None:
                self.misses += 1
                return []
            uids, expires = entry
            if expires <= util.now():
                self._map.pop(key, None)
                self.misses += 1
                return []
            self.hits += 1
            return list(uids)

    def bind(self, key: str, uid: str) -> None:
        if not key or not uid:
            return
        now = util.now()
        with self._lock:
            uids, _ = self._map.get(key, ([], 0.0))
            uids = [u for u in uids if u != uid]
            uids.insert(0, uid)
            self._map[key] = (uids[: self.history], now + self.ttl)
            if len(self._map) > self.max_entries:
                self._gc_locked(now)
                if len(self._map) > self.max_entries:
                    # 还是超：按过期时间清掉最老的一批，别让字典无限长。
                    for stale in sorted(self._map, key=lambda k: self._map[k][1])[: len(self._map) - self.max_entries]:
                        self._map.pop(stale, None)

    def demote(self, key: str, uid: str) -> None:
        """只摘掉失败的账号，其余保留（它们上游可能还有这个对话的 prefix）。"""
        if not key or not uid:
            return
        with self._lock:
            entry = self._map.get(key)
            if entry is None:
                return
            uids, expires = entry
            remaining = [u for u in uids if u != uid]
            if remaining:
                self._map[key] = (remaining, expires)
            else:
                self._map.pop(key, None)

    def drop(self, key: str) -> None:
        with self._lock:
            self._map.pop(key, None)

    # -- 维护 ---------------------------------------------------------------
    def gc(self) -> int:
        with self._lock:
            return self._gc_locked(util.now())

    def _gc_locked(self, now: float) -> int:
        dead = [key for key, (_uids, expires) in self._map.items() if expires <= now]
        for key in dead:
            self._map.pop(key, None)
        return len(dead)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "sessions": len(self._map),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits * 100.0 / total, 1) if total else 0.0,
                "ttl_seconds": self.ttl,
            }

    def reset_stats(self) -> None:
        with self._lock:
            self.hits = 0
            self.misses = 0
