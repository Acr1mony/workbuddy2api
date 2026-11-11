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

import os
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from . import identity, util
from .log import get_logger

log = get_logger("models")

CACHE_TTL = 3600.0
NEGATIVE_TTL = 300.0
# 磁盘缓存的文件名模板：进程重启后不必再打一次上游。这份目录是"上游会怎么想"
# 的一部分，少打一次少一次暴露面。
DISK_TEMPLATE = "catalog-{realm}.json"

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

# "能不能看图"在不同地方有四种说法：标签、能力表、布尔字段、模态列表。
# 只认明确的说法——把不能看图的模型报成能看图，客户端就会塞图片进来，
# 然后上游回 400 11135，我们还查不出来是谁的错。
VISION_TAGS = {"vision", "visual", "image-input", "image_input", "multimodal", "vlm"}
VISION_BOOL_FIELDS = ("supports_vision", "supportsVision", "vision", "is_vision")
VISION_CAP_KEYS = ("vision", "image_input", "imageInput", "image")
MODALITY_FIELDS = ("input_modalities", "inputModalities", "modalities", "inputTypes")

# 上游目录里从来没有"能不能看图"这个字段（实测 66 个条目，标签只有 craft /
# text-to-image 这类），但模型名本身很诚实：`glm-4.6v`、`qwen-vl`、`xxx-vision`、
# `xxx-omni`。所以留一条**可关掉**的名字启发式：只在四个地方都没说时兜底，
# 并且在 capabilities.vision_source 里写明结论是猜的（`id`）还是上游说的（`upstream`）。
# 边界里带数字是因为 `glm-4.6v` 的 v 前面是版本号里的 6，不是连字符。
VISION_ID_RE = re.compile(r"(?:^|[-_/0-9.])(?:v|vl|vision|omni|multimodal)$", re.IGNORECASE)

# OpenAI 的 /v1/models 每条都带 created（unix 秒）。给一个固定值而不是 0：
# 部分客户端会把它当"模型上线时间"排序，0 会让它们排在最旧或直接判非法。
CREATED_AT = 1735689600  # 2025-01-01T00:00:00Z


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

    __slots__ = ("id", "name", "realm", "max_output", "rate", "tags", "reasoning", "raw",
                 "context_length", "alias_of", "vision", "modalities", "vision_source")

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
        context_length: int = 0,
        alias_of: str = "",
        vision: Optional[bool] = None,
        modalities: Optional[List[str]] = None,
        vision_source: str = "",
    ) -> None:
        self.id = id
        self.name = name or id
        self.realm = realm
        self.max_output = max_output
        self.rate = rate
        self.tags = list(tags or [])
        self.reasoning = dict(reasoning or {})
        self.raw = raw or {}
        self.context_length = context_length
        self.alias_of = alias_of
        self.vision = vision
        self.modalities = list(modalities or [])
        self.vision_source = vision_source

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
        payload: Dict[str, Any] = {
            "id": self.id,
            "object": "model",
            "created": CREATED_AT,
            "owned_by": "workbuddy",
            "name": self.name,
        }
        if self.max_output:
            # 只报上游真给了的数：瞎填一个 context_length 会让客户端算出错误的截断策略。
            payload["max_output_tokens"] = self.max_output
        if self.context_length:
            # 来源只有三个：上游自己说的、``models.context_lengths`` 里配的、别名继承的。
            payload["context_length"] = self.context_length
            payload["context_window"] = self.context_length
        if self.alias_of:
            payload["alias_of"] = self.alias_of
        caps = self.capabilities()
        if caps:
            payload["capabilities"] = caps
            # 这两套拼写客户端各认一半：老客户端读 supports_vision，
            # 新客户端读 capabilities.vision。都带上下不冲突。
            if "vision" in caps:
                payload["supports_vision"] = caps["vision"]
        return payload

    def capabilities(self) -> Dict[str, Any]:
        """能力表：只写我们真能断定的事。

        聊天/流式/工具这三项是网关自身的能力（我们三种请求都原样转发），
        视觉和推理是模型自身的属性，上游没说就不写——留空比写错强。
        """
        caps: Dict[str, Any] = {"chat": True, "stream": True, "tools": True}
        if self.reasoning:
            caps["reasoning"] = True
        if self.vision is not None:
            caps["vision"] = bool(self.vision)
            if self.vision_source:
                # 让调用方分得清"上游说的"和"按名字猜的"：猜错的时候好排查。
                caps["vision_source"] = self.vision_source
        if self.modalities:
            caps["input_modalities"] = list(self.modalities)
        return caps

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


