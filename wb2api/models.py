"""模型目录：向上游要一次清单，然后记住它。

网关需要这份目录做三件事：

* ``GET /v1/models`` 得报得出可用的模型名；
* 每个模型是否要 ``thinking`` / ``reasoning_effort``（发错了就是 11150）；
* 按 credits 判断一个模型是免费还是收费（``credit_floor`` 要拿它做判断）。

三个来源的权威度不一样，按顺序试：``/v3/config`` 是客户端自己在用的那份；
``/console/enterprises/personal/models`` 是后台的 allowlist；``/v2/enterprises/personal/models``
是旧路径，只在前面都不通时兜底。

UA 和 ``X-Product`` 是硬要求：同一个接口换个 UA 就 400 ``12403``，而 ``/v3/config``
少发 ``X-Product: SaaS`` 会静默少返回模型（少 2 个，不报错——最难查的那种）。"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from . import identity
from .log import get_logger

log = get_logger("models")

CACHE_TTL = 3600.0
NEGATIVE_TTL = 300.0

CONFIG_PATH = "/v3/config"
CONSOLE_PATH = "/console/enterprises/personal/models"
LEGACY_PATH = "/v2/enterprises/personal/models"

# 不是聊天模型的东西：上游把它们和聊天模型放在同一个清单里。
SKIP_PREFIXES = ("nes-", "completion-", "codewise-")
SKIP_TAGS = {
    "text-to-image", "image-to-image", "text-to-video", "image-to-video",
    "image", "embedding", "rerank",
}
MIN_OUTPUT_TOKENS = 256


def parse_rate(value: Any) -> Optional[float]:
    """把 ``credits`` 字段解析成"每 1k token 多少积分"。

    取值形态实测有三种：``"x0.11"``（倍率）、``"x0.34 credits"``（带单位）、
    ``"x0.00"``（免费）。解析不出来就是 ``None``——**未知不是免费**：
    把未知当免费会让 ``credit_floor`` 失效。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    text = text.replace("credits", "").replace("credit", "").strip()
    if text.startswith("x"):
        text = text[1:].strip()
    try:
        return float(text)
    except ValueError:
        return None


