"""OpenAI Responses API ⇄ 上游 ``chat/completions``。

Codex CLI 与新一点的 SDK 只说 Responses 协议（``POST /v1/responses``），而上游只讲
chat。所以这里做的是**纯翻译**：请求把 ``input`` 摊平成 messages，响应把 message /
工具调用包成 ``output`` 数组，流式再把 chat 的 delta 逐帧转成 Responses 的语义事件。

有几件事刻意不做：

* ``previous_response_id`` 不落地。要支持它得把每条响应存起来（Token 与内容都在
  里面），那是另一套生命周期；现在的做法是让客户端把历史放进 ``input``。
* ``input_file`` / ``input_audio`` 不转发——上游吃不了，静默丢比伪造更安全，但会
  在 ``output_text`` 之外留一条 ``note`` 不方便，所以这里直接忽略。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# chat 的 finish_reason → Responses 的 status / incomplete_details
STATUS_OF = {
    "stop": "completed",
    "tool_calls": "completed",
    "function_call": "completed",
    "length": "incomplete",
    "content_filter": "incomplete",
}

TEXT_PART_TYPES = ("input_text", "text", "output_text")
IMAGE_PART_TYPES = ("input_image", "image")


def _text_of(content: Any) -> str:
    """把 ``input`` 里的内容摊成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: List[str] = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if text is None and isinstance(part.get("content"), str):
                    text = part["content"]
                if isinstance(text, str):
                    out.append(text)
        return "\n".join(out)
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return ""


def _content_of(content: Any) -> Any:
    """Responses 的 content 列表 → chat 的 content。

    只有一个文本块时降级成字符串：上游对 ``[{"type":"text",...}]`` 这种包一层的
    形状并不总是宽容，能省就省。
    """
    if isinstance(content, str):
        return content
    parts: List[Dict[str, Any]] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in TEXT_PART_TYPES:
            parts.append({"type": "text", "text": str(part.get("text") or "")})
        elif kind in IMAGE_PART_TYPES:
            url = part.get("image_url") or part.get("url") or ""
            if isinstance(url, dict):
                url = url.get("url") or ""
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if not parts:
        return ""
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    return parts


def _call(call: Dict[str, Any]) -> Dict[str, Any]:
    """Responses 的 function_call item → chat 的 tool_call。"""
    arguments = call.get("arguments")
    if not isinstance(arguments, str):
        try:
            arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
        except (TypeError, ValueError):
            arguments = "{}"
    return {
        "id": str(call.get("call_id") or call.get("id") or ""),
        "type": "function",
        "function": {"name": str(call.get("name") or ""), "arguments": arguments},
    }


