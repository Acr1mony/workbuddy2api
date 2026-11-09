"""Anthropic Messages ↔ OpenAI Chat 的翻译层。

为什么要它：Claude Code、Codex 这类客户端只会说 ``/v1/messages``。网关上游只有
OpenAI 形状的 ``/v2/chat/completions``，所以两边都得翻译：

* 请求：``system`` 提到 messages[0]，``content`` 的块数组摊平成文本 / 图片 / 工具调用，
  ``tool_use`` / ``tool_result`` 换成 ``tool_calls`` / ``role:"tool"``；
* 响应：``stop_reason`` 映射到 Anthropic 的枚举，工具调用换成 ``tool_use`` 块；
* 流式：OpenAI 的每一帧 delta 要重排成 Anthropic 的 content_block 事件。

翻译层不碰网络、不认识账号，只做结构变换——这样它能被单测钉死，而不用起上游。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Anthropic 的 stop_reason 只有这几个值，客户端会按枚举分支。
STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


def _text_of(content: Any) -> str:
    """把 ``string | [blocks]`` 里的文本部分拼起来。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _image_block(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Anthropic 的 image 块 → OpenAI 的 ``image_url`` 部件。"""
    source = block.get("source") or {}
    if isinstance(source, dict):
        if source.get("type") == "base64" and source.get("data"):
            media = source.get("media_type") or "image/png"
            return {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (media, source["data"])}}
        if source.get("type") == "url" and source.get("url"):
            return {"type": "image_url", "image_url": {"url": source["url"]}}
    if block.get("url"):
        return {"type": "image_url", "image_url": {"url": block["url"]}}
    return None


def _tool_result_message(block: Dict[str, Any]) -> Dict[str, Any]:
    content = block.get("content")
    if isinstance(content, list):
        content = _text_of(content)
    return {
        "role": "tool",
        "tool_call_id": str(block.get("tool_use_id") or ""),
        "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
    }


def _user_messages(content: Any) -> List[Dict[str, Any]]:
    """user 消息可能同时含文本、图片和 tool_result，OpenAI 允许一部分、另一部分要拆。"""
    if isinstance(content, str) or content is None:
        return [{"role": "user", "content": content or ""}]
    if not isinstance(content, list):
        return [{"role": "user", "content": str(content)}]

    tools: List[Dict[str, Any]] = []
    parts: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append({"type": "text", "text": str(block)})
            continue
        kind = block.get("type")
        if kind == "tool_result":
            tools.append(_tool_result_message(block))
        elif kind == "image":
            image = _image_block(block)
            if image:
                parts.append(image)
        elif kind in (None, "text", "document"):
            text = block.get("text") if kind != "document" else _document_text(block)
            if text:
                parts.append({"type": "text", "text": str(text)})

    messages: List[Dict[str, Any]] = list(tools)
    if parts:
        # 只有文本时给字符串：上游对"只有一个 text 部件"的数组也能吃，但字符串更贴近原生客户端。
        if len(parts) == 1 and parts[0].get("type") == "text":
            messages.append({"role": "user", "content": parts[0]["text"]})
        else:
            messages.append({"role": "user", "content": parts})
    return messages


def _document_text(block: Dict[str, Any]) -> str:
    source = block.get("source") or {}
    if isinstance(source, dict) and source.get("type") == "text":
        return str(source.get("data") or "")
    return str(block.get("text") or "")


def _assistant_message(content: Any) -> Dict[str, Any]:
    if isinstance(content, str) or content is None:
        return {"role": "assistant", "content": content or ""}
    if not isinstance(content, list):
        return {"role": "assistant", "content": str(content)}

    texts: List[str] = []
    calls: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            texts.append(str(block))
            continue
        kind = block.get("type")
        if kind in (None, "text"):
            texts.append(str(block.get("text") or ""))
        elif kind == "tool_use":
            calls.append({
                "id": str(block.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                },
            })
        # thinking 块不进正文，单独回填到 reasoning_content（见下面 _thinking_text）
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(texts)}
    if calls:
        message["tool_calls"] = calls
    thinking_text = _thinking_text(content)
    if thinking_text:
        message["reasoning_content"] = thinking_text
    return message


def _thinking_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("thinking") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "thinking"
    )


def _tools(tools: Any) -> List[Dict[str, Any]]:
    result = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        schema = tool.get("input_schema") or tool.get("parameters") or {"type": "object", "properties": {}}
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(tool.get("description") or ""),
                "parameters": schema,
            },
        })
    return result


def _tool_choice(choice: Any) -> Optional[str]:
    """上游只认字符串形式的 tool_choice，所以这里全部折成枚举。

    ``any``/指定工具都退化成 ``auto``：上游不保证支持 required，而"没调用工具"
    总比"请求被 400 挡掉"好——客户端看到文本会自己再试一轮。
    """
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "none":
        return "none"
    if kind in ("any", "tool"):
        return "auto"
    return None


def to_chat(body: Dict[str, Any]) -> Dict[str, Any]:
    """Anthropic Messages 请求 → OpenAI Chat 请求（只做形状变换）。"""
    messages: List[Dict[str, Any]] = []

    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": _text_of(system)})

    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role == "assistant":
            messages.append(_assistant_message(content))
        elif role == "system":
            messages.append({"role": "system", "content": _text_of(content)})
        else:
            messages.extend(_user_messages(content))

    chat: Dict[str, Any] = {
        "model": body.get("model"),
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    # Anthropic 的 max_tokens 是必填的；上游的等价物是 max_tokens（不是
    # max_completion_tokens，那个会被 11101 拒）。
    if body.get("max_tokens"):
        chat["max_tokens"] = int(body["max_tokens"])
    for key in ("temperature", "top_p"):
        if body.get(key) is not None:
            chat[key] = body[key]
    if body.get("stop_sequences"):
        chat["stop"] = list(body["stop_sequences"])
    tools = _tools(body.get("tools"))
    if tools:
        chat["tools"] = tools
        choice = _tool_choice(body.get("tool_choice"))
        if choice:
            chat["tool_choice"] = choice
    if body.get("metadata"):
        chat["metadata"] = body["metadata"]
    return chat


def from_chat(completion: Dict[str, Any], model: str = "") -> Dict[str, Any]:
    """OpenAI 的完整响应 → Anthropic Message。"""
    choices = completion.get("choices") or [{}]
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    blocks: List[Dict[str, Any]] = []

    thinking = message.get("reasoning_content") or message.get("reasoning")
    if thinking:
        blocks.append({"type": "thinking", "thinking": thinking})
    text = message.get("content")
    if isinstance(text, str) and text:
        blocks.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw = function.get("arguments") or "{}"
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            parsed = {"_raw": raw}
        blocks.append({
            "type": "tool_use",
            "id": str(call.get("id") or ""),
            "name": str(function.get("name") or ""),
            "input": parsed if isinstance(parsed, dict) else {"value": parsed},
        })
    if not blocks:
        blocks.append({"type": "text", "text": ""})

    usage = completion.get("usage") or {}
    stop = choice.get("finish_reason") or "stop"
    return {
        "id": "msg_" + str(completion.get("id") or "").replace("chatcmpl-", "")[:24],
        "type": "message",
        "role": "assistant",
        "model": model or completion.get("model") or "",
        "content": blocks,
        "stop_reason": STOP_REASONS.get(str(stop), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


class MessageStream:
    """把 OpenAI 的 SSE 帧实时重排成 Anthropic 的事件序列。

    状态机：``message_start`` → 若干 ``content_block_start/delta/stop`` → ``message_delta``
    → ``message_stop``。文本块和工具块各自占一个 index，工具块的参数用
    ``input_json_delta`` 分片吐出。
    """

    def __init__(self, model: str, message_id: str = "", input_tokens: int = 0) -> None:
        self.model = model
        self.message_id = message_id or ("msg_" + str(int(__import__("time").time() * 1000)))
        self.input_tokens = input_tokens
        self._started = False
        self._finished = False
        self._index = 0
        self._open: Optional[Dict[str, Any]] = None  # 当前打开的块
        self._tools: Dict[int, Dict[str, Any]] = {}  # OpenAI tool_calls 的 index → 块
        self.output_tokens = 0
        self.stop_reason = "end_turn"
        self.text = ""

    # ---- 事件构造 -----------------------------------------------------
    def _frame(self, event: str, data: Dict[str, Any]) -> Tuple[str, str]:
        return event, json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    def start(self) -> List[Tuple[str, str]]:
        if self._started:
            return []
        self._started = True
        return [self._frame("message_start", {
            "type": "message_start",
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": self.input_tokens, "output_tokens": 0},
            },
        })]

    def _close_open(self, out: List[Tuple[str, str]]) -> None:
        if self._open is not None:
            out.append(self._frame("content_block_stop", {"type": "content_block_stop", "index": self._open["index"]}))
            self._open = None

    def _open_text(self, out: List[Tuple[str, str]]) -> Dict[str, Any]:
        if self._open is not None and self._open["type"] == "text":
            return self._open
        self._close_open(out)
        block = {"type": "text", "index": self._index}
        self._index += 1
        self._open = block
        out.append(self._frame("content_block_start", {
            "type": "content_block_start",
            "index": block["index"],
            "content_block": {"type": "text", "text": ""},
        }))
        return block

    def _open_tool(self, index: int, call_id: str, name: str, out: List[Tuple[str, str]]) -> Dict[str, Any]:
        block = self._tools.get(index)
        if block is not None:
            return block
        self._close_open(out)
        block = {"type": "tool_use", "index": self._index, "id": call_id, "name": name, "args": ""}
        self._index += 1
        self._tools[index] = block
        self._open = block
        out.append(self._frame("content_block_start", {
            "type": "content_block_start",
            "index": block["index"],
            "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}},
        }))
        return block

    def feed(self, chunk: Dict[str, Any]) -> List[Tuple[str, str]]:
        """吃一帧 OpenAI chunk，吐出零到多个 Anthropic 事件。"""
        out: List[Tuple[str, str]] = []
        choices = chunk.get("choices") or []
        if not choices:
            # usage 帧：只有 token 数，没有内容。
            usage = chunk.get("usage") or {}
            if usage.get("completion_tokens"):
                self.output_tokens = int(usage["completion_tokens"])
            if usage.get("prompt_tokens") and not self.input_tokens:
                self.input_tokens = int(usage["prompt_tokens"])
            return out

        choice = choices[0]
        delta = choice.get("delta") or {}

        reasoning = delta.get("reasoning_content")
        if reasoning:
            # 思考块：Claude 客户端认 thinking_delta；不支持它的客户端会忽略这个块。
            if not (self._open is not None and self._open["type"] == "thinking"):
                self._close_open(out)
                block = {"type": "thinking", "index": self._index}
                self._index += 1
                self._open = block
                out.append(self._frame("content_block_start", {
                    "type": "content_block_start",
                    "index": block["index"],
                    "content_block": {"type": "thinking", "thinking": ""},
                }))
            if self._open["type"] == "thinking":
                out.append(self._frame("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self._open["index"],
                    "delta": {"type": "thinking_delta", "thinking": reasoning},
                }))

        text = delta.get("content")
        if text:
            self.text += text
            block = self._open_text(out)
            out.append(self._frame("content_block_delta", {
                "type": "content_block_delta",
                "index": block["index"],
                "delta": {"type": "text_delta", "text": text},
            }))

        for call in delta.get("tool_calls") or []:
            index = int(call.get("index") or 0)
            function = call.get("function") or {}
            block = self._tools.get(index)
            if block is None:
                block = self._open_tool(index, str(call.get("id") or ""), str(function.get("name") or ""), out)
            elif function.get("name") and not block["name"]:
                block["name"] = str(function["name"])
            arguments = function.get("arguments")
            if arguments:
                block["args"] += str(arguments)
                out.append(self._frame("content_block_delta", {
                    "type": "content_block_delta",
                    "index": block["index"],
                    "delta": {"type": "input_json_delta", "partial_json": str(arguments)},
                }))

        finish = choice.get("finish_reason")
        if finish:
            self.stop_reason = STOP_REASONS.get(str(finish), "end_turn")
        return out

    def finish(self) -> List[Tuple[str, str]]:
        if self._finished:
            return []
        self._finished = True
        out: List[Tuple[str, str]] = []
        self._close_open(out)
        out.append(self._frame("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": self.output_tokens},
        }))
        out.append(self._frame("message_stop", {"type": "message_stop"}))
        return out

    def error(self, message: str, kind: str = "api_error") -> List[Tuple[str, str]]:
        """中途失败：发一个 error 事件结束，绝不补 message_stop 假装成功。"""
        self._finished = True
        return [self._frame("error", {"type": "error", "error": {"type": kind, "message": message}})]


def count_tokens(body: Dict[str, Any]) -> int:
    """粗略估 token：Anthropic 的 count_tokens 是精确值，但本地做不到。

    用"字符数 / 2.2"这个偏保守的估算（中文约 1.5 字/token，英文约 4 字符/token）。
    客户端只用它判断上下文够不够，估大一点比估小安全。
    """
    text = _text_of(body.get("system")) if body.get("system") else ""
    for message in body.get("messages") or []:
        if isinstance(message, dict):
            text += _text_of(message.get("content"))
    for tool in _tools(body.get("tools")):
        text += json.dumps(tool, ensure_ascii=False)
    return max(1, int(len(text) / 2.2))


def stream_frames(events: Iterable[Tuple[str, str]]) -> Iterable[bytes]:
    """Anthropic 的 SSE 带 event 名，空行分隔。"""
    for event, data in events:
        yield ("event: %s\ndata: %s\n\n" % (event, data)).encode("utf-8")
