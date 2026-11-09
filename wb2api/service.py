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
from .billing import Billing
from .credits import CreditBook, summarize as summarize_credits
from .diagnostics import Diagnostics
from .egress import Egress
from .log import get_logger
from .markup import MARKUP_HINT
from .models import ModelCatalog
from .pool import Pool
from .proxies import ProxyPool
from .reqlog import RequestLog
from .store import ApiKey
from .sticky import SessionAffinity, session_key as make_session_key
from .upstream import UpstreamClient, UpstreamError, is_restricted
from .metrics import Metrics
from .usage import DEFAULT_MAX_BYTES, UsageLedger

log = get_logger("service")

MAX_ROTATE = 3  # 再换 3 个号；加上第一次共 4 次尝试
BACKOFF_BASE = 0.5
BACKOFF_MAX = 8.0
CHAT_PATH = "/v2/chat/completions"
KEY_RATE_WINDOW = 60.0  # 每 key 速率窗口：滑动一分钟，不是固定分钟桶

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
            page_size=int(cfg.get("session_sticky.page_size", 3)) if cfg else 0,
        )
        self.pool = Pool(store, cfg, self.affinity)
        self.egress = Egress.from_config(cfg)
        self.ledger = UsageLedger(
            store.data_dir if hasattr(store, "data_dir") else "./data",
            retention_days=int(cfg.get("usage.retention_days", 7)) if cfg else 7,
            async_write=bool(cfg.get("usage.async_write", True)) if cfg else True,
            max_bytes=int(float(cfg.get("usage.max_mb", 64)) * 1024 * 1024) if cfg else DEFAULT_MAX_BYTES,
        )
        self.ledger.load()
        # 余额的"钱"视角：资源包清单 + 到期分桶 + 变动台账（回答"钱去哪了"）。
        self.billing = Billing(self.client, cfg)
        self.credit_book = CreditBook(
            store.data_dir if hasattr(store, "data_dir") else "./data",
            retention_days=int(cfg.get("credits.retention_days", 30)) if cfg else 30,
        )
        self.max_payload_bytes = int(float(cfg.get("server.max_payload_mb", 16)) * 1024 * 1024) if cfg else 16 * 1024 * 1024
        data_dir = store.data_dir if hasattr(store, "data_dir") else "./data"
        # 出口池：账号自己配了 proxy 的永远优先，其它请求从池里挑。
        self.proxies = ProxyPool(cfg)
        # 失败报文留存（脱敏、限大小、按天封顶）：排障时"上游原话"比 code 有用得多。
        self.diag = Diagnostics(
            data_dir,
            enabled=bool(cfg.get("diagnostics.enabled", True)) if cfg else True,
            max_body_bytes=int(cfg.get("diagnostics.max_body_bytes", 8192)) if cfg else 8192,
            retention_days=int(cfg.get("diagnostics.retention_days", 3)) if cfg else 3,
            daily_max=int(cfg.get("diagnostics.daily_max", 200)) if cfg else 200,
        )
        # 逐请求明细：一条请求一行，面板能按状态/模型/账号/关键字翻。
        self.reqlog = RequestLog(
            data_dir,
            enabled=bool(cfg.get("requests.enabled", True)) if cfg else True,
            retention_days=int(cfg.get("requests.retention_days", 3)) if cfg else 3,
            capacity=int(cfg.get("requests.capacity", 500)) if cfg else 500,
        )
        self.reqlog.load()
        self.scheduler = None  # 由 app 装配时注入
        self._lock = threading.RLock()
        # 每 key 的在飞计数：一个被 API key 卖出去的额度，不该被一个客户端占满。
        self._key_inflight: Dict[str, int] = {}
        self._key_hits: Dict[str, List[float]] = {}
        self.metrics = Metrics(capacity=int(cfg.get("metrics.capacity", 2000)) if cfg else 2000)
        self._key_lock = threading.Lock()
        # WAF 波次：内容被拦是"这份内容"的问题，不是号的问题——但连续踩中说明
        # 出口/阶段已经被盯上，这时候继续换号只是把整池的账号都送上去记一笔。
        self._waf_hits: Dict[str, List[Tuple[str, float]]] = {}

    # -- 生命周期 -----------------------------------------------------------
    def close(self) -> None:
        try:
            self.pool.flush(force=True)
        finally:
            # 台账有独立写线程：先排空队列再退出，否则最后几条记录会丢。
            self.ledger.close()
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

    # -- 每 key 并发 ---------------------------------------------------------
    def _acquire_key(self, key: Optional[ApiKey]) -> str:
        """占一个 key 名额；并发或每分钟速率超限就直接 429。

        账号池的并发槽是"保护上游"的，这里是"保护别的使用者"的：一个人挂 20 个
        并发把 3 个槽全占住，其他人就只能在 503 里等——限流该发生在入口，而不是
        让别人排队失败。

        速率用滑动窗口（默认 60 秒）而不是固定分钟桶：固定桶会在整分钟边界上放
        两倍的量过去，对上游来说就是"前一分钟 0 次、这一分钟 20 次"。
        """
        if key is None or not key.key:
            return ""
        limit = int(getattr(key, "max_concurrency", 0) or 0)
        rpm = int(getattr(key, "rate_limit", 0) or 0)
        now = time.time()
        with self._key_lock:
            if rpm > 0:
                window = [stamp for stamp in self._key_hits.get(key.key, []) if now - stamp < KEY_RATE_WINDOW]
                if len(window) >= rpm:
                    oldest = window[0]
                    self._key_hits[key.key] = window
                    raise ServiceError(
                        "这把 API key 的速率上限是 %d 次/分钟，请稍后再试" % rpm,
                        429, "key_rate_limited", "rate_limit_error",
                        retry_after=max(0.5, round(KEY_RATE_WINDOW - (now - oldest), 1)),
                    )
                window.append(now)
                self._key_hits[key.key] = window
            if limit <= 0:
                return ""
            used = self._key_inflight.get(key.key, 0)
            if used >= limit:
                raise ServiceError(
                    "这把 API key 的并发上限是 %d，当前已满" % limit,
                    429, "key_concurrency_exceeded", "rate_limit_error",
                    retry_after=1.0,
                )
            self._key_inflight[key.key] = used + 1
        return key.key

    def _release_key(self, slot: str) -> None:
        if not slot:
            return
        with self._key_lock:
            used = self._key_inflight.get(slot, 0) - 1
            if used > 0:
                self._key_inflight[slot] = used
            else:
                self._key_inflight.pop(slot, None)

    def key_inflight(self, key: str) -> int:
        with self._key_lock:
            return self._key_inflight.get(key, 0)

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
            append_prompt=str(self.cfg.get("prompt.append") or ""),
        )
        meta_body["model"] = model
        payload = json.dumps(meta_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        stick = make_session_key(request.headers, body)
        if chat_adapter.has_media(body):
            # 媒体请求不绑亲和：同一张图被反复提交和多轮对话无关，钉在同一个号上
            # 只会让这个号的请求画像更反常。
            stick = ""
        # 有的抓包环境里官方 chat 不带稳定的 conversation id，这时"派生"反而更显眼，
        # 所以留一个开关：关掉就退回每轮随机（老行为）。
        hint = stick if bool(self.cfg.get("upstream.derive_conversation_id", True)) else ""
        meta = identity.ChatMeta.for_request(
            body,
            request.header("X-Trace-Id") or request.header("X-B3-TraceId"),
            session_hint=hint,
        )
        tool_names = _tool_names(meta_body)
        completion_id = "chatcmpl-" + identity.new_message_id()[:24]
        # 请求 id：客户端报障时只要给这个串，日志和台账都能直接定位到那一条。
        request_id = "req_" + util.rand_hex(8)
        request.state["request_id"] = request_id
        alias = str(request.state.get("model_alias") or "")
        req_extra = {
            "path": "/v1/chat/completions",
            "client_ip": str(getattr(request, "client_ip", "") or ""),
            "ua": str(request.header("User-Agent", ""))[:120],
        }

        key_slot = self._acquire_key(key)
        try:
            account, response, attempts = self._open_upstream(
                realm=realm, model=model, key=key, stick=stick, payload=payload, meta=meta, request=request
            )
        except BaseException:
            self._release_key(key_slot)
            raise
        extra_headers = {"X-Request-Id": request_id}
        if alias:
            extra_headers["X-Model-Alias"] = alias

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
                    stick=stick,
                    request_id=request_id,
                    attempts=attempts,
                    alias=alias,
                    key_slot=key_slot,
                    extra=req_extra,
                ),
                headers=extra_headers,
            )
        # 非流式：本地聚合。上游本来只给流，是我们替客户端攒完再回一条完整 JSON。
        # 槽位必须在 finally 里松开：聚合/记账任何一步抛异常都不能把账号永久占住
        # （in_flight 只增不减 = 几个请求之后整域就是 503 no_account）。
        try:
            lines = list(response.iter_text(float(self.cfg.get("upstream.idle_timeout_seconds", 300))))
            aggregated = chat_adapter.aggregate(lines, completion_id, model, tool_names)
            usage = aggregated.get("usage") or {}
            try:
                self._record(
                    started=started, model=model, realm=realm, account=account, key=key,
                    stream=False, outcome="completed", usage=usage, status=200, ttft=0,
                    request_id=request_id, attempts=attempts, alias=alias,
                    extra=req_extra,
                )
            except Exception:  # 记账失败不该让一个已经成功的请求变成 500
                log.exception("记账失败（%s）", model)
            self.pool.note_success(account, model=model, session_key=stick)
            return json_response(aggregated, headers=extra_headers)
        finally:
            response.close()
            self.pool.release(account)
            self._release_key(key_slot)

    # -- Anthropic Messages 入口 -------------------------------------------
    def messages(self, request: Any) -> Any:
        """处理一次 ``POST /v1/messages``（Anthropic 形状）。

        上游只讲 OpenAI，所以这里翻译两次：请求翻成 chat，响应/流再翻回 Anthropic。
        翻译本身在 ``adapters/messages.py``，这里是编排（选号、重试、记账与 chat 一致）。
        """
        from .adapters import messages as msg_adapter
        from .httpserver import json_response, stream_response

        started = time.time()
        token = request.bearer() or request.arg("api_key")
        key = self.authenticate(token)

        body = request.json_body()
        if not isinstance(body, dict):
            raise ServiceError("请求体必须是 JSON 对象", 400, "invalid_request")
        chat_body = msg_adapter.to_chat(body)
        if not chat_body.get("messages"):
            raise ServiceError("messages 不能为空", 400, "invalid_request")

        model, realm = self._resolve_model(chat_body, request, key)
        want_stream = bool(body.get("stream"))
        prepared = chat_adapter.prepare_request(
            chat_body,
            self.catalog.meta_map(realm),
            sanitize_prompt=str(self.cfg.get("prompt.mode") or "") == "sanitize",
            append_prompt=str(self.cfg.get("prompt.append") or ""),
        )
        prepared["model"] = model
        payload = json.dumps(prepared, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        stick = make_session_key(request.headers, chat_body)
        if chat_adapter.has_media(chat_body):
            stick = ""
        hint = stick if bool(self.cfg.get("upstream.derive_conversation_id", True)) else ""
        meta = identity.ChatMeta.for_request(
            chat_body,
            request.header("X-Trace-Id") or request.header("X-B3-TraceId"),
            session_hint=hint,
        )
        tool_names = _tool_names(prepared)
        completion_id = "chatcmpl-" + identity.new_message_id()[:24]
        request_id = "req_" + util.rand_hex(8)
        request.state["request_id"] = request_id
        alias = str(request.state.get("model_alias") or "")
        input_tokens = msg_adapter.count_tokens(body)
        req_extra = {
            "path": "/v1/messages",
            "client_ip": str(getattr(request, "client_ip", "") or ""),
            "ua": str(request.header("User-Agent", ""))[:120],
        }

        key_slot = self._acquire_key(key)
        try:
            account, response, attempts = self._open_upstream(
                realm=realm, model=model, key=key, stick=stick, payload=payload, meta=meta, request=request
            )
        except BaseException:
            self._release_key(key_slot)
            raise

        extra_headers = {"X-Request-Id": request_id}
        if alias:
            extra_headers["X-Model-Alias"] = alias

        if want_stream:
            stream = msg_adapter.MessageStream(
                model, message_id="msg_" + identity.new_message_id()[:24], input_tokens=input_tokens
            )
            return stream_response(
                self._relay_messages(
                    account=account, response=response, stream=stream, model=model, realm=realm,
                    key=key, started=started, stick=stick, request_id=request_id,
                    attempts=attempts, alias=alias, key_slot=key_slot, extra=req_extra,
                ),
                headers=extra_headers,
            )

        try:
            lines = list(response.iter_text(float(self.cfg.get("upstream.idle_timeout_seconds", 300))))
            aggregated = chat_adapter.aggregate(lines, completion_id, model, tool_names)
            usage = aggregated.get("usage") or {}
            try:
                self._record(
                    started=started, model=model, realm=realm, account=account, key=key,
                    stream=False, outcome="completed", usage=usage, status=200, ttft=0,
                    request_id=request_id, attempts=attempts, alias=alias, kind="messages",
                    extra=req_extra,
                )
            except Exception:
                log.exception("记账失败（%s）", model)
            self.pool.note_success(account, model=model, session_key=stick)
            return json_response(msg_adapter.from_chat(aggregated, model), headers=extra_headers)
        finally:
            response.close()
            self.pool.release(account)
            self._release_key(key_slot)

    def _relay_messages(
        self,
        *,
        account: Any,
        response: Any,
        stream: Any,
        model: str,
        realm: str,
        key: Optional[ApiKey],
        started: float,
        stick: str = "",
        request_id: str = "",
        attempts: int = 1,
        alias: str = "",
        key_slot: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Iterator[bytes]:
        """把上游的 OpenAI 流重排成 Anthropic 事件流。"""
        from .adapters import messages as msg_adapter

        idle = float(self.cfg.get("upstream.idle_timeout_seconds", 300))
        usage: Dict[str, Any] = {}
        outcome = "completed"
        error_text = ""
        ttft = 0.0
        first = True
        try:
            for item in stream.start():
                yield msg_adapter.stream_frames([item]).__next__()
            for parsed in sse.iter_sse(response.iter_text(idle)):
                if parsed.event == "done" or (parsed.data or "").strip() == "[DONE]":
                    break
                raw = (parsed.data or "").strip()
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"] or usage
                    if first:
                        # 第一帧就有 usage 的情况很少，真有时至少别把 input_tokens 记成 0
                        first = False
                if first:
                    ttft = (time.time() - started) * 1000.0
                    first = False
                for item in stream.feed(chunk):
                    yield msg_adapter.stream_frames([item]).__next__()
            for item in stream.finish():
                yield msg_adapter.stream_frames([item]).__next__()
        except GeneratorExit:
            outcome = "client_aborted"
            raise
        except UpstreamError as err:
            outcome = "upstream_aborted"
            error_text = err.message or str(err)
            for item in stream.error("上游流中断：" + error_text[:200], "upstream_stream_error"):
                yield msg_adapter.stream_frames([item]).__next__()
        except Exception as exc:
            outcome = "failed"
            error_text = str(exc)
            log.exception("Messages 流处理失败 %s/%s", realm, model)
            for item in stream.error("网关转发失败：" + error_text[:200], "gateway_error"):
                yield msg_adapter.stream_frames([item]).__next__()
        finally:
            self.pool.release(account)
            self._release_key(key_slot)
            try:
                response.close()
            except Exception:
                pass
            if not usage.get("completion_tokens"):
                usage = dict(usage)
                usage["completion_tokens"] = stream.output_tokens
            if not usage.get("prompt_tokens"):
                usage = dict(usage)
                usage["prompt_tokens"] = stream.input_tokens
            try:
                self._record(
                    started=started, model=model, realm=realm, account=account, key=key,
                    stream=True, outcome=outcome, usage=usage,
                    status=200 if outcome == "completed" else 499, ttft=ttft,
                    error=error_text, request_id=request_id, attempts=attempts,
                    alias=alias, kind="messages", extra=extra,
                )
            except Exception:
                log.exception("记账失败（%s）", model)
            if outcome == "completed":
                try:
                    self.pool.note_success(account, model=model, session_key=stick)
                except Exception:
                    log.exception("标记成功失败（%s）", model)

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
    ) -> Tuple[Any, Any, int]:
        """选号、发请求，失败按策略换号。返回 (账号, 200 响应, 尝试次数)。"""
        exclude: List[str] = []
        last_error: Optional[UpstreamError] = None
        attempts = 0
        neutralized = False  # 内容审核降级只允许一次
        wave = self.waf_wave(realm)
        if wave.get("active"):
            # 一分钟内两个不同的号都踩到 WAF：这不是内容问题，是出口被抓了。
            # 直接回给客户端，别再把剩下的号一个个送上去记一笔。
            raise ServiceError(
                "上游风控拦截（%s 域近一分钟内多个账号被拦），请 %d 秒后再试"
                % (realm, int(wave.get("for_seconds") or 60)),
                403, "content_blocked", "api_error",
                retry_after=float(wave.get("for_seconds") or 60),
            )
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

            url = ""
            proxy = ""
            try:
                self._ensure_ready(account, realm)
                self._pace(account)
                url = self.client.base_url(realm) + CHAT_PATH
                headers = identity.chat_headers(
                    account, self.cfg, meta=meta, client_ip=request.client_ip
                )
                proxy = str(getattr(account, "proxy", "") or "")
                if not proxy and self.proxies.enabled():
                    # 账号没单独配出口时从池里挑一个：出口 IP 跟着请求一起变，
                    # 比"所有账号都从家里那条宽带出去"难关联得多。
                    proxy = self.proxies.pick(realm)
                # 同一出口还在退避就先等一等：一口气重试只会让上游更确信这是脚本。
                # 但这时账号槽位已经占了，睡太久会把整域的并发额度（默认 3）锁死，
                # 所以持槽期间最多等 2 秒，剩下的交给下一轮退避。
                wait = self.egress.wait_for(Egress.key_for(url, proxy))
                if wait > 0:
                    time.sleep(min(wait, 2.0))
                response = self.client.post(url, headers, raw_body=payload, proxy=proxy)
            except UpstreamError as err:
                last_error = err
                self._dump_failure(err, account=account, realm=realm, model=model, url=url, meta=meta,
                                   request=request, payload=payload)
                if err.kind in ("timeout", "transient") or err.status >= 500:
                    # 链路问题记在出口上：记到账号头上会让一次网络抖动把整池打断
                    self.egress.note_failure(Egress.key_for(url, proxy), err.message or "")
                    if proxy:
                        self.proxies.note_failure(proxy, err.message or "")
                if err.kind == "waf" and not neutralized and self.cfg.get("prompt.content_retry", True):
                    # 内容审核打回时换号没用（同一份内容在哪个号上都会被拦），
                    # 但把用户自带的系统提示词换成中性最小版常常能过。只试一次，
                    # 且这一次不记冷却——同一份请求的第二发不该算这个号的账。
                    neutral = chat_adapter.neutralize_payload(payload)
                    if neutral is not None:
                        neutralized = True
                        payload = neutral
                        self.pool.release(account)
                        log.info("内容审核打回（%s）：改用中性提示词重试一次", model)
                        continue
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
            self.egress.note_success(Egress.key_for(url, proxy))
            if proxy:
                self.proxies.note_success(proxy)
            return account, response, attempts

        error = last_error
        raise self._client_error(error, realm, model) if error is not None else ServiceError(
            "上游持续失败", 503, "upstream_unavailable", "api_error"
        )

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
        except Exception:
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
        stick: str = "",
        request_id: str = "",
        attempts: int = 1,
        alias: str = "",
        key_slot: str = "",
        extra: Optional[Dict[str, Any]] = None,
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
            # 顺序很重要：先松开槽位，再关连接、记账。记账要落盘，磁盘满/权限问题
            # 抛出来时槽位已经还回去了，不会把账号永久占死。
            self.pool.release(account)
            self._release_key(key_slot)
            try:
                response.close()
            except Exception:
                pass
            try:
                self._record(
                    started=started, model=model, realm=realm, account=account, key=key,
                    stream=True, outcome=outcome, usage=usage, status=200,
                    ttft=ttft, error=error_text,
                    request_id=request_id, attempts=attempts, alias=alias,
                    extra=extra,
                )
            except Exception:
                log.exception("记账失败（%s）", model)
            if outcome == "completed":
                # 必须用**真实**会话键绑定亲和：流式是主路径，这里传空键等于
                # 从来没有亲和——同一段对话会被摊到每台"设备"上，既漏缓存也留关联痕迹。
                try:
                    self.pool.note_success(account, model=model, session_key=stick)
                except Exception:
                    log.exception("绑定会话亲和失败（%s）", model)

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
        request_id: str = "",
        attempts: int = 1,
        alias: str = "",
        kind: str = "chat.completions",
        extra: Optional[Dict[str, Any]] = None,
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
            "kind": kind,
            "elapsed_ms": elapsed,
            # 非流式没有"首字"可言：写 0 会让分位数统计把一堆 0 算成"快得离谱"，
            # 写 None 让面板显示"—"、让 metrics 直接跳过这个样本。
            "ttft_ms": int(ttft) if ttft > 0 else None,
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
            "request_id": request_id,
            "attempts": int(attempts),
        }
        if alias and alias != model:
            event["alias"] = alias
        if error:
            event["error"] = error[:200]
        if outcome == "failed":
            event["status"] = status or 502
        if extra:
            # 明细字段（客户端 IP / UA / 入口路径）只进逐请求日志，不进聚合台账：
            # 聚合表要小，明细表要全。
            for name, value in extra.items():
                if value not in (None, ""):
                    event[name] = value
        self.ledger.record(event)
        try:
            self.reqlog.record(event)
        except Exception:
            log.debug("逐请求日志写入失败", exc_info=True)
        # 指标只留最近 N 条（内存里），和台账的 7 天回放是两件事。
        try:
            self.metrics.observe(event)
        except Exception:
            log.debug("metrics observe failed", exc_info=True)
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
        # 别名映射：客户端写 gpt-4o，上游收 deepseek-v4-flash。白名单允许任一侧，
        # 否则"配了别名却用不了"这种坑会让人查很久。
        alias = str(self.catalog.resolve_alias(raw) or "")
        model = alias or raw
        if key is not None and not (key.allows_model(raw) or (alias and key.allows_model(alias))):
            raise ServiceError(f"API key 不允许使用模型 {raw}", 403, "model_not_allowed", "invalid_request_error")
        realm = self.resolve_realm(
            realm_hint or request.arg("realm"), key, request.header("X-Realm")
        )
        if alias:
            request.state["model_alias"] = raw
        return model, realm

    def models(self, request: Any) -> Any:
        from .httpserver import json_response

        token = request.bearer() or request.arg("api_key")
        key = self.authenticate(token)
        realm = self.resolve_realm(request.arg("realm"), key, request.header("X-Realm"))
        self._ensure_catalog(realm)
        allowed = key.allows_model if key is not None else None
        return json_response(self.catalog.openai_list(realm, allowed))

    def catalog_view(self, realm: str = "") -> Dict[str, Any]:
        """面板用：把模型列成"可以直接多选"的清单。

        `realm` 留空时返回两个域的全部（配一把 key 本来就不该被迫先选域），
        每项都带 `realm`，选中之后写进 `key.models` 的是确定的模型 id。
        """
        realms = [identity.normalize_realm(realm)] if realm else ["cn", "global"]
        rows: List[Dict[str, Any]] = []
        for item in realms:
            self._ensure_catalog(item)
            for info in self.catalog.entries(item, chat_only=True):
                if self.catalog.hidden(info.id):
                    continue
                row: Dict[str, Any] = {"id": info.id, "name": info.name, "realm": item}
                if info.alias_of:
                    row["alias_of"] = info.alias_of
                if info.max_output:
                    row["max_output_tokens"] = info.max_output
                if info.context_length:
                    row["context_length"] = info.context_length
                rows.append(row)
        rows.sort(key=lambda entry: (entry["realm"], entry["id"]))
        counts = {item: sum(1 for entry in rows if entry["realm"] == item) for item in realms}
        return {"models": rows, "realms": counts, "total": len(rows)}

    def refresh_credit(self, account: Any, realm: str = "", note: str = "") -> Dict[str, Any]:
        """刷新一个账号的余额，并把"钱什么时候过期"一起留下。

        返回 ``{ok, credits, expiring_soon, buckets, soonest_expire_at, package_count, read_at}``。
        读不到时 `credits` 为 None（**不是 0**）：0 会让 `credit_floor` 把号踢出池，
        None 只是"这次没读到"。
        """
        target_realm = realm or getattr(account, "realm", "") or "cn"
        packages = self.billing.packages(account, target_realm)
        summary: Dict[str, Any]
        if packages:
            summary = summarize_credits(packages)
        else:
            # 包清单读不到时退回"只读总数"，至少让面板上的数字是新的。
            value = self.billing.credits(account, target_realm)
            summary = {
                "total": value,
                "packages": [],
                "package_count": 0,
                "expiring_soon": 0.0,
                "buckets": {},
                "soonest_expire_at": 0.0,
            }
            if value is None:
                return {"ok": False, "credits": None, "detail": "读不到余额",
                        "buckets": {}, "expiring_soon": 0.0, "package_count": 0}

        value = summary.get("total")
        self.store.update_credit(account, value, save=False)
        account.credit_buckets = {
            "buckets": summary.get("buckets") or {},
            "expiring_soon": summary.get("expiring_soon") or 0.0,
            "soonest_expire_at": summary.get("soonest_expire_at") or 0.0,
            "package_count": summary.get("package_count") or 0,
            "total": value,
            "read_at": util.now(),
        }
        if hasattr(self.store, "save_accounts"):
            try:
                self.store.save_accounts()
            except OSError:
                log.debug("保存余额失败", exc_info=True)
        self.credit_book.record(
            getattr(account, "uid", "") or "", target_realm, summary, note=note
        )
        return {
            "ok": value is not None,
            "credits": value,
            "expiring_soon": summary.get("expiring_soon") or 0.0,
            "buckets": summary.get("buckets") or {},
            "labels": summary.get("labels") or {},
            "soonest_expire_at": summary.get("soonest_expire_at") or 0.0,
            "package_count": summary.get("package_count") or 0,
            "packages": summary.get("packages") or [],
        }

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