def _modalities_of(entry: Dict[str, Any]) -> List[str]:
    """上游的"输入模态"列表，归一到 ``text`` / ``image`` / ``audio`` / ``video``。"""
    raw: Any = None
    for key in MODALITY_FIELDS:
        if key in entry:
            raw = entry.get(key)
            break
    if isinstance(raw, str):
        parts: List[str] = [part for part in raw.split(",")]
    elif isinstance(raw, list):
        parts = [str(item) for item in raw]
    else:
        return []
    out: List[str] = []
    for part in parts:
        word = part.strip().lower()
        if not word:
            continue
        if word in ("image", "images", "vision", "image_url", "image-input", "image_input"):
            word = "image"
        elif word in ("text", "chat", "prompt"):
            word = "text"
        elif word in ("audio", "voice"):
            word = "audio"
        elif word in ("video", "videos"):
            word = "video"
        if word not in out:
            out.append(word)
    return out


def _vision_of(entry: Dict[str, Any], tags: List[str], modalities: List[str]) -> Optional[bool]:
    """能不能看图：明确说了才返回 True/False，认不出来返回 ``None``。"""
    caps = entry.get("capabilities") or entry.get("abilities")
    if isinstance(caps, list):
        caps = {str(item).lower(): True for item in caps}
    if isinstance(caps, dict):
        for key in VISION_CAP_KEYS:
            value = caps.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("true", "yes", "1"):
                return True
    for key in VISION_BOOL_FIELDS:
        value = entry.get(key)
        if isinstance(value, bool):
            return value
    if "image" in modalities:
        return True
    if {tag.lower() for tag in tags} & VISION_TAGS:
        return True
    return None


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
    tags = _tags_of(entry)
    modalities = _modalities_of(entry)
    vision = _vision_of(entry, tags, modalities)
    return ModelInfo(
        id=model_id,
        name=str(entry.get("name") or entry.get("display_name") or model_id),
        realm=realm,
        max_output=_as_int(entry.get("maxOutputTokens") or entry.get("max_output_tokens") or entry.get("max_tokens")),
        rate=parse_rate(entry.get("credits") if "credits" in entry else entry.get("rate")),
        tags=tags,
        reasoning=_reasoning_of(entry),
        raw=entry if isinstance(entry, dict) else {},
        vision=vision,
        modalities=modalities,
        vision_source="upstream" if vision is not None else "",
    )


def _to_dict(info: ModelInfo) -> Dict[str, Any]:
    """落盘用的形状：只留会被读的字段（``raw`` 是上游原文，缓存里不存）。"""
    payload: Dict[str, Any] = {
        "id": info.id,
        "name": info.name,
        "max_output": info.max_output,
        "tags": list(info.tags),
    }
    if info.rate is not None:
        payload["rate"] = info.rate
    if info.reasoning:
        payload["reasoning"] = dict(info.reasoning)
    if info.vision is not None:
        payload["vision"] = bool(info.vision)
        if info.vision_source:
            payload["vision_source"] = info.vision_source
    if info.modalities:
        payload["modalities"] = list(info.modalities)
    return payload


def _from_dict(item: Dict[str, Any], realm: str) -> ModelInfo:
    rate = item.get("rate")
    reasoning = item.get("reasoning")
    vision = item.get("vision")
    return ModelInfo(
        id=str(item.get("id") or ""),
        name=str(item.get("name") or ""),
        realm=realm,
        max_output=_as_int(item.get("max_output")),
        rate=float(rate) if isinstance(rate, (int, float)) else None,
        tags=[str(tag) for tag in item.get("tags") or []],
        reasoning=dict(reasoning) if isinstance(reasoning, dict) else {},
        vision=vision if isinstance(vision, bool) else None,
        modalities=[str(part) for part in item.get("modalities") or []],
        vision_source=str(item.get("vision_source") or ""),
    )


