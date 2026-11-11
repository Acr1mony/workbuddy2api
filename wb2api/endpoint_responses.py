"""``POST /v1/responses`` 的编排。

放在单独的 mixin 里而不是塞进 ``service.py``：那边已经 699 行了，再进来一个入口就
越过仓库自己定的 700 行约定。编排（选号、重试、记账、槽位释放）与 chat / messages
完全一致，只有两侧的翻译不同——翻译在 ``adapters/responses.py``。
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import identity, util
from .adapters import chat as chat_adapter
from .adapters import responses as resp_adapter
from .errors import ServiceError
from .log import get_logger
from .relay import _tool_names
from .sticky import session_key as make_session_key

log = get_logger("responses")


class ResponsesEndpoint:
    """Responses API 入口，混入 :class:`GatewayService`。"""

    def responses(self, request: Any) -> Any:
        """处理一次 ``POST /v1/responses``（OpenAI Responses 形状）。"""
        from .httpserver import json_response, stream_response
        # 延后导入：service 在 import 本模块时还没跑完，顶层 import 会成环。
        from .service import _request_id

        started = time.time()
        token = request.bearer() or request.arg("api_key")
        key = self.authenticate(token)

        body = request.json_body()
        if not isinstance(body, dict):
            raise ServiceError("请求体必须是 JSON 对象", 400, "invalid_request")

        chat_body = resp_adapter.to_chat(body)
        if not chat_body.get("messages"):
            raise ServiceError("input 不能为空", 400, "invalid_request")

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
        response_id = "resp_" + identity.new_message_id()[:24]
        req_extra = {
            "path": "/v1/responses",
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
            stream = resp_adapter.ResponseStream(model, response_id=response_id, created_at=int(started))
            return stream_response(
                self._relay_responses(
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
                    request_id=request_id, attempts=attempts, alias=alias, kind="responses",
                    extra=req_extra,
                )
            except Exception:
                log.exception("记账失败（%s）", model)
            self.pool.note_success(account, model=model, session_key=stick)
            return json_response(
                resp_adapter.from_chat(aggregated, model, response_id=response_id),
                headers=extra_headers,
            )
        finally:
            response.close()
            self.pool.release(account)
            self._release_key(key_slot)
