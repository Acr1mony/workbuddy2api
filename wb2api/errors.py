"""错误模型：把上游的各种失败翻译成客户端看得懂的信封。

刻意做成纯函数：不碰账号、不碰池、不碰磁盘，排障时可以直接拿一段上游原文
在 REPL 里跑一遍，看客户端会收到什么。
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Optional

from .upstream import UpstreamError, is_restricted


CODE_PROMPT_TOO_LONG = 11115
CODE_MODEL_UNAVAILABLE = 11102
CODE_MODEL_FORBIDDEN = 11158
CODE_TOOL_PAIRING = 11148
CODE_EFFORT_UNSUPPORTED = 11150
CODE_UA_REJECTED = 12403
CODE_FINGERPRINT = "11-128"  # 上游把指纹错误写成带连字符的字符串，不是数字
CODE_RESTRICTED = 11140
CODE_SESSION_DEAD = 12153
CODE_QUOTA = 6004
CODE_QUEUE_FULL = 6021


class ServiceError(Exception):
    """可以直接变成 OpenAI 错误信封的失败。"""

    def __init__(
        self,
        message: str,
        status: int = 400,
        code: str = "bad_request",
        kind: str = "invalid_request_error",
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.kind = kind
        # 有它就往响应里带 Retry-After：客户端自己会退避，比它盲目重试把墙撞穿好
        self.retry_after = retry_after



_RESET_CLOCK_RE = re.compile(r"(\d{4}-\d{1,2}-\d{1,2})[ T](\d{1,2}:\d{2}(?::\d{2})?)")


def parse_reset_clock(text: str, now: Optional[float] = None) -> float:
    """从 ``将在 YYYY-MM-DD HH:MM:SS 重置`` 解析出冷却到什么时候。

    上游用的是 UTC+8 的墙上时间且不带时区，按本地时间解析会被跨时区部署搞错
    （差 8 小时就是一个半天不能用的账号）。
    """
    if not text:
        return 0.0
    match = _RESET_CLOCK_RE.search(text)
    if match is None:
        return 0.0
    try:
        stamp = time.strptime(match.group(1) + " " + match.group(2), "%Y-%m-%d %H:%M:%S" if match.group(2).count(":") == 2 else "%Y-%m-%d %H:%M")
    except ValueError:
        try:
            stamp = time.strptime(match.group(1) + " " + match.group(2), "%Y-%m-%d %H:%M")
        except ValueError:
            return 0.0
    epoch = time.mktime(stamp)  # 本地时间 → 先把墙钟当本地
    offset = _local_offset() - 8 * 3600  # 再校正回 UTC+8
    return epoch - offset


def _local_offset() -> float:
    return -time.timezone if not time.daylight else -time.altzone



def client_error(err: Optional[UpstreamError], realm: str, model: str) -> ServiceError:
    """把上游错误翻译成客户端看得懂的信封。"""
    if err is None:
        return ServiceError("上游不可用", 503, "upstream_unavailable", "api_error")
    message = err.message or "上游失败"
    if CODE_FINGERPRINT in message:
        return ServiceError(
            "上游拒绝了本次请求（客户端指纹）。可尝试 prompt.mode=sanitize 或改用官方客户端头族。",
            400, "fingerprint_rejected", "invalid_request_error",
        )
    if err.kind == "restricted" or err.code == CODE_RESTRICTED or is_restricted(message):
        return ServiceError("账号被上游风控限制，已停止重试以免连坐", 403, "restricted", "api_error")
    if err.kind == "waf":
        return ServiceError(
            "上游内容审核拦截了本次请求（与账号无关，换号无效）。请改写触发内容或稍后重试。",
            403, "content_blocked", "api_error",
        )
    if err.code == CODE_PROMPT_TOO_LONG:
        return ServiceError("prompt 过长，上游拒绝（换号无效）", 400, "prompt_too_long", "invalid_request_error")
    if err.kind == "quota":
        return ServiceError("所有可用账号额度不足", 402, "insufficient_quota", "api_error")
    if err.kind == "rate_limit":
        return ServiceError(
            "上游限流，请稍后重试", 429, "rate_limited", "rate_limit_error",
            retry_after=float(err.retry_after or 0.0),
        )
    if err.kind == "timeout":
        return ServiceError(
            "上游响应超时（换号重试无效，请稍后再来）", 503, "upstream_timeout", "api_error", retry_after=0.0
        )
    if err.kind == "transient" or err.status >= 500:
        return ServiceError(
            "上游暂时不可用", 503, "upstream_unavailable", "api_error",
            retry_after=float(err.retry_after or 0.0),
        )
    if err.kind == "auth":
        return ServiceError("账号认证失败，需要重新登录", 503, "account_auth_failed", "api_error")
    # 以下这些是"请求本身有问题"，客户端改一下就好：一律 400，别用 502。
    # 502/503 会让客户端自动重试同一个坏请求，把配额和账号都白烧一遍。
    if err.code in (CODE_MODEL_UNAVAILABLE, CODE_MODEL_FORBIDDEN):
        return ServiceError(
            f"模型 {model} 在上游不可用（{message[:120]}），请换一个模型",
            400, "model_unavailable", "invalid_request_error",
        )
    if err.code == CODE_TOOL_PAIRING:
        return ServiceError(
            "工具调用配对不完整（assistant.tool_calls 与 tool 结果必须成对）", 400, "tool_call_mismatch", "invalid_request_error"
        )
    if err.code == CODE_EFFORT_UNSUPPORTED:
        return ServiceError(
            f"该模型不支持所请求的 reasoning_effort（{message[:120]}）", 400, "reasoning_effort_unsupported", "invalid_request_error"
        )
    if err.code == CODE_UA_REJECTED:
        return ServiceError("上游拒绝了客户端 UA（12403）", 400, "user_agent_rejected", "invalid_request_error")
    if err.kind == "bad_request":
        return ServiceError(message[:300], 400, "upstream_rejected", "invalid_request_error")
    return ServiceError(message[:300], 502, "upstream_error", "api_error")