class ModelCatalog:
    """按域缓存模型清单。并发读走锁；失败走负缓存，别拿上游当靶子打。"""

    def __init__(self, client: Any, cfg: Any) -> None:
        self.client = client
        self.cfg = cfg
        self._lock = threading.RLock()
        self._entries: Dict[str, List[ModelInfo]] = {}
        self._fetched_at: Dict[str, float] = {}
        self._attempted_at: Dict[str, float] = {}
        self._negative_until: Dict[str, float] = {}
        # force 刷新之间的最小间隔：面板连点、多进程冷启动都会变成一串 /v3/config。
        try:
            self.min_refresh = float(cfg.get("models.min_refresh_seconds", 30) or 0)
        except (TypeError, ValueError):
            self.min_refresh = 30.0
        # 只有配置里真给了 data_dir 才用磁盘缓存：测试与嵌入式用法拿到的是
        # 默认的 ""，不该去仓库根目录里翻别人的清单。
        self._data_dir = str(cfg.get("data_dir") or "")
        self._loaded = set()
        self._refresh_meta()
        self._load_disk()

    def _refresh_meta(self) -> None:
        """从配置里读别名表与上下文长度表（面板改完配置会重新调一次）。"""
        aliases = self.cfg.get("models.aliases", {}) or {}
        if not isinstance(aliases, dict):
            aliases = {}
        # 别名一律小写比较：客户端写 GPT-4o 还是 gpt-4o 都该通。
        self.aliases = {str(k).strip().lower(): str(v).strip()
                        for k, v in aliases.items() if str(k).strip() and str(v).strip()}
        contexts = self.cfg.get("models.context_lengths", {}) or {}
        if not isinstance(contexts, dict):
            contexts = {}
        self.context_lengths = {}
        for key, value in contexts.items():
            try:
                tokens = int(value)
            except (TypeError, ValueError):
                continue
            if tokens > 0:
                self.context_lengths[str(key).strip()] = tokens
        visions = self.cfg.get("models.vision", {}) or {}
        # 配置只用来"补上游没说"的情况，也允许把上游说错的改回来：
        # 显式的 true/false 都算数，写别的（或留空）就当没配。
        self.vision_overrides: Dict[str, bool] = {}
        if isinstance(visions, dict):
            for key, value in visions.items():
                name = str(key).strip()
                if not name:
                    continue
                if isinstance(value, bool):
                    self.vision_overrides[name] = value
                elif isinstance(value, str) and value.strip().lower() in ("true", "yes", "1"):
                    self.vision_overrides[name] = True
                elif isinstance(value, str) and value.strip().lower() in ("false", "no", "0"):
                    self.vision_overrides[name] = False
        # 配置改过（面板保存）就要让已经加载的清单重新套一遍覆盖值，
        # 否则改完"能不能看图"得重启才生效。
        if hasattr(self, "_loaded"):
            self._loaded.clear()
        self.vision_heuristics = bool(self.cfg.get("models.vision_heuristics", True))

    def resolve_alias(self, model: str) -> str:
        """把客户端写的别名换成上游真名；没有别名就原样返回。"""
        return self.aliases.get(str(model or "").strip().lower(), "")

    def _apply_context(self, entries: List[ModelInfo]) -> None:
        for info in entries:
            if not info.context_length:
                info.context_length = self.context_lengths.get(info.id, 0)
            if info.id in self.vision_overrides:
                info.vision = self.vision_overrides[info.id]
                # 人说了算，但也要写清楚这个结论是人配的。
                info.vision_source = "config"
            elif info.vision is None and self.vision_heuristics and VISION_ID_RE.search(info.id or ""):
                info.vision = True
                info.vision_source = "id"
            if info.vision is True and "image" not in info.modalities:
                info.modalities = ["text", "image"] + [part for part in info.modalities
                                                       if part not in ("text", "image")]

    # -- 磁盘缓存 -----------------------------------------------------------
    def _cache_path(self, realm: str) -> str:
        return os.path.join(self._data_dir, DISK_TEMPLATE.format(realm=realm))

    def _load_disk(self) -> None:
        """进程启动时捡回上次的清单：重启不该等于"再打一次上游"。"""
        if not self._data_dir:
            return
        for realm in ("cn", "global"):
            path = self._cache_path(realm)
            payload = util.read_json(path, None)
            if not isinstance(payload, dict):
                continue
            fetched_at = float(payload.get("fetched_at") or 0)
            if time.time() - fetched_at >= CACHE_TTL:
                continue
            entries = [_from_dict(item, realm) for item in payload.get("models") or [] if isinstance(item, dict)]
            entries = [item for item in entries if item.id]
            if not entries:
                continue
            self._entries[realm] = entries
            self._fetched_at[realm] = fetched_at
            log.info("%s: 从磁盘缓存恢复 %d 个模型", realm, len(entries))

    def _save_disk(self, realm: str, entries: List[ModelInfo], fetched_at: float) -> None:
        if not self._data_dir:
            return
        payload = {
            "realm": realm,
            "fetched_at": fetched_at,
            "models": [_to_dict(item) for item in entries],
        }
        try:
            util.atomic_write_json(self._cache_path(realm), payload)
        except OSError as exc:  # 缓存写不了不该影响请求
            log.debug("模型目录落盘失败: %s", exc)

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
            else:
                # force 也不许把它当按钮连打：面板连点、多个进程同时冷启动都会变成
                # 一串 /v3/config 请求，这是"同一机器 id 高频拉目录"的标准形状。
                last_try = self._attempted_at.get(realm, 0)
                if now - last_try < self.min_refresh:
                    return self._entries.get(realm, [])
            self._attempted_at[realm] = now
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
        if entries:
            self._save_disk(realm, entries, now)
        return entries

    # -- 查询 ---------------------------------------------------------------
    def entries(self, realm: str = "cn", account: Any = None, chat_only: bool = True) -> List[ModelInfo]:
        realm = identity.normalize_realm(realm)
        with self._lock:
            cached = self._entries.get(realm)
        if cached is None and account is not None:
            cached = self.fetch(account, realm)
        items = cached or []
        if realm not in self._loaded:
            # 上下文长度来自配置，配置可能在模型清单加载之后才改，所以每次新清单
            # 进来都补一次（ModelInfo 上只补空的，不覆盖上游自己给的数）。
            self._apply_context(items)
            self._loaded.add(realm)
        return [info for info in items if info.chat_capable] if chat_only else list(items)

    def get(self, model: str, realm: str = "cn") -> Optional[ModelInfo]:
        wanted = self.resolve_alias(model) or model
        for info in self.entries(realm, chat_only=False):
            if info.id == wanted:
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

    def openai_list(self, realm: str = "cn", allowed: Any = None) -> Dict[str, Any]:
        """列出可用模型；`allowed` 是可选的 `(model_id) -> bool` 过滤器。

        不过滤会让客户端看见一堆自己调不动的模型：拿到 400 model_not_allowed 之后
        还得人工回来改配置。按 key 的白名单过滤，"看见的"就等于"能用的"。

        别名也要列出来（`alias_of` 指回真名）：客户端只有看见 `gpt-4o` 才会去用它，
        否则别名等于没配。
        """
        entries = self.entries(realm)
        data = [
            info.as_openai()
            for info in entries
            if not self.hidden(info.id) and (allowed is None or allowed(info.id))
        ]
        known = {info.id: info for info in entries}
        for alias in sorted(self.aliases):
            target = self.aliases[alias]
            info = known.get(target)
            if info is None or self.hidden(alias):
                continue
            if allowed is not None and not (allowed(alias) or allowed(target)):
                continue
            item = info.as_openai()
            item["id"] = alias
            item["name"] = alias
            item["alias_of"] = target
            data.append(item)
        return {"object": "list", "data": data}

    def names(self, realm: str = "cn") -> List[str]:
        return [info.id for info in self.entries(realm) if not self.hidden(info.id)]

    def invalidate(self, realm: str = "") -> None:
        realms = [identity.normalize_realm(realm)] if realm else ["cn", "global"]
        with self._lock:
            for item in realms:
                self._entries.pop(item, None)
                self._fetched_at.pop(item, None)
                self._negative_until.pop(item, None)
        for item in realms:  # 内存清了磁盘也得清，否则下次启动又捡回旧清单
            if not self._data_dir:
                continue
            try:
                os.remove(self._cache_path(item))
            except OSError:
                pass

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
