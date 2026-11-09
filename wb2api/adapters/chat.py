"""chat/completions 适配层：改写上游请求体、转发上游流、聚合非流式响应。

上游只认流式（``stream:true``），且比 OpenAI 规范挑剔得多，所以这里有一整套
"让它别报 400"的改写管线。每条改写都对应一次实测过的上游拒绝行为：

======================  ====================================================
上游行为                 改写
======================  ====================================================
拒绝非流式               ``stream=true`` 强制打开
只认 ``max_tokens``      ``max_completion_tokens`` 翻译过去
``tool_choice`` 只认字符串  对象形式压成 ``"auto"/"required"/"none"``
``image_url`` 只认对象     字符串/裸 URL 包成 ``{"url": ...}``
GPT 系 ``max_tokens<16``   抬到 16（否则 11133）
tool 配对不完整           合并相邻 tool_calls、重排 tool 结果、剔除孤儿
======================  ====================================================
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .. import markup, sse
from .. import sanitize as request_sanitize
from ..log import get_logger

log = get_logger("adapter.chat")

EFFORT_RANK = {"off": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}
GPT_MIN_MAX_TOKENS = 16
KNOWN_ROLES = {"system", "user", "assistant", "tool", "function"}
DEFAULT_REASONING_EFFORT = "high"
# 目录没预热时按名字判断是否推理模型；宁可多带字段也别少带（少带 = 没有思考输出）。
REASONING_MARKERS = ("deepseek", "reasoner", "thinking", "o1-", "o3-", "o4-", "claude-3-7", "claude-sonnet-4")
# 这些模型名字里没有思考字样，却对"缺 effort"敏感：不带 reasoning_effort 直接 11150。
# 实测 glm 系列就是这一类——它不需要 thinking 字段，但需要显式的思考档位。
EFFORT_ONLY_MARKERS = ("glm-",)

# 内容审核打回时用的中性系统提示词：只说"你是个助手"，不携带任何项目/身份线索。
NEUTRAL_SYSTEM_PROMPT = "You are a helpful assistant."

# 上游 usage 里缓存命中字段的别名，统一成 OpenAI 规范名。
_CACHE_PAIRS = (
    ("prompt_tokens_details", "cached_tokens"),
    ("", "prompt_cache_hit_tokens"),
    ("", "cache_read_input_tokens"),
    ("", "cached_tokens"),
    ("input_tokens_details", "cached_tokens"),
)


# --------------------------------------------------------------------------
# 请求体改写
# --------------------------------------------------------------------------


def prepare_request(
    body: Dict[str, Any],
    model_meta: Optional[Dict[str, Any]] = None,
    sanitize_prompt: bool = False,
    append_prompt: str = "",
) -> Dict[str, Any]:
    """把客户端请求体改写成上游能接受的形状。入参不会被修改。

    顺序不是随意的：``stream`` 必须最先钉死（上游拒绝非流式），工具配对必须在
    thinking 注入之前（否则补出来的字段会被当成孤儿的判据），回填 reasoning 必须
    在配对之后（配对可能删除消息）。前面的改动都可能让后面的判断失效。

    ``append_prompt`` 是网关自己的追加提示词：接在最后一条 system 后面（没有就造一条）。
    它是"网关替客户端说的话"，所以不参与 sanitize——洗用户内容，不该洗自己的配置。
    """
    obj = json.loads(json.dumps(body)) if body else {}
    obj["stream"] = True
    _translate_max_completion_tokens(obj)
    _clamp_gpt_max_tokens(obj)
    obj.setdefault("stream_options", {"include_usage": True})
    reasoning_model = is_reasoning_model(str(obj.get("model") or ""), model_meta)
    _normalize_tool_choice(obj)
    _normalize_tools(obj)
    _normalize_stop(obj)
    _drop_underscore_keys(obj)
    _normalize_roles(obj)
    _normalize_image_parts(obj)
    _drop_reasoning_parts(obj)
    if reasoning_model:
        _default_system_prompt(obj)
    messages = obj.get("messages")
    if isinstance(messages, list):
        obj["messages"] = pair_tool_calls(messages)
    if sanitize_prompt:
        # 放在配对之后：配对会删除消息，白洗一遍是浪费，删掉的消息也不需要洗。
        request_sanitize.sanitize_messages(obj.get("messages"))
    _inject_thinking(obj, model_meta, reasoning_model)
    _ensure_reasoning_effort(obj, model_meta, reasoning_model)
    _clamp_reasoning_effort(obj, model_meta)
    if reasoning_model:
        _backfill_reasoning(obj)
    if append_prompt:
        _append_system_prompt(obj, append_prompt)
    # 上游不认的字段留给他们自己忽略，但空 choices/messages 一定要有。
    obj.setdefault("messages", [])
    return obj


def _append_system_prompt(obj: Dict[str, Any], text: str) -> None:
    """把网关的追加提示词接到 system 消息末尾（缺失就新建一条）。

    追加而不是覆盖：客户端自己的系统提示词是它调好的，网关只补"习惯"这一层。
    """
    messages = obj.get("messages")
    if not isinstance(messages, list):
        messages = []
        obj["messages"] = messages
    extra = str(text).strip()
    if not extra:
        return
    for message in messages:
        if isinstance(message, dict) and str(message.get("role") or "") == "system":
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = (content + "\n\n" + extra).strip()
            elif isinstance(content, list):
                content.append({"type": "text", "text": extra})
            else:
                message["content"] = extra
            return
    messages.insert(0, {"role": "system", "content": extra})


def is_reasoning_model(model: str, model_meta: Optional[Dict[str, Any]] = None) -> bool:
    """判断该模型是否吃 ``thinking`` / ``reasoning_effort``。

    目录里的 ``reasoning`` 字段最权威；目录没预热时按名字猜，猜错的代价只是多带
    两个上游会忽略的字段（带了不支持的 ``reasoning_effort`` 才会 11150，那个由
    ``_clamp_reasoning_effort`` 兜）。
    """
    meta = _meta_for(model, model_meta)
    if isinstance(meta, dict) and "reasoning" in meta:
        return bool(meta.get("reasoning"))
    lowered = model.lower()
    return any(marker in lowered for marker in REASONING_MARKERS)


DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."


def _default_system_prompt(obj: Dict[str, Any]) -> None:
    """上游对"第一条不是 system"的请求会插入自己的提示词，行为不可控；先占位。"""
    messages = obj.get("messages")
    if not isinstance(messages, list) or not messages:
        return
    first = messages[0]
    if isinstance(first, dict) and str(first.get("role") or "") == "system":
        return
    messages.insert(0, {"role": "system", "content": DEFAULT_SYSTEM_PROMPT})


def _inject_thinking(obj: Dict[str, Any], model_meta: Optional[Dict[str, Any]], reasoning_model: bool) -> None:
    """deepseek 系必须 ``thinking`` 与 ``reasoning_effort`` 同时出现才有思考输出。

    实测同一 prompt：只发 ``thinking`` → ``reasoning_tokens: 0``；两者都发 → 37。
    ``reasoning_effort: "none"/"off"`` 表示用户主动关掉，此时不发 ``thinking``。
    """
    if not reasoning_model:
        obj.pop("thinking", None)
        obj.pop("reasoning_effort", None)
        return
    effort = obj.get("reasoning_effort")
    normalized = str(effort).lower() if isinstance(effort, str) else ""
    off = normalized in ("none", "off", "disabled")
    if off:
        meta = _meta_for(str(obj.get("model") or ""), model_meta) or {}
        if meta.get("canDisableThinking") is False:
            obj["thinking"] = {"type": "enabled"}  # 模型关不掉，别假装关
            obj["reasoning_effort"] = meta.get("defaultEffort") or "medium"
        else:
            # 只发 thinking 不带 effort 上游会当成"没开思考"，干脆两个都不发
            obj.pop("thinking", None)
            obj.pop("reasoning_effort", None)
            obj.pop("reasoningEffort", None)
        return
    thinking = obj.get("thinking")
    if not isinstance(thinking, dict) or not thinking.get("type"):
        obj["thinking"] = {"type": "enabled"}
    if not isinstance(effort, str) or not effort.strip():
        meta = _meta_for(str(obj.get("model") or ""), model_meta) or {}
        obj["reasoning_effort"] = meta.get("defaultEffort") or DEFAULT_REASONING_EFFORT


def _backfill_reasoning(obj: Dict[str, Any]) -> None:
    """历史里的 assistant 消息必须带 reasoning 字段，否则上游长度校验不过。

    有的客户端只回传 ``content``，网关就补一个空格——长度校验要的是"非空"，
    空格足够，而编造内容会被当成真实思考历史再次计费。
    """
    messages = obj.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict) or str(message.get("role")) != "assistant":
            continue
        text = message.get("reasoning_content") or message.get("reasoning") or ""
        if not isinstance(text, str) or not text.strip():
            text = " "
        message["reasoning_content"] = text
        message["reasoning"] = text


def _normalize_stop(obj: Dict[str, Any]) -> None:
    """上游只认 ``[]string``：字符串 ``stop`` 会 400 11101。"""
    stop = obj.get("stop")
    if stop is None:
        return
    if isinstance(stop, str):
        obj["stop"] = [stop] if stop else []
        if not stop:
            obj.pop("stop")
        return
    if isinstance(stop, (list, tuple)):
        items = [s for s in stop if isinstance(s, str) and s]
        if items:
            obj["stop"] = items
        else:
            obj.pop("stop", None)
        return
    obj.pop("stop", None)


def _drop_underscore_keys(obj: Dict[str, Any]) -> None:
    """``_`` 开头的是客户端私货（如 ``_client_hint``），上游会 11101。"""
    for key in [k for k in list(obj.keys()) if isinstance(k, str) and k.startswith("_")]:
        obj.pop(key, None)


def _translate_max_completion_tokens(obj: Dict[str, Any]) -> None:
    alias = obj.pop("max_completion_tokens", None)
    if alias is None or "max_tokens" in obj:
        return
    if isinstance(alias, bool):
        return
    if isinstance(alias, (int, float)) and alias > 0 and float(alias).is_integer():
        obj["max_tokens"] = int(alias)


def _clamp_gpt_max_tokens(obj: Dict[str, Any]) -> None:
    model = str(obj.get("model") or "").lower()
    if "gpt-" not in model:
        return
    value = obj.get("max_tokens")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value < GPT_MIN_MAX_TOKENS:
        obj["max_tokens"] = GPT_MIN_MAX_TOKENS


def _normalize_tool_choice(obj: Dict[str, Any]) -> None:
    choice = obj.get("tool_choice")
    if isinstance(choice, dict):
        # 上游只认字符串；对象形式（{"type":"function",...}）会被 11101 拒掉。
        name = ""
        function = choice.get("function")
        if isinstance(function, dict):
            name = str(function.get("name") or "")
        if name or choice.get("type") == "function":
            obj["tool_choice"] = "auto"
        else:
            obj.pop("tool_choice", None)
    elif isinstance(choice, bool):
        obj["tool_choice"] = "auto" if choice else "none"


def _normalize_tools(obj: Dict[str, Any]) -> None:
    tools = obj.get("tools")
    if not isinstance(tools, list):
        return
    cleaned: List[Dict[str, Any]] = []
    seen: set = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        # 有的客户端发 {"name":..., "parameters":...}，上游要 {"type":"function","function":{...}}
        if "function" not in tool and "name" in tool:
            tool = {
                "type": "function",
                "function": {
                    "name": tool.get("name"),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters") or tool.get("input_schema") or {"type": "object"},
                },
            }
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
            continue  # 11133：名字必须是 ^[A-Za-z0-9_-]{1,64}$
        if name in seen:
            continue  # 同名工具会让上游 schema 冲突
        seen.add(name)
        function["parameters"] = repair_schema(function.get("parameters"))
        tool["type"] = "function"
        cleaned.append(tool)
    if cleaned:
        obj["tools"] = cleaned
        if obj.get("tool_choice") not in (None, "none", "") and not _choice_known(obj["tool_choice"], seen):
            obj["tool_choice"] = "auto"
    else:
        obj.pop("tools", None)
        obj.pop("tool_choice", None)


TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 客户端手写的 schema 里常见的类型别名：上游只认 JSON Schema 的正式类型名。
TYPE_ALIASES = {
    "str": "string", "text": "string", "int": "integer", "integer": "integer",
    "num": "number", "float": "number", "double": "number", "bool": "boolean",
    "boolean": "boolean", "dict": "object", "map": "object", "obj": "object",
    "list": "array", "array": "array", "null": "null", "none": "null",
}


def repair_schema(schema: Any) -> Dict[str, Any]:
    """把客户端塞进来的 schema 修成上游能校验的形状。

    上游对函数 schema 挑剔到会 400 11129：``type`` 必须是正式类型名、
    ``properties`` 必须是对象、``required`` 只能列 properties 里真有的键。
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    fixed: Dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, str):
            fixed["type"] = TYPE_ALIASES.get(value.strip().lower(), value)
        elif key == "type" and isinstance(value, list):
            types = [TYPE_ALIASES.get(str(v).strip().lower(), str(v)) for v in value]
            fixed["type"] = types[0] if len(types) == 1 else types
        elif key == "properties" and isinstance(value, dict):
            fixed["properties"] = {str(k): repair_schema(v) for k, v in value.items()}
        elif key == "items":
            fixed["items"] = repair_schema(value) if isinstance(value, dict) else {"type": "string"}
        elif key in ("anyOf", "oneOf", "allOf") and isinstance(value, list):
            fixed[key] = [repair_schema(v) for v in value if isinstance(v, dict)]
        elif key == "required" and isinstance(value, list):
            fixed["required"] = [str(v) for v in value if isinstance(v, str)]
        else:
            fixed[key] = value
    fixed.setdefault("type", "object")
    if fixed["type"] == "object":
        fixed.setdefault("properties", {})
        props = fixed.get("properties") or {}
        if isinstance(fixed.get("required"), list):
            kept = [name for name in fixed["required"] if name in props]
            if kept:
                fixed["required"] = kept
            else:
                fixed.pop("required", None)
        if not props:
            fixed.pop("properties", None)
            fixed["properties"] = {}
    if fixed["type"] != "object":
        fixed.pop("required", None)
    return fixed


