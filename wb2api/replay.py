"""失败报文回放：把存下来的请求体再发一次，并给出净化前后的对比。

为什么值得单独一个模块：11-128 那类问题（上游按**逐字**匹配拒绝）靠读错误码是猜
不出来的，唯一的办法是看"我们实际发出去的是什么"。而"再发一次"本身有风险——它
会真的花掉额度，所以这里默认：

* **预览免费**：只做本地翻译与净化，打一个字节都不发上游；
* **回放要显式触发**：一次不重试（不换号、不循环），结果与用量照实记账，
  这样面板上"我点了一下回放"和真实流量在台账里能分开。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from . import identity
from .adapters import chat as chat_adapter
from .errors import ServiceError
from .log import get_logger

log = get_logger("replay")

# 这些角色上的文字才做"整句中性化"，与 sanitize 的作用域保持一致
_TEXT_ROLES = ("system", "developer", "assistant")


def _load(service: Any, request_id: str) -> Dict[str, Any]:
    record = service.diag.get(request_id)
    if not record:
        raise ServiceError(f"没有这条报文：{request_id}", 404, "unknown_dump")
    return record


def _body_of(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw = record.get("request_body")
    if not isinstance(raw, str) or not raw.strip():
        return None
    if record.get("request_truncated"):
        # 截断过的体回放上去只会得到"另一个错误"，别让它冒充原请求
        raise ServiceError("这条报文里的请求体被截断过，回放没有意义", 400, "truncated_body")
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise ServiceError("报文里的请求体不是合法 JSON", 400, "invalid_stored_body")
    return parsed if isinstance(parsed, dict) else None


def _translate(service: Any, body: Dict[str, Any], realm: str, model: str, sanitize: bool) -> Dict[str, Any]:
    prepared = chat_adapter.prepare_request(
        body,
        service.catalog.meta_map(realm),
        sanitize_prompt=sanitize,
        append_prompt=str(service.cfg.get("prompt.append") or ""),
    )
    prepared["model"] = model
    return prepared


def _texts(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """取出发给上游的每段系统/助手文字，供前后对照。"""
    out: List[Dict[str, str]] = []
    for index, message in enumerate(payload.get("messages") or []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role not in _TEXT_ROLES:
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        else:
            continue
        if text.strip():
            out.append({"index": str(index), "role": role, "text": text})
    return out


def preview(service: Any, request_id: str, realm: str = "", model: str = "") -> Dict[str, Any]:
    """净化前后对照。**不打上游**。"""
    record = _load(service, request_id)
    body = _body_of(record)
    if body is None:
        raise ServiceError("这条报文没留下请求体（可能不是 11-128 那类失败）", 404, "no_request_body")
    realm = str(realm or record.get("realm") or "cn")
    model = str(model or record.get("model") or body.get("model") or "")

    raw_payload = _translate(service, body, realm, model, sanitize=False)
    washed = _translate(service, body, realm, model, sanitize=True)

    before = _texts(raw_payload)
    after = _texts(washed)
    changes = []
    for left, right in zip(before, after):
        if left["text"] != right["text"]:
            changes.append({
                "index": left["index"],
                "role": left["role"],
                "before": left["text"],
                "after": right["text"],
            })
    return {
        "request_id": request_id,
        "realm": realm,
        "model": model,
        "changed": bool(changes),
        "changes": changes,
        "prompt_mode": str(service.cfg.get("prompt.mode") or ""),
        "system_before": [item["text"] for item in before if item["role"] in ("system", "developer")],
        "system_after": [item["text"] for item in after if item["role"] in ("system", "developer")],
    }


def replay(
    service: Any,
    request_id: str,
    *,
    sanitize: Optional[bool] = None,
    realm: str = "",
    model: str = "",
    request: Any = None,
) -> Dict[str, Any]:
    """把报文里的请求体重发一次。不换号、不重试，结果原样返回给面板。"""
    record = _load(service, request_id)
    body = _body_of(record)
    if body is None:
        raise ServiceError("这条报文没留下请求体，没法回放", 404, "no_request_body")
    realm = str(realm or record.get("realm") or "cn")
    model = str(model or record.get("model") or body.get("model") or "")
    if sanitize is None:
        sanitize = str(service.cfg.get("prompt.mode") or "") == "sanitize"

    prepared = _translate(service, body, realm, model, sanitize)
    # 上游只认流式（非流式会被 400 打回），所以这里保持 stream=true，
    # 只在本地把帧聚合成完整回复——回放关心的是"上游答不答应"。
    prepared["stream"] = True
    payload = json.dumps(prepared, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    meta = identity.ChatMeta.for_request(body, "", session_hint="")
    started = time.time()
    try:
        account, response, attempts = service._open_upstream(
            realm=realm, model=model, key=None, stick="", payload=payload, meta=meta, request=request
        )
    except ServiceError as exc:
        return {
            "ok": False,
            "status": exc.status,
            "code": exc.code,
            "message": exc.message,
            "sanitize": bool(sanitize),
            "realm": realm,
            "model": model,
        }
    try:
        lines = list(response.iter_text(float(service.cfg.get("upstream.idle_timeout_seconds", 300))))
        aggregated = chat_adapter.aggregate(lines, "chatcmpl-replay", model)
        usage = aggregated.get("usage") or {}
        try:
            service._record(
                started=started, model=model, realm=realm, account=account, key=None,
                stream=False, outcome="completed", usage=usage, status=200, ttft=None,
                request_id="replay:" + request_id, attempts=attempts, kind="replay",
                extra={"path": "/api/replay", "replay_of": request_id},
            )
        except Exception:
            log.exception("回放记账失败（%s）", request_id)
        message = (aggregated.get("choices") or [{}])[0].get("message") or {}
        text = message.get("content") or ""
        if not text and message.get("reasoning_content"):
            # 预算小的回放常常"正文一个字都没有、token 全被思考吃掉"，
            # 这时候空字符串会让人以为回放没成功，所以把原因写出来。
            text = "（正文为空：%d 个 token 全花在思考上）" % len(message["reasoning_content"])
        return {
            "ok": True,
            "status": 200,
            "sanitize": bool(sanitize),
            "realm": realm,
            "model": model,
            "account": account.uid,
            "attempts": attempts,
            "usage": usage,
            "credit": usage.get("credit") or 0.0,
            "elapsed_ms": int((time.time() - started) * 1000),
            "sample": str(text)[:2000],
        }
    finally:
        response.close()
        service.pool.release(account)
