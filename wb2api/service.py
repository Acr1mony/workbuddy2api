"""请求编排：鉴权 → 选号 → 出站 → 转发 → 记账。

这一层是唯一知道"一次客户端请求可能撞上好几个上游账号"的地方，所以三件事都在这：

* **故障转移**。哪些错误该换号、哪些绝对不换（换号只会把更多账号一起封掉），
  判断集中在这里；策略参数在 ``pool``。
* **流式边界**。上游只能流式，客户端也多半要流式；一旦响应头发给客户端就没法
  再换号了，所以"什么时候算开始发送"必须精确。
* **记账**。每个请求一行，成功失败都记——重试放大只能从台账里看出来。
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from . import identity, sse, util
from .adapters import chat as chat_adapter
from .log import get_logger
from .markup import MARKUP_HINT
from .models import ModelCatalog
from .pool import Pool
from .store import ApiKey
from .sticky import SessionAffinity, session_key as make_session_key
from .upstream import UpstreamClient, UpstreamError
from .usage import UsageLedger

log = get_logger("service")

MAX_ROTATE = 3  # 再换 3 个号；加上第一次共 4 次尝试
BACKOFF_BASE = 0.5
BACKOFF_MAX = 8.0
CHAT_PATH = "/v2/chat/completions"

# 上游业务码。判断"换不换号"全靠它，所以每个都单独命名，别写成裸数字。
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

    def __init__(self, message: str, status: int = 400, code: str = "bad_request", kind: str = "invalid_request_error") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.kind = kind


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


class GatewayService:
    """网关的全部业务逻辑。HTTP 层只负责把 Request 递进来、把 Response 发出去。"""

    def __init__(self, cfg: Any, store: Any, client: Optional[UpstreamClient] = None) -> None:
        from .authflow import AuthFlow

        self.cfg = cfg
        self.store = store
        self.client = client or UpstreamClient(cfg)
        self.auth = AuthFlow(self.client, store, cfg)
        self.catalog = ModelCatalog(self.client, cfg)
        sticky_on = bool(cfg.get("session_sticky.enabled", True)) if cfg else True
        self.affinity = SessionAffinity(
            ttl=float(util.parse_duration(cfg.get("session_sticky.ttl", "30m"), 1800)) if cfg else 1800.0,
            enabled=sticky_on,
        )
        self.pool = Pool(store, cfg, self.affinity)
        self.ledger = UsageLedger(
            store.data_dir if hasattr(store, "data_dir") else "./data",
            retention_days=int(cfg.get("usage.retention_days", 7)) if cfg else 7,
        )
        self.ledger.load()
        self.max_payload_bytes = int(float(cfg.get("server.max_payload_mb", 16)) * 1024 * 1024) if cfg else 16 * 1024 * 1024
        self.scheduler = None  # 由 app 装配时注入
        self._lock = threading.RLock()

    # -- 生命周期 -----------------------------------------------------------
    def close(self) -> None:
        try:
            self.pool.flush(force=True)
        finally:
            self.ledger.flush() if hasattr(self.ledger, "flush") else None
            self.client.close()

    # -- 鉴权 ---------------------------------------------------------------
    def authenticate(self, token: str) -> Optional[ApiKey]:
        """空 token 在"没配置任何密钥"时放行（本机自用），否则一律拒绝。"""
        keys = self.store.list_keys() if hasattr(self.store, "list_keys") else []
        configured = [item for item in keys if not item.disabled]
        default_key = str(self.cfg.get("api_key") or "") if self.cfg else ""
        if not token:
            if not configured and not default_key:
                return None  # 开放模式
            raise ServiceError("缺少 API key", 401, "invalid_api_key", "authentication_error")
        for item in configured:
            if item.key == token:
                return item
        if default_key and token == default_key:
            return None
        raise ServiceError("API key 无效", 401, "invalid_api_key", "authentication_error")

    def resolve_realm(self, wanted: str, key: Optional[ApiKey], header_realm: str = "") -> str:
        if wanted:
            return identity.normalize_realm(wanted)
        if key is not None and getattr(key, "realm", ""):
            return identity.normalize_realm(key.realm)
        if header_realm:
            return identity.normalize_realm(header_realm)
        return identity.normalize_realm(str(self.cfg.get("default_realm") or "cn")) if self.cfg else "cn"

    # -- 主流程 -------------------------------------------------------------
    def chat_completions(self, request: Any) -> Any:
        """处理一次 ``POST /v1/chat/completions``。"""
        from .httpserver import json_response, stream_response

        started = time.time()
        token = request.bearer() or request.arg("api_key")
        key = self.authenticate(token)

        body = request.json_body()
        if not isinstance(body, dict):
            raise ServiceError("请求体必须是 JSON 对象", 400, "invalid_request")

        model, realm = self._resolve_model(body, request, key)
        want_stream = bool(body.get("stream"))
        meta_body = chat_adapter.prepare_request(
            body,
            self.catalog.meta_map(realm),
            sanitize_prompt=str(self.cfg.get("prompt.mode") or "") == "sanitize",
        )
        meta_body["model"] = model
        payload = json.dumps(meta_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        stick = make_session_key(request.headers, body)
        meta = identity.ChatMeta.for_request(
            body,
            request.header("X-Trace-Id") or request.header("X-B3-TraceId"),
            session_hint=stick,
        )
        tool_names = _tool_names(meta_body)
        completion_id = "chatcmpl-" + identity.new_message_id()[:24]

        account, response = self._open_upstream(
            realm=realm, model=model, key=key, stick=stick, payload=payload, meta=meta, request=request
        )

        if want_stream:
            return stream_response(
                self._relay_stream(
                    account=account,
                    response=response,
                    completion_id=completion_id,
                    model=model,
                    realm=realm,
                    key=key,
                    tool_names=tool_names,
                    started=started,
                )
            )

        # 非流式：本地聚合。上游本来只给流，是我们替客户端攒完再回一条完整 JSON。
        try:
            lines = list(response.iter_text(float(self.cfg.get("upstream.idle_timeout_seconds", 300))))
        finally:
            response.close()
        aggregated = chat_adapter.aggregate(lines, completion_id, model, tool_names)
        usage = aggregated.get("usage") or {}
        self._record(
            started=started, model=model, realm=realm, account=account, key=key,
            stream=False, outcome="completed", usage=usage, status=200, ttft=0,
        )
        self.pool.note_success(account, model=model, session_key=stick)
        self.pool.release(account)
        return json_response(aggregated)

    # -- 选号 + 出站 --------------------------------------------------------
    def _open_upstream(
        self,
        *,
        realm: str,
        model: str,
        key: Optional[ApiKey],
        stick: str,
        payload: bytes,
        meta: identity.ChatMeta,
        request: Any,
    ) -> Tuple[Any, Any]:
        """选号、发请求，失败按策略换号。返回 (账号, 200 响应)。"""
        exclude: List[str] = []
        last_error: Optional[UpstreamError] = None
        attempts = 0
        while attempts <= MAX_ROTATE:
            attempts += 1
            account = self.pool.select(
                realm,
                model,
                key=key,
                session_key=stick,
                exclude=exclude,
            )
            if account is None:
                if last_error is not None:
                    raise self._client_error(last_error, realm, model)
                blocked = self.pool.model_blocked(realm, model)
                if blocked:
                    # 池子是空的，但原因是"这个模型谁都跑不通"，不是"账号都在冷却"。
                    detail = blocked.get("reason") or "上游没有该模型的服务"
                    raise ServiceError(
                        f"模型 {model} 在上游不可用（{detail}），请换一个模型",
                        400, "model_unavailable", "invalid_request_error",
                    )
                raise ServiceError(
                    f"{realm} 域没有可用账号（都被冷却或未启用）", 503, "no_account", "api_error"
                )
            exclude.append(account.key)

            try:
                self._ensure_ready(account, realm)
                self._pace(account)
                url = self.client.base_url(realm) + CHAT_PATH
                headers = identity.chat_headers(
                    account, self.cfg, meta=meta, client_ip=request.client_ip
                )
                proxy = str(getattr(account, "proxy", "") or "")
                response = self.client.post(url, headers, raw_body=payload, proxy=proxy)
            except UpstreamError as err:
                last_error = err
                rotate, auth_retry = self._absorb(account, err, model, realm)
                self.pool.release(account)
                if auth_retry:
                    # 令牌过期是可修复的：刷新一次再用同一个号，比换号便宜得多。
                    try:
                        self.auth.refresh(account, force=True)
                        exclude.pop()  # 允许再试这个号一次
                        attempts -= 1
                        continue
                    except Exception as exc:  # 刷新失败 = 这个号要重新登录
                        log.warning("账号 %s 刷新失败: %s", account.label, exc)
                        self.pool.note_auth_dead(account)
                        rotate = True
                if not rotate:
                    raise self._client_error(err, realm, model) from err
                self._backoff(attempts)
                continue
            except Exception as exc:  # 本地配置/网络栈问题，不该重试
                self.pool.release(account)
                log.exception("请求 %s 失败", model)
                raise ServiceError(f"网关内部错误: {exc}", 500, "internal_error", "api_error") from exc
            return account, response

        error = last_error
        raise self._client_error(error, realm, model) if error is not None else ServiceError(
            "上游持续失败", 503, "upstream_unavailable", "api_error"
        )

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

    def _absorb(self, account: Any, err: UpstreamError, model: str, realm: str) -> Tuple[bool, bool]:
        """把一次失败记到号上，并决定"换不换号 / 要不要先刷新令牌"。"""
        message = err.message or ""
        code = err.code
        if CODE_FINGERPRINT in message:
            # 指纹拦截：换号没用，换的是"客户端指纹"，不是账号。
            log.warning("指纹被拦（%s）：检查 UA/头族，不要换号", model)
            return False, False
        if code == CODE_RESTRICTED or (err.status == 403 and "11140" in message):
            # 风控：实测一轮请求能把 7 个号一起封掉，所以绝不换号。
            self.pool.note_restricted(account, message)
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
        if err.kind == "transient" or err.status >= 500:
            self.pool.note_failure(account, message or "上游 5xx")
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
        if err.code == CODE_RESTRICTED or err.status == 403:
            return ServiceError("账号被上游风控限制，已停止重试以免连坐", 403, "restricted", "api_error")
        if err.code == CODE_PROMPT_TOO_LONG:
            return ServiceError("prompt 过长，上游拒绝（换号无效）", 400, "prompt_too_long", "invalid_request_error")
        if err.kind == "quota":
            return ServiceError("所有可用账号额度不足", 402, "insufficient_quota", "api_error")
        if err.kind == "rate_limit":
            return ServiceError("上游限流，请稍后重试", 429, "rate_limited", "rate_limit_error")
        if err.kind == "transient" or err.status >= 500:
            return ServiceError("上游暂时不可用", 503, "upstream_unavailable", "api_error")
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

    # -- 流式转发 -----------------------------------------------------------
    def _relay_stream(
        self,
        *,
        account: Any,
        response: Any,
        completion_id: str,
        model: str,
        realm: str,
        key: Optional[ApiKey],
        tool_names: List[str],
        started: float,
    ) -> Iterator[bytes]:
        """把上游流翻译成客户端帧。

        进了这个生成器就说明响应头已经发出去了：**再失败也不能换号**，只能把错误
        作为一帧 ``upstream_stream_error`` 告诉客户端，并按已产生的用量记账
        （这些 token 上游已经计费了，装作没发生只会让额度悄悄漏掉）。
        """
        idle = float(self.cfg.get("upstream.idle_timeout_seconds", 300))
        ttft = 0.0
        first_byte_at = 0.0
        usage: Dict[str, Any] = {}
        outcome = "completed"
        error_text = ""
        try:
            filt = None
            for payload in chat_adapter.stream_frames(
                response.iter_text(idle), completion_id, model, tool_names
            ):
                if not first_byte_at:
                    first_byte_at = time.time()
                    ttft = (first_byte_at - started) * 1000.0
                if _is_usage_frame(payload):
                    usage = _usage_of(payload) or usage
                yield payload
        except GeneratorExit:
            outcome = "client_aborted"
            raise
        except UpstreamError as err:
            outcome = "upstream_aborted"
            error_text = err.message or str(err)
            log.warning("流中断 %s/%s: %s", realm, model, error_text)
            yield sse.sse_bytes(
                {
                    "error": {
                        "message": "上游流中断：" + error_text[:200],
                        "type": "upstream_stream_error",
                        "code": "stream_interrupted",
                    }
                }
            )
        except Exception as exc:  # 解析/网络都可能炸，别让线程带着异常死掉
            outcome = "failed"
            error_text = str(exc)
            log.exception("流处理失败 %s/%s", realm, model)
            yield sse.sse_bytes(
                {
                    "error": {
                        "message": "网关转发失败：" + error_text[:200],
                        "type": "gateway_error",
                        "code": "stream_failed",
                    }
                }
            )
        finally:
            response.close()
            self._record(
                started=started, model=model, realm=realm, account=account, key=key,
                stream=True, outcome=outcome, usage=usage, status=200,
                ttft=ttft, error=error_text,
            )
            if outcome == "completed":
                self.pool.note_success(
                    account, model=model,
                    session_key=make_session_key({}, {"model": model}),
                )
            self.pool.release(account)

    # -- 记账 ---------------------------------------------------------------
    def _record(
        self,
        *,
        started: float,
        model: str,
        realm: str,
        account: Any,
        key: Optional[ApiKey],
        stream: bool,
        outcome: str,
        usage: Dict[str, Any],
        status: int,
        ttft: float = 0.0,
        error: str = "",
    ) -> None:
        elapsed = int((time.time() - started) * 1000)
        prompt = _num(usage.get("prompt_tokens"))
        completion = _num(usage.get("completion_tokens"))
        reasoning = _num(usage.get("completion_tokens_details", {}).get("reasoning_tokens") if isinstance(usage.get("completion_tokens_details"), dict) else usage.get("reasoning_tokens"))
        cached = _num((usage.get("prompt_tokens_details") or {}).get("cached_tokens") if isinstance(usage.get("prompt_tokens_details"), dict) else usage.get("cached_tokens"))
        total = _num(usage.get("total_tokens")) or (prompt + completion)
        # 上游 usage 里带 credit（这次请求扣了多少积分）。台账要记它，否则面板上
        # 永远显示"花了 0 积分"，用户没法知道钱去哪了。
        credit = _num(usage.get("credit"))
        if not credit:
            credit = _num(usage.get("credits")) or _num(usage.get("credit_cost"))
        event = {
            "at": int(started),
            "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
            "model": model,
            "stream": stream,
            "outcome": outcome,
            "kind": "chat.completions",
            "elapsed_ms": elapsed,
            "ttft_ms": int(ttft),
            "prompt_tokens": int(prompt),
            "completion_tokens": int(completion),
            "reasoning_tokens": int(reasoning),
            "cached_tokens": int(cached),
            "total_tokens": int(total),
            "credit": round(float(credit), 6),
            "realm": realm,
            "account": getattr(account, "uid", "") or getattr(account, "id", ""),
            "key": getattr(key, "key", "")[:12] if key is not None else "",
            "status": status,
        }
        if error:
            event["error"] = error[:200]
        if outcome == "failed":
            event["status"] = status or 502
        self.ledger.record(event)
        if key is not None and hasattr(self.store, "touch_key"):
            self.store.touch_key(key.key, int(prompt), int(completion))

    # -- 辅助端点 -----------------------------------------------------------
    def _resolve_model(self, body: Dict[str, Any], request: Any, key: Optional[ApiKey]) -> Tuple[str, str]:
        raw = str(body.get("model") or self.cfg.get("models.default") or "").strip()
        realm_hint = ""
        for prefix, realm in (("cn:", "cn"), ("global:", "global")):
            if raw.startswith(prefix):
                realm_hint, raw = realm, raw[len(prefix):]
        if not raw:
            raise ServiceError("缺少 model 字段", 400, "invalid_request")
        if key is not None and not key.allows_model(raw):
            raise ServiceError(f"API key 不允许使用模型 {raw}", 403, "model_not_allowed", "invalid_request_error")
        realm = self.resolve_realm(
            realm_hint or request.arg("realm"), key, request.header("X-Realm")
        )
        return raw, realm

    def models(self, request: Any) -> Any:
        from .httpserver import json_response

        token = request.bearer() or request.arg("api_key")
        key = self.authenticate(token)
        realm = self.resolve_realm(request.arg("realm"), key, request.header("X-Realm"))
        self._ensure_catalog(realm)
        return json_response(self.catalog.openai_list(realm))

    def _ensure_catalog(self, realm: str) -> None:
        if self.catalog.names(realm):
            return
        accounts = [item for item in self.store.list_accounts(realm) if item.enabled and item.access_token]
        if not accounts:
            return
        self.catalog.fetch(accounts[0], realm)

    def status(self, request: Any = None) -> Dict[str, Any]:
        # /status 里有账号数量、余额合计与用量：这些不该匿名可见，所以和 /v1/* 同等待遇。
        if request is not None:
            self.authenticate(request.bearer() or request.arg("api_key"))
        return {
            "ok": True,
            "listening": str(self.cfg.get("listen") or ""),
            "accounts": {
                realm: len(self.store.list_accounts(realm))
                for realm in ("cn", "global")
            },
            "pool": self.pool.stats(),
            "usage": self.ledger.summary(),
            "models": self.catalog.snapshot(),
        }


def _tool_names(body: Dict[str, Any]) -> List[str]:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return []
    names = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.append(function["name"])
    return names


def _mentions_model(message: str) -> bool:
    lowered = message.lower()
    return "model" in lowered or "模型" in message


def _num(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _is_usage_frame(payload: bytes) -> bool:
    return b'"usage"' in payload


def _usage_of(payload: bytes) -> Optional[Dict[str, Any]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            return obj["usage"]
    return None
