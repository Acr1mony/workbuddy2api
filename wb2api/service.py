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
from .errors import (
    CODE_EFFORT_UNSUPPORTED,
    CODE_FINGERPRINT,
    CODE_MODEL_FORBIDDEN,
    CODE_MODEL_UNAVAILABLE,
    CODE_PROMPT_TOO_LONG,
    CODE_QUEUE_FULL,
    CODE_QUOTA,
    CODE_RESTRICTED,
    CODE_SESSION_DEAD,
    CODE_TOOL_PAIRING,
    CODE_UA_REJECTED,
    ServiceError,
    client_error,
    parse_reset_clock,
)
from .endpoint_models import ModelsEndpoint
from .endpoint_responses import ResponsesEndpoint
from .failover import FailoverMixin
from .relay import RelayMixin, _is_usage_frame, _mentions_model, _num, _tool_names, _usage_of

log = get_logger("service")

MAX_ROTATE = 3  # 再换 3 个号；加上第一次共 4 次尝试
BACKOFF_BASE = 0.5
BACKOFF_MAX = 8.0
CHAT_PATH = "/v2/chat/completions"
KEY_RATE_WINDOW = 60.0  # 每 key 速率窗口：滑动一分钟，不是固定分钟桶

# 上游业务码。判断"换不换号"全靠它，所以每个都单独命名，别写成裸数字。
_REQUEST_ID_OK = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def _request_id(request: Any) -> str:
    """沿用客户端自带的请求 id；没有或不合规才自己生成。

    复用是排障的关键：客户端（SDK、网关、你自己写的脚本）报障时给的是**它**的
    id，我们如果另发一个，日志和台账就对不上号。但原样收下也有代价——它会进日志、
    台账、响应头，所以字符集和白名单一样收窄，长度封顶，不合规就当没给。
    """
    for name in ("X-Request-Id", "X-Correlation-Id", "Request-Id"):
        raw = str(request.header(name, "") or "").strip()
        if raw and _REQUEST_ID_OK.match(raw):
            return raw
    return "req_" + util.rand_hex(8)


class GatewayService(RelayMixin, FailoverMixin, ResponsesEndpoint, ModelsEndpoint):
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
        # 请求 id：有客户端自带的就沿用，没有才生成——报障时两边对得上号。
        request_id = _request_id(request)
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
                    stream=False, outcome="completed", usage=usage, status=200, ttft=None,
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
        request_id = _request_id(request)
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
                    stream=False, outcome="completed", usage=usage, status=200, ttft=None,
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
                    raise client_error(last_error, realm, model)
                blocked = self.pool.model_blocked(realm, model)
                if blocked:
                    # 池子是空的，但原因是"这个模型谁都跑不通"，不是"账号都在冷却"。
                    detail = blocked.get("reason") or "上游没有该模型的服务"
                    raise ServiceError(
                        f"模型 {model} 在上游不可用（{detail}），请换一个模型",
                        400, "model_unavailable", "invalid_request_error",
                    )
                # 池子知道最早什么时候会有号解禁：直接告诉客户端等多久，比让它在
                # "等一会儿再说"里反复撞墙强。没有可靠的等待时间就不编一个。
                wait = self.pool.earliest_ready(realm, model)
                hint = f"，最早约 {int(wait)} 秒后恢复" if wait and wait > 0 else ""
                raise ServiceError(
                    f"{realm} 域没有可用账号（都被冷却或未启用）{hint}",
                    503, "no_account", "api_error",
                    retry_after=min(float(wait), 3600.0) if wait and wait > 0 else 0.0,
                )
            exclude.append(account.key)

            url = ""
            proxy = ""
            try:
                self._ensure_ready(account, realm)
                self._pace(account)
                url = self.client.base_url(realm) + CHAT_PATH
                headers = identity.chat_headers(
                    account, self.cfg, meta=meta,
                    client_ip=str(getattr(request, "client_ip", "") or ""),
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
                    raise client_error(err, realm, model) from err
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
        raise client_error(error, realm, model) if error is not None else ServiceError(
            "上游持续失败", 503, "upstream_unavailable", "api_error"
        )

    # -- 流式转发 -----------------------------------------------------------
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


