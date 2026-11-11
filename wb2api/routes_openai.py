"""客户端面路由：OpenAI 兼容的四个端点。

只有四个，因为客户端只需要这些：

===============  ======================================================
POST /v1/chat/completions   主入口（流式/非流式）
GET  /v1/models             模型列表（从上游目录聚合，按 realm 过滤）
GET  /healthz               存活探针，**不需要鉴权**（反代挂了的告警靠它）
GET  /status                人看的摘要：池状态、余额、最近错误
===============  ======================================================

``/status`` 与 ``/healthz`` 的分工是刻意的：探针不该暴露账号数量或余额，
所以 ``/healthz`` 只回 ``{"ok": true}``。
"""

from __future__ import annotations

from typing import Any, Callable, Dict

from .httpserver import BadRequest, Response, Router, error_response, json_response
from .service import ServiceError
from .upstream import UpstreamError


def _error_headers(req: Any) -> Dict[str, str]:
    """错误响应也要能对上号：客户端报障时只会给你它看到的那个 id。

    成功路径在 `service` 里把 `request_id` 写进 `request.state`，这里读回来；
    还没走到那一步的（比如鉴权就失败了）自然就没有这个头。
    """
    request_id = ""
    try:
        request_id = str((getattr(req, "state", None) or {}).get("request_id") or "")
    except Exception:
        request_id = ""
    return {"X-Request-Id": request_id} if request_id else {}


def guard(handler: Callable[..., Any]) -> Callable[..., Any]:
    """把各层的异常统一翻译成 OpenAI 错误信封。

    没有这一层，``dispatch`` 里的兜底会把每个预料之中的失败都变成 500。
    """

    def wrapper(req: Any, **params: Any) -> Response:
        try:
            return handler(req, **params)
        except ServiceError as exc:
            headers = _error_headers(req)
            if getattr(exc, "retry_after", 0):
                # 带上 Retry-After，客户端会自己等；不带的话它们会立刻重试把墙撞穿
                headers["Retry-After"] = str(int(exc.retry_after))
            return error_response(exc.status, exc.message, exc.code, kind=exc.kind, headers=headers or None)
        except BadRequest as exc:
            return error_response(exc.status, exc.message, exc.code, kind="invalid_request_error",
                                  headers=_error_headers(req) or None)
        except UpstreamError as exc:
            return error_response(503, str(exc), "upstream_unavailable", kind="api_error",
                                  headers=_error_headers(req) or None)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as exc:  # 仍然兜底，但要留下堆栈
            from .log import get_logger

            get_logger("routes").exception("handler %s failed", getattr(handler, "__name__", handler))
            return error_response(500, f"internal error: {exc}", "internal_error", kind="api_error")

    return wrapper


def anthropic_guard(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Anthropic 的错误信封是 ``{"type":"error","error":{...}}``，与 OpenAI 不同。

    Claude Code 会按这个形状取 ``error.message``；给错形状它只显示 "unknown error"，
    用户拿不到任何可用信息。
    """

    def wrapper(req: Any, **params: Any) -> Response:
        try:
            return handler(req, **params)
        except ServiceError as exc:
            return _anthropic_error(exc.status, exc.message, _anthropic_kind(exc), exc.retry_after,
                                    req=req)
        except BadRequest as exc:
            return _anthropic_error(exc.status, exc.message, "invalid_request_error", req=req)
        except UpstreamError as exc:
            return _anthropic_error(503, str(exc), "api_error", req=req)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as exc:
            from .log import get_logger

            get_logger("routes").exception("handler %s failed", getattr(handler, "__name__", handler))
            return _anthropic_error(500, f"internal error: {exc}", "api_error", req=req)

    return wrapper


def _anthropic_kind(exc: ServiceError) -> str:
    mapping = {
        "invalid_request_error": "invalid_request_error",
        "rate_limit_error": "rate_limit_error",
        "authentication_error": "authentication_error",
    }
    return mapping.get(exc.kind, "api_error")


def _anthropic_error(status: int, message: str, kind: str, retry_after: float = 0.0, req: Any = None) -> Response:
    headers = _error_headers(req) if req is not None else {}
    if retry_after:
        headers["Retry-After"] = str(int(retry_after))
    return json_response(
        {"type": "error", "error": {"type": kind, "message": message}},
        status=status,
        headers=headers or None,
    )


def _count_tokens(req: Any) -> Response:
    from .adapters.messages import count_tokens

    body = req.json_body()
    return json_response({"input_tokens": count_tokens(body if isinstance(body, dict) else {})})


def register(router: Router, service: Any) -> None:
    router.add("POST", r"/v1/chat/completions", guard(lambda req: service.chat_completions(req)))
    # Anthropic 形状：Claude Code / Codex 只认这个入口。count_tokens 不需要打上游。
    router.add("POST", r"/v1/messages", anthropic_guard(lambda req: service.messages(req)))
    router.add("POST", r"/v1/messages/count_tokens", anthropic_guard(lambda req: _count_tokens(req)))
    # Responses 形状：Codex CLI 与新版 SDK 只认这个入口（OpenAI 的错误信封）。
    router.add("POST", r"/v1/responses", guard(lambda req: service.responses(req)))
    router.add("GET", r"/v1/models", guard(lambda req: service.models(req)))
    router.add("GET", r"/healthz", lambda req: json_response({"ok": True}))
    router.add("GET", r"/status", guard(lambda req: json_response(service.status(req))))
    # 有些客户端会先试 /v1/models/{id} 确认能力，回 404 比 500 友好。
    router.add(
        "GET",
        r"/v1/models/(?P<model>[^/]+)",
        guard(lambda req, model: _one_model(service, req, model)),
    )


def _one_model(service: Any, req: Any, model: str) -> Response:
    from .httpserver import error_response as err

    payload = service.models(req)
    listing = payload.body if isinstance(payload.body, bytes) else b""
    import json

    try:
        data = json.loads(listing.decode("utf-8")).get("data") or []
    except ValueError:
        data = []
    for item in data:
        if item.get("id") == model:
            return json_response(item)
    # 区分"不存在"和"没给你开"：后者是配置问题，说清楚比 404 省一轮排查。
    known = set(service.catalog.names("cn")) | set(service.catalog.names("global"))
    if model in known:
        return err(
            403,
            f"model {model} is not allowed for this API key",
            "model_not_allowed",
            kind="invalid_request_error",
        )
    return err(404, f"model {model} not found", "model_not_found", kind="invalid_request_error")