class ModelInfo:
    """一个模型的最小画像。"""

    __slots__ = ("id", "name", "realm", "max_output", "rate", "tags", "reasoning", "raw")

    def __init__(
        self,
        id: str,
        name: str = "",
        realm: str = "cn",
        max_output: int = 0,
        rate: Optional[float] = None,
        tags: Optional[List[str]] = None,
        reasoning: Optional[Dict[str, Any]] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.id = id
        self.name = name or id
        self.realm = realm
        self.max_output = max_output
        self.rate = rate
        self.tags = list(tags or [])
        self.reasoning = dict(reasoning or {})
        self.raw = raw or {}

    @property
    def free(self) -> bool:
        return self.rate is not None and self.rate <= 0

    @property
    def chat_capable(self) -> bool:
        """过滤掉图片/视频/向量这类共用同一清单的非聊天模型。"""
        lowered = self.id.lower()
        if any(lowered.startswith(prefix) for prefix in SKIP_PREFIXES):
            return False
        if self.max_output and self.max_output <= MIN_OUTPUT_TOKENS:
            return False
        tags = {tag.lower() for tag in self.tags}
        if tags & SKIP_TAGS:
            return False
        return True

    def meta(self) -> Dict[str, Any]:
        """给 ``adapters.chat`` 用的形状（字段名兼容新老两套）。"""
        meta: Dict[str, Any] = {"reasoning": bool(self.reasoning)}
        for key in ("defaultEffort", "supportedEfforts", "canDisableThinking"):
            if key in self.reasoning:
                meta[key] = self.reasoning[key]
        if self.rate is not None:
            meta["rate"] = self.rate
        return meta

    def as_openai(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "object": "model",
            "created": 0,
            "owned_by": "workbuddy",
            "name": self.name,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ModelInfo {self.id} realm={self.realm} rate={self.rate}>"


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _tags_of(entry: Dict[str, Any]) -> List[str]:
    raw = entry.get("tags") or entry.get("categories") or []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return [str(item) for item in raw if isinstance(item, (str, int))]
    return []


def _reasoning_of(entry: Dict[str, Any]) -> Dict[str, Any]:
    raw = entry.get("reasoning")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bool):
        return {"enabled": raw}
    # 老字段：支持了档位就说明是推理模型。
    if entry.get("reasoning_efforts") or entry.get("supportedEfforts"):
        return {
            "supportedEfforts": entry.get("supportedEfforts") or entry.get("reasoning_efforts"),
        }
    if entry.get("thinking") is True:
        return {"enabled": True}
    return {}


def parse_entries(payload: Any, realm: str = "cn") -> List[ModelInfo]:
    """从各种信封里挖出模型清单。

    见到过三种形状：``{"data":{"models":[...]}}``、``{"data":{"agents":[...]}}``，
    以及裸 list。认形状比认字段更稳，所以按"深挖到第一个像模型的数据"来写。
    """
    found: List[ModelInfo] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 4 or node is None:
            return
        if isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    model_id = item.get("id") or item.get("model") or item.get("name")
                    if isinstance(model_id, str) and model_id:
                        found.append(_build(item, model_id, realm))
                    else:
                        walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        agents = node.get("agents")
        if isinstance(agents, list):
            for agent in agents:
                if isinstance(agent, dict) and str(agent.get("name") or "").lower() == "cli":
                    walk(agent.get("models"), depth + 1)
            if found:
                return
        for key in ("models", "data", "list", "items", "config"):
            if key in node:
                walk(node[key], depth + 1)

    walk(payload)
    unique: Dict[str, ModelInfo] = {}
    for info in found:
        unique.setdefault(info.id, info)
    return list(unique.values())


def _build(entry: Dict[str, Any], model_id: str, realm: str) -> ModelInfo:
    return ModelInfo(
        id=model_id,
        name=str(entry.get("name") or entry.get("display_name") or model_id),
        realm=realm,
        max_output=_as_int(entry.get("maxOutputTokens") or entry.get("max_output_tokens") or entry.get("max_tokens")),
        rate=parse_rate(entry.get("credits") if "credits" in entry else entry.get("rate")),
        tags=_tags_of(entry),
        reasoning=_reasoning_of(entry),
        raw=entry if isinstance(entry, dict) else {},
    )


class ModelCatalog:
    """按域缓存模型清单。并发读走锁；失败走负缓存，别拿上游当靶子打。"""

    def __init__(self, client: Any, cfg: Any) -> None:
        self.client = client
        self.cfg = cfg
        self._lock = threading.RLock()
        self._entries: Dict[str, List[ModelInfo]] = {}
        self._fetched_at: Dict[str, float] = {}
        self._negative_until: Dict[str, float] = {}

    # -- 抓取 ---------------------------------------------------------------
    def _paths(self) -> Iterable[str]:
        for path in (CONFIG_PATH, CONSOLE_PATH, LEGACY_PATH):
            yield path

    def _headers(self, account: Any, path: str) -> Dict[str, str]:
        headers = identity.billing_headers(account, self.cfg)
        headers["Accept"] = "application/json"
        if path == CONFIG_PATH:
            # /v3/config 靠这两个头认"官方客户端"，缺了会静默少返回模型。
            headers["X-Product"] = "SaaS"
            headers["User-Agent"] = identity.user_agent(
                getattr(account, "realm", ""),
                str(self.cfg.get("upstream.client_version") or ""),
                str(self.cfg.get("upstream.cli_version") or ""),
            )
        return headers

    def fetch(self, account: Any, realm: str = "", force: bool = False) -> List[ModelInfo]:
        realm = identity.normalize_realm(realm or getattr(account, "realm", ""))
        now = time.time()
        with self._lock:
            if not force:
                cached = self._entries.get(realm)
                if cached is not None and now - self._fetched_at.get(realm, 0) < CACHE_TTL:
                    return cached
                if now < self._negative_until.get(realm, 0):
                    return self._entries.get(realm, [])
        entries: List[ModelInfo] = []
        for path in self._paths():
            url = self.client.base_url(realm) + path
            try:
                response = self.client.get(url, self._headers(account, path))
                try:
                    payload = response.json()
                finally:
                    response.close()
            except Exception as exc:  # 网络/鉴权/格式都可能炸，换下一个来源
                log.debug("模型目录 %s 取失败: %s", path, exc)
                continue
            entries = parse_entries(payload, realm)
            if entries:
                log.info("%s: 从 %s 拿到 %d 个模型", realm, path, len(entries))
                break
        with self._lock:
            if entries:
                self._entries[realm] = entries
                self._fetched_at[realm] = now
                self._negative_until.pop(realm, None)
            else:
                self._negative_until[realm] = now + NEGATIVE_TTL  # 别每 10 秒重试一次
                log.warning("%s: 三个来源都没拿到模型清单（%ds 内不再重试）", realm, int(NEGATIVE_TTL))
        return entries

    # -- 查询 ---------------------------------------------------------------
    def entries(self, realm: str = "cn", account: Any = None, chat_only: bool = True) -> List[ModelInfo]:
        realm = identity.normalize_realm(realm)
        with self._lock:
            cached = self._entries.get(realm)
        if cached is None and account is not None:
            cached = self.fetch(account, realm)
        items = cached or []
        return [info for info in items if info.chat_capable] if chat_only else list(items)

    def get(self, model: str, realm: str = "cn") -> Optional[ModelInfo]:
        for info in self.entries(realm, chat_only=False):
            if info.id == model:
                return info
        return None

    def meta_map(self, realm: str = "cn") -> Dict[str, Dict[str, Any]]:
        return {info.id: info.meta() for info in self.entries(realm, chat_only=False)}

    def rate(self, model: str, realm: str = "cn") -> Optional[float]:
        info = self.get(model, realm)
        if info is not None and info.rate is not None:
            return info.rate
        configured = self.cfg.get("pool.model_rates") or {}
        if isinstance(configured, dict):
            value = configured.get(model)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return None

    def hidden(self, model: str) -> bool:
        hidden = self.cfg.get("models.hidden") or []
        if not isinstance(hidden, list):
            return False
        for pattern in hidden:
            pattern = str(pattern)
            if pattern.endswith("*"):
                if model.startswith(pattern[:-1]):
                    return True
            elif pattern == model:
                return True
        return False

    def openai_list(self, realm: str = "cn") -> Dict[str, Any]:
        data = [info.as_openai() for info in self.entries(realm) if not self.hidden(info.id)]
        return {"object": "list", "data": data}

    def names(self, realm: str = "cn") -> List[str]:
        return [info.id for info in self.entries(realm) if not self.hidden(info.id)]

    def invalidate(self, realm: str = "") -> None:
        with self._lock:
            if realm:
                self._entries.pop(identity.normalize_realm(realm), None)
                self._fetched_at.pop(identity.normalize_realm(realm), None)
                self._negative_until.pop(identity.normalize_realm(realm), None)
            else:
                self._entries.clear()
                self._fetched_at.clear()
                self._negative_until.clear()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                realm: {
                    "count": len(entries),
                    "fetched_at": self._fetched_at.get(realm, 0),
                    "rate_known": sum(1 for info in entries if info.rate is not None),
                }
                for realm, entries in self._entries.items()
            }