def _choice_known(choice: Any, names: set) -> bool:
    """``tool_choice`` 指名了某个工具但它已被剔除时，退回 ``auto``。"""
    if not isinstance(choice, str):
        return True
    if choice in ("auto", "required", "none", "any"):
        return True
    return choice in names


def _normalize_roles(obj: Dict[str, Any]) -> None:
    messages = obj.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").lower()
        if role == "developer":
            message["role"] = "system"
        elif role == "function":
            message["role"] = "tool"  # 老式 function 调用按 tool 结果发
        elif role not in KNOWN_ROLES and role:
            message["role"] = "user"


def _normalize_image_parts(obj: Dict[str, Any]) -> None:
    messages = obj.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image" and "image_url" not in part:
                source = part.get("source") if isinstance(part.get("source"), dict) else {}
                url = source.get("url") or source.get("data") or ""
                if url and source.get("type") == "base64":
                    media = source.get("media_type") or "image/png"
                    url = f"data:{media};base64,{url}"
                if url:
                    part["type"] = "image_url"
                    part["image_url"] = {"url": url}
                    part.pop("source", None)
            image = part.get("image_url")
            if isinstance(image, str):
                part["image_url"] = {"url": image}
            elif isinstance(image, dict) and isinstance(image.get("url"), str):
                image.setdefault("detail", "auto")


