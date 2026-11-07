"""额度、签到、用量上报——那些"顺手做一下"的上游调用。

这些接口的返回结构上游改过好几版，两个参考仓库各自只认一种形状，所以这里统一
用**宽容解析**：在一层层的 dict/list 里按字段名找，找到就取，找不到就返回 ``None``
并且不写坏数据。宁可面板上显示"未知"，也不要显示一个假的 12 万额度。
"""

from __future__ import annotations

import json
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


class Billing:
    def __init__(self, client: UpstreamClient, cfg: Any) -> None:
        self.client = client
        self.cfg = cfg

    # -- 单次请求 -----------------------------------------------------------
    def _call(self, account: Any, realm: str, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
        """读账号余额；读不到返回 None（不代表 0）。"""
        try:
            payload = self._call(account, realm, "GET", RESOURCE_PATH)
        except (UpstreamError, ValueError) as exc:
            # ValueError：上游 200 但回了 HTML/空体（网关错误页），json() 解不出来。
            # 这一样是"读不到"，返回 None，别让调度循环整个炸掉。
            log.debug("读取额度失败 %s: %s", getattr(account, "label", "?"), exc)
            return None
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

    def report(self, account: Any, realm: str, payload: Dict[str, Any]) -> bool:
        """活跃上报。失败无关紧要，绝不因为它影响请求。"""
        try:
            self._call(account, realm, "POST", REPORT_PATH, payload)
            return True
        except (UpstreamError, ValueError) as exc:
            log.debug("上报失败: %s", exc)
            return False


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
