"""故障转移：谁该换号、谁绝对不能换，以及换号前的等待与留证。

从 ``service.py`` 按职责切出来的；``GatewayService`` 多继承把这些方法接回去，
调用方无感。
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from . import identity, sse, util
from .adapters import chat as chat_adapter
from .errors import (
    CODE_EFFORT_UNSUPPORTED,
    CODE_FINGERPRINT,
    CODE_MODEL_FORBIDDEN,
    CODE_MODEL_UNAVAILABLE,
    CODE_PROMPT_TOO_LONG,
    CODE_QUOTA,
    CODE_RESTRICTED,
    CODE_SESSION_DEAD,
    CODE_TOOL_PAIRING,
    CODE_UA_REJECTED,
    ServiceError,
    client_error,
    parse_reset_clock,
)
from .log import get_logger
from .markup import MARKUP_HINT
from .upstream import UpstreamError, is_restricted

log = get_logger("failover")

# 退避参数留在 service 侧属于历史原因（那里也用到），这里复制一份默认值，
# 免得两个模块互相 import。
BACKOFF_BASE = 0.5
BACKOFF_MAX = 8.0

class FailoverMixin:
    """错误处置。宿主类需提供 cfg / pool / auth / diag / egress / proxies。"""

    def _absorb(self, account: Any, err: UpstreamError, model: str, realm: str) -> Tuple[bool, bool]:
        """把一次失败记到号上，并决定"换不换号 / 要不要先刷新令牌"。"""
        message = err.message or ""
        code = err.code
        if CODE_FINGERPRINT in message:
            # 指纹拦截：换号没用，换的是"客户端指纹"，不是账号。
            log.warning("指纹被拦（%s）：检查 UA/头族，不要换号", model)
            return False, False
        if err.kind == "restricted" or code == CODE_RESTRICTED or is_restricted(message):
            # 风控：实测一轮请求能把 7 个号一起封掉，所以绝不换号。
            self.pool.note_restricted(account, message)
            return False, False
        if err.kind == "waf":
            # 内容审核 / 边缘 WAF：跟账号健康无关，换号也一样被拦，
            # 而且"换号继续打"正是连坐的形态。给一分钟软冷却，然后把错交给客户端。
            self.pool.note_waf(account, message)
            self._note_waf_hit(realm, account)
            return False, False
        if code == CODE_PROMPT_TOO_LONG:
            return False, False
        if code == CODE_SESSION_DEAD or "12153" in message or "Offline user session" in message:
            self.pool.note_session_dead(account)
            return True, False
        if err.kind == "auth":
            return True, True
        if code == CODE_QUOTA or err.kind == "quota":
            self.pool.note_hard_credit(account, message or "余额不足")
            return True, False
        if err.kind == "rate_limit" or err.status == 429:
            if code == CODE_QUEUE_FULL:
                # queue.full 是排队打满，不是配额；短暂等待即可，不记到号上。
                return True, False
            reset_at = parse_reset_clock(message)
            if reset_at:
                self.pool.note_soft_rate(account, reset_at=reset_at, model=model)
            elif code == CODE_QUOTA:
                self.pool.note_hard_credit(account, message or "额度耗尽")
            elif _mentions_model(message) or code == 6004:
                self.pool.block_model(account, model, message or "模型限流")
            else:
                self.pool.note_unscoped_rate(account, message or "429")
            return True, False
        if code == CODE_MODEL_UNAVAILABLE:
            # "service info not found"：模型侧没接入，所有账号都一样，换号只会多烧三次退避。
            self.pool.block_model(account, model, message or "模型不可用")
            return False, False
        if code == CODE_MODEL_FORBIDDEN:
            # 11158 是"这个账号的用户类型/权限用不了这个模型"，换号可能就通了，所以换。
            self.pool.block_model(account, model, message or "模型不可用")
            return True, False
        if code in (CODE_TOOL_PAIRING, CODE_EFFORT_UNSUPPORTED, CODE_UA_REJECTED):
            return False, False
        if err.status == 404:
            return True, False
        if err.kind == "timeout":
            # 超时绝不换号：同一份请求换个号只会再撞一次慢上游，还可能被重复生成。
            # 客户端拿到 503 自己决定要不要重来，比网关偷偷重试安全。
            return False, False
        if err.kind == "transient" or err.status >= 500:
            # 链路问题不记在账号上（已记到出口）。换号可以，只是别再罚这个号。
            return True, False
        if err.kind == "bad_request":
            return False, False
        self.pool.note_unknown_failure(account, message or "未知失败")
        return True, False

    def _backoff(self, attempt: int) -> None:
        delay = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** (attempt - 1)))
        delay *= 1 + random.uniform(-0.25, 0.25)
        time.sleep(max(0.0, delay))

    def _client_error(self, err: Optional[UpstreamError], realm: str, model: str) -> ServiceError:
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

    def _dump_failure(
        self,
        err: UpstreamError,
        *,
        account: Any,
        realm: str,
        model: str,
        url: str,
        meta: Any = None,
        request: Any = None,
        payload: Any = b"",
    ) -> None:
        """把上游这次的拒绝原文留下来。

        只留"有 body 的拒绝"：超时的 body 是空的，留一条空记录没有意义。脱敏、限
        大小、按天封顶都在 :mod:`wb2api.diagnostics` 里做——这里只负责把上下文凑齐。
        """
        body = getattr(err, "body", "") or ""
        if not getattr(self.diag, "enabled", False) or not body:
            return
        request_id = ""
        client_ip = ""
        alias = ""
        if request is not None:
            state = getattr(request, "state", {}) or {}
            request_id = str(state.get("request_id") or "")
            alias = str(state.get("model_alias") or "")
            client_ip = str(getattr(request, "client_ip", "") or "")
        headers: Dict[str, Any] = {}
        try:
            headers = identity.chat_headers(account, self.cfg, meta=meta, client_ip=client_ip)
        except Exception:  # pragma: no cover - 算头出错也不该拖垮请求
            log.debug("dump: 计算请求头失败", exc_info=True)
            headers = {}
        try:
            self.diag.dump(
                request_id=request_id,
                realm=realm,
                model=model,
                alias=alias,
                account=str(getattr(account, "label", "") or getattr(account, "uid", "")),
                url=url,
                status=int(getattr(err, "status", 0) or 0),
                code=str(getattr(err, "code", "") or ""),
                message=str(getattr(err, "message", "") or ""),
                request_headers=headers,
                body=body,
                request_body=payload,
            )
        except Exception:
            log.debug("失败报文留存失败", exc_info=True)

    def _pace(self, account: Any) -> None:
        """同账号两次出站请求之间留最小间隔。

        真人不会 200ms 连发；被 agent 驱动的客户端会。对上游来说"同一机器 id
        高频调用"是最像脚本的特征，所以宁可让客户端多等一会儿。
        """
        delay = self.pool.pace_delay(account)
        if delay > 0:
            log.debug("账号 %s 节奏等待 %.0fms", account.label, delay * 1000)
            time.sleep(min(delay, 5.0))
        self.pool.note_chat_sent(account)

    def _ensure_ready(self, account: Any, realm: str) -> None:
        """出站前确保令牌可用；未过期就不碰上游（刷新令牌是一次性的，别乱用）。"""
        if not self.auth.ensure_token(account):
            self.pool.note_auth_dead(account, "令牌不可用")
            raise UpstreamError("账号令牌不可用", kind="auth", status=401)

    # -- WAF 波次 ------------------------------------------------------------
    WAF_WAVE_WINDOW = 60.0
    WAF_WAVE_UIDS = 2
    WAF_WAVE_HOLD = 60.0

    def _note_waf_hit(self, realm: str, account: Any) -> None:
        uid = str(getattr(account, "uid", "") or getattr(account, "id", ""))
        now = time.time()
        with self._key_lock:
            hits = [item for item in self._waf_hits.get(realm, []) if now - item[1] <= self.WAF_WAVE_WINDOW]
            hits.append((uid, now))
            self._waf_hits[realm] = hits

    def waf_wave(self, realm: str) -> Dict[str, Any]:
        """短时间内多个不同账号都踩到 WAF：这是出口/IP 级的问题。

        这时再换号只是把剩下的账号一个个送上去记一笔——而它们本来什么事都没有。
        停手一分钟，比"全池各被记一次"划算得多。
        """
        now = time.time()
        with self._key_lock:
            hits = [item for item in self._waf_hits.get(realm, []) if now - item[1] <= self.WAF_WAVE_WINDOW]
            self._waf_hits[realm] = hits
        uids = {item[0] for item in hits}
        active = len(uids) >= self.WAF_WAVE_UIDS
        until = (max(item[1] for item in hits) + self.WAF_WAVE_HOLD) if hits else 0.0
        return {
            "active": active,
            "uids": len(uids),
            "realm": realm,
            "until": until if active else 0.0,
            "for_seconds": int(max(0.0, until - now)) if active else 0,
        }

    def _client_error(self, err: Optional[UpstreamError], realm: str, model: str) -> ServiceError:
        """兼容入口：翻译逻辑在 ``errors.client_error``（纯函数，好测）。"""
        return client_error(err, realm, model)