def _drop_reasoning_parts(obj: Dict[str, Any]) -> None:
    """content 数组里的 reasoning 片段上游不认，会导致 11101。"""
    messages = obj.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        kept = [p for p in content if not (isinstance(p, dict) and p.get("type") in ("reasoning", "thinking"))]
        if len(kept) != len(content):
            message["content"] = kept or ""


def _meta_for(model: str, model_meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """取模型目录里的一项。目录既可能是 ``{model: meta}`` 也可能是 meta 本身。"""
    if not isinstance(model_meta, dict) or not model_meta:
        return {}
    entry = model_meta.get(model)
    if isinstance(entry, dict):
        return entry
    if any(k in model_meta for k in ("reasoning", "supportedEfforts", "reasoning_efforts", "canDisableThinking", "defaultEffort")):
        return model_meta
    return {}


def _supported_efforts(meta: Dict[str, Any]) -> List[str]:
    """目录里字段名有新老两套：``supportedEfforts``（新）/ ``reasoning_efforts``（旧）。"""
    raw = meta.get("supportedEfforts") or meta.get("reasoning_efforts") or []
    return [str(item).lower() for item in raw if isinstance(item, str)]


def _ensure_reasoning_effort(
    obj: Dict[str, Any], model_meta: Optional[Dict[str, Any]], reasoning_model: bool
) -> None:
    """给"必须带档位"的模型补一个合法档位。

    glm 这类模型名里没有思考字样、也不需要 ``thinking`` 字段，但缺 ``reasoning_effort``
    会直接 11150。档位优先取目录里的 ``defaultEffort``，其次取它支持的最弱档，
    最后才用 ``high``——补错档位比不补好，因为不补是硬错误。
    """
    if reasoning_model:
        return  # 推理模型已经由 _inject_thinking 处理
    model = str(obj.get("model") or "").lower()
    if not any(marker in model for marker in EFFORT_ONLY_MARKERS):
        return
    meta = _meta_for(str(obj.get("model") or ""), model_meta)
    current = obj.get("reasoning_effort") or obj.get("reasoningEffort")
    if isinstance(current, str) and current.strip():
        return
    supported = _supported_efforts(meta)
    effort = meta.get("defaultEffort") if isinstance(meta, dict) else None
    if not isinstance(effort, str) or not effort.strip():
        known = [item for item in supported if item in EFFORT_RANK and EFFORT_RANK[item] > 0]
        effort = min(known, key=lambda item: EFFORT_RANK[item]) if known else DEFAULT_REASONING_EFFORT
    obj["reasoning_effort"] = str(effort).strip().lower()


def _clamp_reasoning_effort(obj: Dict[str, Any], model_meta: Optional[Dict[str, Any]]) -> None:
    """把请求档位降到模型支持的档位，避免出站带非法档位（11150）。"""
    model = str(obj.get("model") or "")
    meta = _meta_for(model, model_meta)
    supported = _supported_efforts(meta)
    if not supported:
        return
    key = "reasoning_effort" if "reasoning_effort" in obj else ("reasoningEffort" if "reasoningEffort" in obj else "")
    if not key:
        return
    requested = str(obj.get(key) or "").strip().lower()
    if requested in supported:
        return
    rank = EFFORT_RANK.get(requested)
    if rank is None:
        return
    candidates = [EFFORT_RANK.get(item) for item in supported]
    lower = [r for r in candidates if r is not None and r <= rank]
    if lower:
        target_rank = max(lower)
    else:
        known = [r for r in candidates if r is not None]
        target_rank = min(known) if known else None
    if target_rank is None:
        return
    for item in supported:
        if EFFORT_RANK.get(item) == target_rank:
            obj[key] = item
            return


# --------------------------------------------------------------------------
# tool 配对修复
# --------------------------------------------------------------------------


def pair_tool_calls(messages: List[Any]) -> List[Any]:
    """修不完整的 tool_calls/tool 配对。

    上游对"assistant 声明了 tool_calls 却没有对应 tool 结果"的会话会**之后每条
    消息都返回 400**，所以这层必须兜住：先合并背靠背的 tool_calls，再把 tool 结果
    挪到紧邻位置，最后剔除两侧的孤儿。
    """
    merged = _merge_adjacent_tool_calls(messages)
    repacked = _repack_tool_results(merged)
    return _drop_orphan_tools(repacked)


def _merge_adjacent_tool_calls(messages: List[Any]) -> List[Any]:
    result: List[Any] = []
    for message in messages:
        if (
            result
            and isinstance(message, dict)
            and isinstance(result[-1], dict)
            and message.get("role") == "assistant"
            and result[-1].get("role") == "assistant"
            and message.get("tool_calls")
            and result[-1].get("tool_calls")
            and not message.get("content")
        ):
            result[-1]["tool_calls"] = list(result[-1]["tool_calls"]) + list(message["tool_calls"])
            continue
        result.append(message)
    return result


def _repack_tool_results(messages: List[Any]) -> List[Any]:
    """把插在 tool 结果中间的普通消息挪到这批结果之后。"""
    result: List[Any] = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        result.append(message)
        index += 1
        if not (isinstance(message, dict) and message.get("role") == "assistant" and message.get("tool_calls")):
            continue
        declared = _declared_ids(message)
        results: List[Any] = []
        deferred: List[Any] = []
        while index < total:
            candidate = messages[index]
            role = candidate.get("role") if isinstance(candidate, dict) else ""
            if role == "tool":
                results.append(candidate)
                index += 1
                continue
            # 只允许"同一轮里"的普通消息被推迟一次，避免把后续对话整体后移。
            if role in ("user", "system"):
                break
            deferred.append(candidate)
            index += 1
        result.extend(results)
        result.extend(deferred)
        if declared:
            continue
    return result


def _drop_orphan_tools(messages: List[Any]) -> List[Any]:
    declared: set = set()
    result: List[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            result.append(message)
            continue
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            declared |= _declared_ids(message)
            result.append(message)
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id and call_id in declared:
                result.append(message)
            continue
        result.append(message)
    # 反向：声明了但没有结果的 tool_calls 也要摘掉，否则下一轮必 400。
    answered = {str(m.get("tool_call_id")) for m in result if isinstance(m, dict) and m.get("role") == "tool"}
    for message in result:
        if isinstance(message, dict) and message.get("role") == "assistant" and message.get("tool_calls"):
            kept = [call for call in message["tool_calls"] if str(_call_id(call)) in answered]
            if len(kept) != len(message["tool_calls"]):
                if kept:
                    message["tool_calls"] = kept
                else:
                    message.pop("tool_calls", None)
    return result


def _declared_ids(message: Dict[str, Any]) -> set:
    return {str(_call_id(call)) for call in (message.get("tool_calls") or []) if _call_id(call)}


def _call_id(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("id") or "")
    return ""


# --------------------------------------------------------------------------
# 响应侧
# --------------------------------------------------------------------------


_MEDIA_PART_TYPES = (
    "image_url", "input_image", "image",
    "video_url", "input_video",
    "audio_url", "input_audio",
    "file", "input_file",
)


def has_media(body: Dict[str, Any]) -> bool:
    """请求里有没有非文本内容（图片/音频/文件）。

    媒体请求不该绑会话亲和：同一张图被反复提交和多轮对话无关，把这种请求钉在
    一个号上只会让该号的请求画像更反常（审计原话：媒体路径应禁用亲和）。
    """
    messages = (body or {}).get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and str(part.get("type") or "").lower() in _MEDIA_PART_TYPES:
                return True
    return False


def neutralize_payload(raw: bytes) -> Optional[bytes]:
    """把系统提示词换成中性最小版，供内容审核打回后的唯一一次重试。

    只动 ``role == "system"`` 的消息：用户自己的话一个字都不改（改了就是在替用户
    说话，也可能把请求变成本意不同的东西）。没有 system 消息时返回 ``None``——
    同一份内容原样重发只会再被拦一次，纯浪费一次风控计数。
    """
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    messages = obj.get("messages")
    if not isinstance(messages, list):
        return None
    system_messages = [
        item for item in messages
        if isinstance(item, dict) and str(item.get("role") or "") == "system"
    ]
    if not system_messages:
        return None
    if all(_plain_text(item) == NEUTRAL_SYSTEM_PROMPT for item in system_messages):
        # 本来就已经是中性提示词（多数是 prepare_request 自动补的那句），
        # 原样重发只是白烧一次风控计数。
        return None
    kept = [
        item for item in messages
        if not (isinstance(item, dict) and str(item.get("role") or "") == "system")
    ]
    obj["messages"] = [{"role": "system", "content": NEUTRAL_SYSTEM_PROMPT}] + kept
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _plain_text(message: Any) -> str:
    """取一条消息的纯文本内容（用于比较提示词是否已经"足够中性"）。"""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and str(item.get("type") or "") == "text":
                parts.append(str(item.get("text") or ""))
        return "".join(parts).strip()
    return ""


def normalize_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    """补齐缓存别名和 total_tokens，客户端各按自己的字段名读。"""
    out = dict(usage or {})
    best = 0.0
    for parent, key in _CACHE_PAIRS:
        container = out.get(parent) if parent else out
        if isinstance(container, dict):
            value = container.get(key)
            if isinstance(value, (int, float)) and value > best:
                best = float(value)
    out["cache_read_input_tokens"] = best
    out["cached_tokens"] = best
    out["prompt_cache_hit_tokens"] = best
    details = out.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = {}
    details["cached_tokens"] = best
    out["prompt_tokens_details"] = details
    prompt = _number(out.get("prompt_tokens"))
    completion = _number(out.get("completion_tokens"))
    if "total_tokens" not in out and (prompt or completion):
        out["total_tokens"] = int(prompt + completion)
    return out


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def normalize_chunk(obj: Dict[str, Any], completion_id: str, model: str) -> Optional[Dict[str, Any]]:
    """把上游分片改写成规范化 OpenAI chunk；无内容可转发时返回 None。"""
    out: Dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": obj.get("created") or int(__import__("time").time()),
        "model": model,
        "choices": [],
    }
    choices = obj.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                message = choice.get("message")
                delta = dict(message) if isinstance(message, dict) else {}
                if delta.get("role") == "assistant" and not delta.get("content"):
                    delta.pop("content", None)
            cleaned: Dict[str, Any] = {}
            for key in ("role", "content", "reasoning_content", "tool_calls", "function_call", "refusal"):
                if key in delta and delta[key] not in (None, "", []):
                    cleaned[key] = delta[key]
            finish = choice.get("finish_reason")
            frame: Dict[str, Any] = {
                "index": choice.get("index", 0),
                "delta": cleaned,
                "finish_reason": finish,
            }
            out["choices"].append(frame)
    if "usage" in obj:
        usage = obj.get("usage")
        out["usage"] = normalize_usage(usage) if isinstance(usage, dict) else None
    if not out["choices"] and "usage" not in out:
        return None
    return out


def stream_frames(
    lines: Iterable[str],
    completion_id: str,
    model: str,
    tool_names: Optional[Iterable[str]] = None,
) -> Iterable[bytes]:
    """上游 SSE 行 -> 客户端 SSE 帧（含结尾 ``[DONE]``）。

    正文要过一遍 ``MarkupFilter``：上游偶尔把工具调用以标记文本写进 ``content``，
    直接透传等于把标记糊到用户脸上。
    """
    filt = markup.MarkupFilter(list(tool_names) if tool_names else None)
    emitted = False
    call_index = 0

    def frames_for_chunk(chunk: Dict[str, Any]) -> Iterable[bytes]:
        nonlocal emitted
        emitted = True
        yield sse.sse_bytes(chunk)

    for event in sse.iter_sse(lines):
        if event.data == "[DONE]":
            break
        try:
            obj = json.loads(event.data)
        except (TypeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        chunk = normalize_chunk(obj, completion_id, model)
        if chunk is None:
            continue
        content_holder = {"text": ""}
        for choice in chunk["choices"]:
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if isinstance(text, str):
                released = filt.feed(text)
                if released:
                    delta["content"] = released
                else:
                    delta.pop("content", None)
                content_holder["text"] = released
            if not delta:
                delta["content"] = ""
        if all(not choice["delta"] for choice in chunk["choices"]) and "usage" not in chunk:
            continue
        yield from frames_for_chunk(chunk)

    tail = filt.flush()
    if tail:
        emitted = True
        yield sse.sse_bytes(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"content": tail}, "finish_reason": None}],
            }
        )
    if filt.calls:
        emitted = True
        yield sse.sse_bytes(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": markup.calls_to_delta(filt.calls, call_index),
                        "finish_reason": None,
                    }
                ],
            }
        )
        yield sse.sse_bytes(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            }
        )
    if not emitted:
        # 上游一帧有效内容都没有：给客户端一个空但合法的流，避免 SDK 卡死。
        yield sse.sse_bytes(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
            }
        )
    yield sse.sse_done()