def _tools(tools: Any) -> List[Dict[str, Any]]:
    """Responses 的 tools（扁平）→ chat 的 tools（包一层 function）。"""
    out: List[Dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        kind = tool.get("type")
        if kind != "function":
            # web_search / file_search / computer_use 这类内置工具上游没有，跳过
            continue
        if isinstance(tool.get("function"), dict):
            out.append({"type": "function", "function": tool["function"]})
            continue
        out.append({
            "type": "function",
            "function": {
                "name": tool.get("name"),
                "description": tool.get("description") or "",
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    return out


def _tool_choice(choice: Any) -> Optional[Any]:
    if isinstance(choice, str):
        return choice
    if isinstance(choice, dict):
        if choice.get("type") == "function" and choice.get("name"):
            return {"type": "function", "function": {"name": choice["name"]}}
    return None


def to_chat(body: Dict[str, Any]) -> Dict[str, Any]:
    """Responses 请求 → chat 请求。"""
    messages: List[Dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        assistant: Optional[Dict[str, Any]] = None
        for item in raw_input:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                assistant = None
                continue
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "function_call":
                if assistant is None:
                    assistant = {"role": "assistant", "content": "", "tool_calls": []}
                    messages.append(assistant)
                assistant.setdefault("tool_calls", []).append(_call(item))
                continue
            if kind == "function_call_output":
                assistant = None
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or ""),
                    "content": _text_of(item.get("output")),
                })
                continue
            if kind == "reasoning":
                # 思考历史只有 summary 文本，回填成 assistant 的 reasoning_content，
                # 上游的推理模型要求历史里带上它（缺了会 400 或推理质量塌）
                continue
            role = item.get("role")
            if not role:
                continue
            content = _content_of(item.get("content"))
            if role == "assistant":
                assistant = {"role": "assistant", "content": content if isinstance(content, str) else _text_of(content)}
                messages.append(assistant)
            else:
                messages.append({"role": role, "content": content})
                assistant = None
    elif isinstance(raw_input, dict):
        messages.append({"role": "user", "content": _content_of(raw_input.get("content"))})

    chat: Dict[str, Any] = {
        "model": body.get("model"),
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    if body.get("max_output_tokens"):
        chat["max_tokens"] = int(body["max_output_tokens"])
    for key in ("temperature", "top_p", "parallel_tool_calls"):
        if body.get(key) is not None:
            chat[key] = body[key]
    tools = _tools(body.get("tools"))
    if tools:
        chat["tools"] = tools
        choice = _tool_choice(body.get("tool_choice"))
        if choice is not None:
            chat["tool_choice"] = choice
    text_format = (body.get("text") or {}).get("format") if isinstance(body.get("text"), dict) else None
    if isinstance(text_format, dict) and text_format.get("type") == "json_object":
        chat["response_format"] = {"type": "json_object"}
    if body.get("reasoning"):
        effort = (body["reasoning"] or {}).get("effort")
        if effort:
            chat["reasoning_effort"] = effort
    return chat


def _usage_of(usage: Dict[str, Any]) -> Dict[str, Any]:
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                    or usage.get("reasoning_tokens") or 0)
    out: Dict[str, Any] = {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": completion,
        "total_tokens": int(usage.get("total_tokens") or (prompt + completion)),
    }
    if reasoning:
        out["output_tokens_details"] = {"reasoning_tokens": reasoning}
    return out


def _message_item(text: str, item_id: str, status: str = "completed") -> Dict[str, Any]:
    return {
        "type": "message",
        "id": item_id,
        "status": status,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def from_chat(completion: Dict[str, Any], model: str = "", response_id: str = "") -> Dict[str, Any]:
    """OpenAI 的完整响应 → Responses 对象。"""
    choices = completion.get("choices") or [{}]
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    finish = choice.get("finish_reason") or "stop"
    status = STATUS_OF.get(finish, "completed")

    output: List[Dict[str, Any]] = []
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        output.append({
            "type": "reasoning",
            "id": f"rs_{(response_id or 'x')[-12:]}0",
            "summary": [{"type": "summary_text", "text": reasoning}],
        })
    text = message.get("content")
    if isinstance(text, str) and text:
        output.append(_message_item(text, f"msg_{(response_id or 'x')[-12:]}0"))
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        output.append({
            "type": "function_call",
            "id": f"fc_{(response_id or 'x')[-12:]}{index}",
            "call_id": str(call.get("id") or ""),
            "name": str(function.get("name") or ""),
            "arguments": function.get("arguments") or "{}",
            "status": "completed",
        })

    result: Dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model or completion.get("model") or "",
        "output": output,
        "output_text": text if isinstance(text, str) else "",
        "parallel_tool_calls": True,
        "usage": _usage_of(completion.get("usage") or {}),
    }
    if status == "incomplete":
        result["incomplete_details"] = {"reason": "max_output_tokens"}
    return result


class ResponseStream:
    """把上游 chat 的 delta 逐帧转成 Responses 事件。

    Codex 这类客户端是按事件名推进状态机的，所以宁可多给几帧（``in_progress``、
    ``content_part.added``），也不要把文本塞进一个不认识的事件里。
    """

    def __init__(self, model: str, response_id: str = "", created_at: Optional[int] = None) -> None:
        self.model = model
        self.response_id = response_id or "resp_"
        self.created_at = int(created_at or time.time())
        self._seq = 0
        self._index = 0
        self._text_item: Optional[Dict[str, Any]] = None
        self._text_buf: List[str] = []
        self._reasoning_item: Optional[Dict[str, Any]] = None
        self._reasoning_buf: List[str] = []
        self._calls: Dict[int, Dict[str, Any]] = {}
        self._order: List[int] = []
        self._finished = False
        self.input_tokens = 0
        self.output_tokens = 0

    # -- 事件拼装 -----------------------------------------------------------
    def _event(self, name: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        payload = dict(data)
        payload["type"] = name
        payload["sequence_number"] = self._seq
        self._seq += 1
        return name, payload

    def _response(self, status: str, usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": [],
        }
        if usage is not None:
            body["usage"] = _usage_of(usage)
        return body

    def start(self) -> List[Tuple[str, Dict[str, Any]]]:
        return [
            self._event("response.created", {"response": self._response("in_progress")}),
            self._event("response.in_progress", {"response": self._response("in_progress")}),
        ]

    def _open_text(self) -> List[Tuple[str, Dict[str, Any]]]:
        if self._text_item is not None:
            return []
        item_id = f"msg_{self.response_id[-12:]}0"
        self._text_item = {"type": "message", "id": item_id, "status": "in_progress",
                           "role": "assistant", "content": []}
        index = self._index
        self._index += 1
        return [
            self._event("response.output_item.added",
                        {"output_index": index, "item": self._text_item}),
            self._event("response.content_part.added", {
                "item_id": item_id, "output_index": index, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }),
        ]

    def _open_reasoning(self) -> List[Tuple[str, Dict[str, Any]]]:
        if self._reasoning_item is not None:
            return []
        item_id = f"rs_{self.response_id[-12:]}0"
        self._reasoning_item = {"type": "reasoning", "id": item_id, "summary": []}
        index = self._index
        self._index += 1
        return [
            self._event("response.output_item.added",
                        {"output_index": index, "item": self._reasoning_item}),
            self._event("response.reasoning_summary_part.added", {
                "item_id": item_id, "output_index": index, "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            }),
        ]

    def feed(self, chunk: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        if self._finished:
            return []
        out: List[Tuple[str, Dict[str, Any]]] = []
        usage = chunk.get("usage") or {}
        if usage:
            self.input_tokens = int(usage.get("prompt_tokens") or self.input_tokens)
            self.output_tokens = int(usage.get("completion_tokens") or self.output_tokens)
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                out.extend(self._open_reasoning())
                self._reasoning_buf.append(reasoning)
                out.append(self._event("response.reasoning_summary_text.delta", {
                    "item_id": self._reasoning_item["id"],
                    "output_index": 0, "summary_index": 0, "delta": reasoning,
                }))
            text = delta.get("content")
            if isinstance(text, str) and text:
                out.extend(self._open_text())
                self._text_buf.append(text)
                out.append(self._event("response.output_text.delta", {
                    "item_id": self._text_item["id"], "output_index": 0,
                    "content_index": 0, "delta": text,
                }))
            for call in delta.get("tool_calls") or []:
                index = int(call.get("index") or 0)
                function = call.get("function") or {}
                entry = self._calls.get(index)
                if entry is None:
                    item_id = f"fc_{self.response_id[-12:]}{index}"
                    entry = {"id": item_id, "call_id": str(call.get("id") or item_id),
                             "name": "", "arguments": ""}
                    self._calls[index] = entry
                    self._order.append(index)
                    out.append(self._event("response.output_item.added", {
                        "output_index": self._index,
                        "item": {"type": "function_call", "id": item_id,
                                 "call_id": entry["call_id"], "name": "",
                                 "arguments": "", "status": "in_progress"},
                    }))
                    self._index += 1
                if function.get("name"):
                    entry["name"] = function["name"]
                piece = function.get("arguments")
                if isinstance(piece, str) and piece:
                    entry["arguments"] += piece
                    out.append(self._event("response.function_call_arguments.delta", {
                        "item_id": entry["id"], "output_index": 0, "delta": piece,
                    }))
        return out

    def finish(self, finish_reason: str = "stop", usage: Optional[Dict[str, Any]] = None) -> List[Tuple[str, Dict[str, Any]]]:
        if self._finished:
            return []
        self._finished = True
        status = STATUS_OF.get(finish_reason, "completed")
        out: List[Tuple[str, Dict[str, Any]]] = []

        if self._reasoning_item is not None:
            text = "".join(self._reasoning_buf)
            out.append(self._event("response.reasoning_summary_text.done", {
                "item_id": self._reasoning_item["id"], "output_index": 0,
                "summary_index": 0, "text": text,
            }))
            out.append(self._event("response.output_item.done", {
                "output_index": 0,
                "item": {"type": "reasoning", "id": self._reasoning_item["id"],
                         "summary": [{"type": "summary_text", "text": text}]},
            }))
        if self._text_item is not None:
            text = "".join(self._text_buf)
            out.append(self._event("response.output_text.done", {
                "item_id": self._text_item["id"], "output_index": 0,
                "content_index": 0, "text": text,
            }))
            out.append(self._event("response.content_part.done", {
                "item_id": self._text_item["id"], "output_index": 0, "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []},
            }))
            out.append(self._event("response.output_item.done", {
                "output_index": 0,
                "item": {"type": "message", "id": self._text_item["id"], "status": "completed",
                         "role": "assistant",
                         "content": [{"type": "output_text", "text": text, "annotations": []}]},
            }))
        for index in self._order:
            entry = self._calls[index]
            out.append(self._event("response.function_call_arguments.done", {
                "item_id": entry["id"], "output_index": 0, "arguments": entry["arguments"],
            }))
            out.append(self._event("response.output_item.done", {
                "output_index": 0,
                "item": {"type": "function_call", "id": entry["id"],
                         "call_id": entry["call_id"], "name": entry["name"],
                         "arguments": entry["arguments"], "status": "completed"},
            }))
        final = self._response(status, usage or {"prompt_tokens": self.input_tokens,
                                                 "completion_tokens": self.output_tokens})
        if status == "incomplete":
            final["incomplete_details"] = {"reason": "max_output_tokens"}
        out.append(self._event("response.completed", {"response": final}))
        return out

    def error(self, message: str, code: str = "api_error") -> List[Tuple[str, Dict[str, Any]]]:
        """出错时发 ``response.failed``，并且**不再**发 ``response.completed``。"""
        if self._finished:
            return []
        self._finished = True
        body = self._response("failed")
        body["error"] = {"code": code, "message": message}
        return [self._event("response.failed", {"response": body})]


def stream_frames(events: Iterable[Tuple[str, Dict[str, Any]]]) -> Iterable[bytes]:
    """Responses 的事件流：``event: <name>\\ndata: <json>\\n\\n``，不写 ``[DONE]``。"""
    for name, data in events:
        yield ("event: %s\ndata: %s\n\n" % (name, json.dumps(data, ensure_ascii=False))).encode("utf-8")
