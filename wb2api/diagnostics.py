"""失败报文留存：把"上游为什么拒了"这句话本身留下来。

为什么需要它
------------
上游拒请求时回的是 JSON 信封加一句人话：``{"code":11101,"message":"tool_choice
invalid"}``。我们把它翻成 ``400 bad_request`` 之后，信封原文就丢了——于是"为什么
这条请求被拒"只能靠复现去猜，而这类问题（tool 配对断了、schema 不合法、机器 id
被判成脚本）往往十条里错一条，靠猜根本猜不出来。

三件事必须做对，否则留存本身会变成新的风险：

1. **脱敏**：``Authorization``、``X-Refresh-Token``、``Cookie`` 这些要么遮掉、要么
   不存；body 里出现的 ``sk-``/``ck_``/``pt_`` 长串也一起遮。
2. **限大小**：一个 8MB 的 400 报文存下来，磁盘很快就被"错误"占满。
3. **限数量**：按天封顶。被错误循环刷的时候，日志留存不能跟着雪崩。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional

from . import util

#: 这些头的值一律不落盘（键名小写比较）。
SENSITIVE_HEADERS = (
    "authorization",
    "cookie",
    "set-cookie",
    "x-refresh-token",
    "x-api-key",
    "x-device-token",
    "x-enterprise-id",
    "x-user-id",
    "proxy-authorization",
)

#: body 里出现的密钥形状。只遮"看起来像凭据"的长串，不碰正常正文。
_TOKEN_RE = re.compile(r"\b(sk|ck|pt)[-_][A-Za-z0-9_-]{12,}", re.IGNORECASE)


def redact_headers(headers: Optional[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for name, value in (headers or {}).items():
        key = str(name).lower()
        if key in SENSITIVE_HEADERS:
            out[key] = "**redacted**"
        else:
            out[key] = str(value)[:300]
    return out


def redact_text(text: str) -> str:
    return _TOKEN_RE.sub(lambda m: util.mask_secret(m.group(0), 6), text or "")


class Diagnostics:
    """按天一个 JSONL 文件；一条记录 = 一次被拒的请求（含脱敏后的报文）。"""

    DIRNAME = "dumps"

    def __init__(
        self,
        data_dir: str,
        *,
        enabled: bool = True,
        max_body_bytes: int = 8192,
        retention_days: int = 3,
        daily_max: int = 200,
    ) -> None:
        self.data_dir = str(data_dir or "")
        self.enabled = bool(enabled) and bool(self.data_dir)
        self.max_body_bytes = max(256, int(max_body_bytes or 8192))
        self.retention_days = max(1, int(retention_days or 3))
        self.daily_max = max(0, int(daily_max or 200))
        self._counts: Dict[str, int] = {}

    # -- 路径 ---------------------------------------------------------------
    @property
    def root(self) -> str:
        return os.path.join(self.data_dir, self.DIRNAME)

    def _path(self, day: str = "") -> str:
        day = day or self._today()
        return os.path.join(self.root, "dump-%s.jsonl" % day)

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    # -- 写入 ---------------------------------------------------------------
    def dump(
        self,
        *,
        request_id: str,
        kind: str = "chat.completions",
        realm: str = "",
        model: str = "",
        alias: str = "",
        account: str = "",
        url: str = "",
        status: int = 0,
        code: str = "",
        message: str = "",
        response_headers: Optional[Dict[str, Any]] = None,
        request_headers: Optional[Dict[str, Any]] = None,
        body: Any = "",
    ) -> str:
        """留一次被拒请求；返回落盘路径（未启用时返回空串）。"""
        if not self.enabled:
            return ""
        day = self._today()
        used = self._counts.get(day, 0)
        if self.daily_max and used >= self.daily_max:
            return ""  # 今天留满了：宁可漏，也不能让留存把盘吃干净
        text = body if isinstance(body, str) else (body or "")
        if isinstance(text, (bytes, bytearray)):
            text = bytes(text[: self.max_body_bytes]).decode("utf-8", "replace")
        truncated = len(text) > self.max_body_bytes
        record = {
            "at": time.time(),
            "iso": util.iso(),
            "request_id": str(request_id or ""),
            "kind": kind,
            "realm": realm,
            "model": model,
            "alias": alias,
            "account": account,
            "url": url,
            "status": int(status or 0),
            "code": str(code or ""),
            "message": redact_text(str(message or ""))[:600],
            "truncated": truncated,
            "body": redact_text(text[: self.max_body_bytes]),
            "response_headers": redact_headers(response_headers),
            "request_headers": redact_headers(request_headers),
        }
        path = self._path(day)
        try:
            util.append_jsonl(path, record)
        except OSError:
            return ""
        self._counts[day] = used + 1
        return path

    # -- 读取 ---------------------------------------------------------------
    def _files(self) -> List[str]:
        if not os.path.isdir(self.root):
            return []
        names = [name for name in os.listdir(self.root) if name.startswith("dump-") and name.endswith(".jsonl")]
        return [os.path.join(self.root, name) for name in sorted(names)]

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        """最近的被拒请求（摘要，不含完整 body）。"""
        limit = max(1, min(int(limit or 50), 500))
        files = self._files()[-3:]
        rows: List[Dict[str, Any]] = []
        for path in files:
            for item in util.tail_jsonl(path, limit=limit * 2):
                rows.append(
                    {
                        "request_id": item.get("request_id", ""),
                        "at": item.get("at", 0),
                        "iso": item.get("iso", ""),
                        "model": item.get("model", ""),
                        "account": item.get("account", ""),
                        "status": item.get("status", 0),
                        "code": item.get("code", ""),
                        "message": item.get("message", ""),
                        "size": len(item.get("body", "") or ""),
                    }
                )
        rows.sort(key=lambda row: row.get("at") or 0, reverse=True)
        return rows[:limit]

    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        target = str(request_id or "").strip()
        if not target:
            return None
        for path in reversed(self._files()[-3:]):
            for item in util.tail_jsonl(path, limit=2000):
                if str(item.get("request_id") or "") == target:
                    return item
        return None

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
                stamp = time.mktime(time.strptime(name[len("dump-"):-len(".jsonl")], "%Y-%m-%d"))
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
            "files": len(self._files()),
            "today": self._counts.get(self._today(), 0),
            "daily_max": self.daily_max,
            "retention_days": self.retention_days,
            "max_body_bytes": self.max_body_bytes,
        }


def dump_json(record: Dict[str, Any]) -> str:
    """给面板"复制整条"用。"""
    return json.dumps(record, ensure_ascii=False, indent=2)