def aggregate(
    lines: Iterable[str],
    completion_id: str,
    model: str,
    tool_names: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """把上游流聚合成一条非流式 chat.completion。"""
    text: List[str] = []
    reasoning: List[str] = []
    tool_calls: Dict[int, Dict[str, Any]] = {}
    finish = "stop"
    usage: Dict[str, Any] = {}
    role = "assistant"

    for event in sse.iter_sse(lines):
        if event.data == "[DONE]":
            break
        try:
            obj = json.loads(event.data)
        except (TypeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else choice.get("message")
            if not isinstance(delta, dict):
                continue
            if delta.get("role"):
                role = str(delta["role"])
            if isinstance(delta.get("content"), str):
                text.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning.append(delta["reasoning_content"])
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                index = int(call.get("index") or 0)
                slot = tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    slot["id"] = str(call["id"])
                function = call.get("function") or {}
                if isinstance(function, dict):
                    if function.get("name"):
                        slot["function"]["name"] += str(function["name"])
                    if function.get("arguments"):
                        slot["function"]["arguments"] += str(function["arguments"])

    joined = "".join(text)
    if not tool_calls and "DSML" in joined:
        joined, leaked = markup.extract(joined, list(tool_names) if tool_names else None)
        if leaked:
            tool_calls = {i: call for i, call in enumerate(leaked)}
            if finish == "stop":
                finish = "tool_calls"

    message: Dict[str, Any] = {"role": role, "content": joined}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = [tool_calls[idx] for idx in sorted(tool_calls)]
        if not message["content"]:
            message["content"] = None
        if finish == "stop":
            finish = "tool_calls"
    response: Dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage:
        response["usage"] = normalize_usage(usage)
    return response
