"""账号体检：用一次最小真请求确认这个号还能不能打。

为什么不能只看模型目录：目录接口对过期令牌也会返回（还可能命中缓存），
"目录能拉"和"能聊天"是两件事。这里的 probe 是只读的——它**不写冷却**，
免得一次网络抖动就把好号关掉。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Tuple

from . import identity
from .adapters import chat as chat_adapter
from .log import get_logger

log = get_logger("probe")

PROBE_PROMPT = "ping"
PROBE_MAX_TOKENS = 16
PROBE_TIMEOUT = 30.0
CHAT_PATH = "/v2/chat/completions"


def probe_account(service: Any, account: Any, model: str = "") -> Tuple[bool, Dict[str, Any]]:
    """发一次 ``max_tokens=16`` 的 chat，返回 ``(ok, 细节)``。

    细节里带 ``model`` / ``sample``（回包片段）或 ``error``，面板和 CLI 都直接展示它。
    """
    realm = identity.normalize_realm(getattr(account, "realm", "cn"))
    try:
        service.auth.ensure_token(account)
        if not model:
            names = service.catalog.names(realm) or service.catalog.names("cn")
            model = names[0] if names else "deepseek-v3"
        body = {
            "model": model,
            "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "stream": False,
            "max_tokens": PROBE_MAX_TOKENS,
        }
        prepared = chat_adapter.prepare_request(body, service.catalog.meta_map(realm))
        payload = json.dumps(prepared, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        meta = identity.ChatMeta.for_request(body)
        headers = identity.chat_headers(account, service.cfg, meta=meta, client_ip="127.0.0.1")
        url = service.client.base_url(realm) + CHAT_PATH
        response = service.client.post(url, headers, raw_body=payload, proxy=getattr(account, "proxy", "") or "")
        try:
            first = ""
            for line in response.iter_text(PROBE_TIMEOUT):
                if line.startswith("data:"):
                    first = line[5:].strip()
                    break
        finally:
            response.close()
    except Exception as exc:  # 体检失败是正常结果，不是异常
        return False, {"model": model, "error": str(exc)[:300]}

    if not first or first == "[DONE]":
        return False, {"model": model, "error": "上游没有返回任何数据"}
    try:
        parsed = json.loads(first)
    except ValueError:
        # 拿到了非 JSON 的流，说明连接确实活着（至少不是被拦）
        return True, {"model": model, "sample": first[:200]}

    if isinstance(parsed, dict) and "error" in parsed:
        error = parsed.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        return False, {"model": model, "error": str(message or error)[:300]}
    return True, {"model": model, "sample": json.dumps(parsed, ensure_ascii=False)[:200]}
