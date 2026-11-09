"""逐请求明细：一条请求一行，能按状态/模型/账号/关键字翻回去。

和用量台账的分工
----------------
``usage`` 回答"今天用了多少"（聚合、按天、按模型），``requests`` 回答"第 137 次
请求到底怎么了"（单条、带 request id、带上游状态码、带客户端）。

为什么单独写、而不是塞进 usage
------------------------------
* 检索形状不同：明细要按 ``request_id``、状态码、关键字过滤，聚合表用不上；
* 保留期不同：明细 3 天就够（排障是这几天的事），聚合要留 7 天以上（看趋势）；
* 体积不同：明细一行约 500 字节，聚合一行才几十字节。

写盘失败只记 warning：日志留不下来是可惜，但不值得让请求失败。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from . import util


class RequestLog:
    DIRNAME = "requests"

    def __init__(
        self,
        data_dir: str,
        *,
        enabled: bool = True,
        retention_days: int = 3,
        capacity: int = 500,
    ) -> None:
        self.data_dir = str(data_dir or "")
        self.enabled = bool(enabled) and bool(self.data_dir)
        self.retention_days = max(1, int(retention_days or 3))
        self.capacity = max(50, int(capacity or 500))
        self._recent: List[Dict[str, Any]] = []
        self._loaded = False

    # -- 路径 ---------------------------------------------------------------
    @property
    def root(self) -> str:
        return os.path.join(self.data_dir, self.DIRNAME)

    def _path(self, day: str = "") -> str:
        day = day or time.strftime("%Y-%m-%d")
        return os.path.join(self.root, "req-%s.jsonl" % day)

    def _files(self) -> List[str]:
        if not os.path.isdir(self.root):
            return []
        names = [n for n in os.listdir(self.root) if n.startswith("req-") and n.endswith(".jsonl")]
        return [os.path.join(self.root, n) for n in sorted(names)]

    # -- 写入 ---------------------------------------------------------------
    def record(self, event: Dict[str, Any]) -> None:
        row = dict(event or {})
        row.setdefault("at", time.time())
        row.setdefault("iso", util.iso())
        self._recent.append(row)
        del self._recent[: -self.capacity]
        self._loaded = True
        if not self.enabled:
            return
        try:
            util.append_jsonl(self._path(), row)
        except OSError:
            return

    # -- 读取 ---------------------------------------------------------------
    def load(self) -> None:
        """启动时把最近几条读进内存，这样重启后第一屏不是空的。"""
        if self._loaded:
            return
        self._loaded = True
        rows: List[Dict[str, Any]] = []
        for path in self._files()[-2:]:
            rows.extend(util.tail_jsonl(path, limit=self.capacity))
        # 文件按日期排序、行按写入顺序，天然就是旧→新；不要按 at 排序（精度不够）。
        self._recent = rows[-self.capacity :]

    def recent(
        self,
        limit: int = 200,
        *,
        status: str = "",
        model: str = "",
        account: str = "",
        query: str = "",
    ) -> List[Dict[str, Any]]:
        self.load()
        limit = max(1, min(int(limit or 200), 1000))
        rows = list(self._recent)
        if status:
            wanted = status.strip()
            if wanted.endswith("xx"):
                prefix = wanted[0]
                rows = [r for r in rows if str(r.get("status") or "").startswith(prefix)]
            elif wanted == "error":
                rows = [r for r in rows if int(r.get("status") or 0) >= 400 or r.get("outcome") != "completed"]
            else:
                rows = [r for r in rows if str(r.get("status") or "") == wanted]
        if model:
            needle = model.strip().lower()
            rows = [r for r in rows if needle in str(r.get("model") or "").lower()]
        if account:
            needle = account.strip().lower()
            rows = [r for r in rows if needle in str(r.get("account") or "").lower()]
        if query:
            needle = query.strip().lower()
            rows = [
                r
                for r in rows
                if needle in json.dumps(r, ensure_ascii=False).lower()
            ]
        # ``_recent`` 本来就是按写入顺序（旧→新）的，倒过来就是"最新在前"。
        # 不用 sort(at)：Windows 上 time.time() 只有 ~15ms 精度，同一批请求的
        # at 会完全相同，排序变成不稳定，顺序看着像随机的。
        rows.reverse()
        return rows[:limit]

    # -- 维护 ---------------------------------------------------------------
    def prune(self, now: Optional[float] = None) -> int:
        if not self.enabled:
            return 0
        now = time.time() if now is None else now
        cutoff = now - self.retention_days * 86400
        removed = 0
        for path in self._files():
            name = os.path.basename(path)
            try:
                stamp = time.mktime(time.strptime(name[len("req-"):-len(".jsonl")], "%Y-%m-%d"))
            except ValueError:
                continue
            if stamp < cutoff:
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
        return removed

    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "buffered": len(self._recent),
            "files": len(self._files()),
            "retention_days": self.retention_days,
            "capacity": self.capacity,
        }
