"""额度、签到、用量上报——那些"顺手做一下"的上游调用。

这些接口的返回结构上游改过好几版，两个参考仓库各自只认一种形状，所以这里统一
用**宽容解析**：在一层层的 dict/list 里按字段名找，找到就取，找不到就返回 ``None``
并且不写坏数据。宁可面板上显示"未知"，也不要显示一个假的 12 万额度。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable, Optional, Tuple

from . import identity
from .log import get_logger
from .upstream import UpstreamClient, UpstreamError

log = get_logger("billing")

RESOURCE_PATH = "/v2/billing/meter/get-user-resource"
DAILY_PATH = "/v2/billing/meter/daily"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
USAGE_PATH = "/v2/billing/meter/get-enterprise-user-usage"
REPORT_PATH = "/v2/report"

# 额度字段的候选名，按"越具体越优先"排列。
CREDIT_KEYS = (
    "remaining",
    "remain",
    "balance",
    "credits",
    "credit",
    "available",
    "total",
    "amount",
    "quota",
)


# 上报里带的模型：默认用一个真实存在的聊天模型。上游只把它当"用户在用什么"的
# 统计维度，不校验它与账号权限（企业号除外，那种账号的上报零收益，调用方跳过）。
REPORT_MODEL_ID = "deepseek-v4-flash"
REPORT_MODEL_NAME = "DeepSeek V4 Flash"


class Billing:
    def __init__(self, client: UpstreamClient, cfg: Any) -> None:
        self.client = client
        self.cfg = cfg

    # -- 单次请求 -----------------------------------------------------------
    def _call(self, account: Any, realm: str, method: str, path: str, body: Any = None) -> Dict[str, Any]:
        # body 允许是 list：活跃上报的契约就是一个数组（一条上报一条事件）。
        url = self.client.base_url(realm) + path
        headers = identity.billing_headers(account, self.cfg)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
        response = self.client.request(method, url, headers, raw_body=raw, proxy=str(getattr(account, "proxy", "") or ""))
        try:
            return response.json()
        finally:
            response.close()

    # -- 对外 ---------------------------------------------------------------
    def credits(self, account: Any, realm: str) -> Optional[float]:
        """读账号余额；读不到返回 None（不代表 0）。

        注意是 **POST**：真实上游对 GET 直接回 404，只有 POST 带空 body 才返回
        腾讯计费侧的 `data.Response.Data.Accounts[]`（每个资源包一行，
        余额在 `CapacityRemainPrecise`）。参考仓库写的是 GET，照着抄会永远读不到。
        """
        try:
            payload = self._call(account, realm, "POST", RESOURCE_PATH, {})
        except (UpstreamError, ValueError) as exc:
            # ValueError：上游 200 但回了 HTML/空体（网关错误页），json() 解不出来。
            # 这一样是"读不到"，返回 None，别让调度循环整个炸掉。
            log.debug("读取额度失败 %s: %s", getattr(account, "label", "?"), exc)
            return None
        value = _pack_credits(payload)
        if value is None:
            value = _dig_credit(payload)
        if value is None:
            log.debug("额度响应里没有可识别的字段: %s", _preview(payload))
        return value

    def checkin(self, account: Any, realm: str) -> Tuple[bool, str]:
        """每日签到。CN 域才有；企业号会被拒（10001），那是正常的。"""
        try:
            payload = self._call(account, realm, "POST", CHECKIN_PATH, {})
        except (UpstreamError, ValueError) as exc:
            return False, str(exc)
        code = payload.get("code")
        message = str(payload.get("msg") or payload.get("message") or "")
        if code in (0, "0", None):
            return True, message or "已签到"
        return False, f"{code}: {message}"

    def usage_breakdown(self, account: Any, realm: str) -> Optional[Dict[str, Any]]:
        try:
            return self._call(account, realm, "GET", USAGE_PATH)
        except (UpstreamError, ValueError) as exc:
            log.debug("读取用量失败: %s", exc)
            return None

    def report(self, account: Any, realm: str, payload: Any = None,
               *, conversation_id: str = "", request_id: str = "",
               model_id: str = REPORT_MODEL_ID, model_name: str = REPORT_MODEL_NAME) -> bool:
        """活跃上报：点亮连登天数 / 解锁 first_buddy 任务。

        上游要的是**数组**（一条上报一条 `chat_request_send` 事件），且事件里
        `userId` 必填——缺了它接口照样 200，但连登天数不动（静默丢弃）。
        `conversationId` 不需要真实存在，服务端不校验一致性。

        失败无关紧要，绝不因为它影响任何请求。
        """
        if payload is None:
            payload = [chat_request_event(
                account,
                conversation_id=conversation_id,
                request_id=request_id,
                model_id=model_id,
                model_name=model_name,
            )]
        try:
            self._call(account, realm, "POST", REPORT_PATH, payload)
            return True
        except (UpstreamError, ValueError) as exc:
            log.debug("上报失败: %s", exc)
            return False


def chat_request_event(account: Any, *, conversation_id: str = "", request_id: str = "",
                       model_id: str = REPORT_MODEL_ID,
                       model_name: str = REPORT_MODEL_NAME) -> Dict[str, Any]:
    """构造一条 `chat_request_send` 事件。

    字段按上游客户端抓包形状给全（缺字段不会报错，但补全更像真客户端）；
    `timestamp`/`presentAt` 用毫秒。
    """
    now_ms = int(time.time() * 1000)
    uid = str(getattr(account, "uid", "") or "")
    conversation_id = conversation_id or f"wb2api-{now_ms}"
    request_id = request_id or conversation_id
    return {
        "eventCode": "chat_request_send",
        "timestamp": now_ms,
        "reportDelay": 0,
        "mode": "craft",
        "conversationId": conversation_id,
        "requestId": request_id,
        "inputLength": 12,
        "requestModelId": model_id,
        "requestModelName": model_name,
        "isPlan": False,
        "isAutoExecuteTerminal": False,
        "isAutoModify": False,
        "codebaseEnable": False,
        "maxToken": 0,
        "maxSteps": 0,
        "temperature": 0,
        "maxRetries": 0,
        "mentionContexts": [],
        "knowledgeId": [],
        "knowledgeName": [],
        "codebaseId": "",
        "mentionContextCount": 0,
        "command": "",
        "expertId": "",
        "recommendId": "",
        "skillId": "",
        "skillCount": 0,
        "totalCount": 0,
        "fileUri": "",
        "presentAt": now_ms,
        "traceId": "",
        "rootRequestId": request_id,
        "parentConversationId": conversation_id,
        "agentName": "default",
        "agentType": "conversation",
        "userId": uid,
    }


def _walk(node: Any, depth: int = 0) -> Iterable[Any]:
    if depth > 6:
        return
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value, depth + 1)


def _dig_credit(payload: Any) -> Optional[float]:
    """在响应里找数值型额度。字段名优先，其次看 ``data`` 层。"""
    if not isinstance(payload, (dict, list)):
        return None
    candidates = []
    for node in _walk(payload):
        if not isinstance(node, dict):
            continue
        for key in CREDIT_KEYS:
            if key not in node:
                continue
            value = _as_number(node[key])
            if value is not None:
                candidates.append((CREDIT_KEYS.index(key), value))
    if not candidates:
        # 有些版本直接给一个裸数字——但只在 data 信封里面找：整个 payload 里乱扫会把
        # code=0 读成"余额 0"（面板显示假数据、pool 的 credit_floor 还会把账号踢掉），
        # 把 code=11100 读成"余额 11100"。没有 data 层就老实返回 None。
        scope = payload.get("data") if isinstance(payload, dict) else None
        if scope is None:
            return None
        for node in _walk(scope):
            value = _as_number(node)
            if value is not None:
                return value
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _pack_credits(payload: Any) -> Optional[float]:
    """腾讯计费侧的资源包形状：``data.Response.Data.Accounts[]`` 每行一个包。

    余额要**求和**：一个人的额度常常拆成"赠送包 + 每日签到包 + 套餐包"，
    只取第一个包会少报一大截（真实账号上就是 100 + 5 拆成两行）。
    """
    node = payload
    for key in ("data", "Response", "Data"):
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return None
    accounts = node.get("Accounts") if isinstance(node, dict) else None
    if not isinstance(accounts, list):
        return None
    total = 0.0
    found = False
    for item in accounts:
        if not isinstance(item, dict):
            continue
        for key in ("CapacityRemainPrecise", "CycleCapacityRemainPrecise", "CapacityRemain", "CycleCapacityRemain"):
            if key not in item:
                continue
            value = _as_number(item[key])
            if value is None:
                continue
            # 只算还在有效期内的包：过期包留着 Remain 但不该算进可用额度。
            if str(item.get("ExpiredTime") or "") and item.get("Status") not in (0, "0", None):
                continue
            total += value
            found = True
            break
    return round(total, 6) if found else None


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    if isinstance(value, dict):
        for key in ("value", "amount", "count", "remaining"):
            if key in value:
                return _as_number(value[key])
    return None


def _preview(payload: Any, limit: int = 200) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(payload)
    return text[:limit]
