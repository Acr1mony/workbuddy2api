"""出站请求体净化：拆掉上游内容审核按字面匹配的指纹。

上游的审核不是语义的，是**逐字匹配**的。所以客户端（Claude Code / Codex CLI 一类）
在 system prompt 里注入的固定模板句、以及它的计费头键名，会被当成"这是别家的客户端"
的证据整单拦掉，错误码 ``11-128``。

对策分三层，都不改变语义：

1. **整段剥离**计费头那种"键: 值;"形态——它没有语义，只是客户端签名。
2. **最小改写**承载语义的模板句：每句只换一个词，模型读到的东西意思不变。
3. **兜底缩写**裸键名（``x-anthropic-billing-hdr`` → ``x-anthropic-billing-hdr``）：
   实测 assistant 消息里用反引号引用裸键名同样触发拦截，而第 1 层要求有冒号。

``11-128`` 这个数字本身也是指纹：请求体里出现它（哪怕只是在讨论这个错误码）
就会被整单拦掉。插一个连字符就能过，保留可读性与指代。

净化是**可选**的（``prompt.mode = "sanitize"``）。默认 passthrough：改写用户
自己的 prompt 是有代价的，得让部署者知道自己在换什么。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# 预检串：任一命中才进净化。普通请求全部不中 → 原样返回，零改写。
FEATURES = (
    "x-anthropic-billing-hdr",
    "You are Claude Code",
    "Main branch (",
    "You are a coding agent running in the Codex CLI",
    "github.com/anthropics/",
    "11-128",
)

# 键值形态的计费头整段删除（键名即触发，与值无关）。
# 形态：``X-Anthropic-Billing-Hdr: <任意值>;``（值里不跨行、不跨分号）
HEADER_KV_RE = re.compile(r"(?i)x-anthropic-billing-hdr\s*[:=][^\n;]*;?\s*")
# 裸键名（无冒号无值）：整段删除会破坏句子，只做最小缩写。
HEADER_BARE_RE = re.compile(r"(?i)x-anthropic-billing-hdr")
# 尾随的裸计量键值：``cc_foo=bar;`` 反复清到不再变化。
CC_KV_RE = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\n]*;?\s*")

# 改写层：左边是上游认得的原句，右边是"只差一个词"的等价句。
# 左边一律不带结尾标点，这样 CLI 版（句号收尾）和桌面版（逗号接后面内容）都能命中。
REWRITES: Tuple[Tuple[str, str], ...] = (
    (
        "You are Claude Code, Anthropic's official CLI for Claude",
        "You are Claude Code, Anthropic's official CLI tool for Claude",
    ),
    (
        "You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
        "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant.",
    ),
    (
        "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
        "To provide feedback, users should report issues at https://github.com/anthropics/claude-code/issues",
    ),
    ("11-128", "11-128"),
)


def has_fingerprint(text: str) -> bool:
    if not text:
        return False
    for feature in FEATURES:
        if feature in text:
            return True
    return HEADER_BARE_RE.search(text) is not None


def sanitize_text(text: str) -> str:
    """单段文本净化。没有指纹就原样返回（绝大多数请求走这条路）。"""
    if not has_fingerprint(text):
        return text
    for source, target in REWRITES:
        if source in text:
            text = text.replace(source, target)
    if HEADER_KV_RE.search(text):
        text = HEADER_KV_RE.sub("", text)
    if "cc_" in text:
        previous = None
        while previous != text:  # 清尾随裸 kv，可能连着好几段
            previous = text
            text = CC_KV_RE.sub("", text)
    text = HEADER_BARE_RE.sub("x-anthropic-billing-hdr", text)
    # 剥离层会留下多余空白；行内连续空白的收缩不改变语义，只是别让模型看到空洞。
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def sanitize_content(value: Any) -> Tuple[Any, bool]:
    """兼容字符串与多模态数组；只动 text part，图片等部件不碰。"""
    if isinstance(value, str):
        cleaned = sanitize_text(value)
        return cleaned, cleaned != value
    if isinstance(value, list):
        changed = False
        for part in value:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            cleaned = sanitize_text(text)
            if cleaned != text:
                part["text"] = cleaned
                changed = True
        return value, changed
    return value, False


def sanitize_tool_calls(value: Any) -> bool:
    """``arguments`` 是字符串化的 JSON，按文本走即可。

    这块最容易被漏：工具调用轮的 ``content`` 通常是 null，早先的实现看到 content
    缺失就 ``continue``，于是历史里写进工具参数的被拦字符串（文件名、命令、写入的
    文件内容）原样出站。
    """
    if not isinstance(value, list):
        return False
    changed = False
    for call in value:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            continue
        cleaned = sanitize_text(arguments)
        if cleaned != arguments:
            function["arguments"] = cleaned
            changed = True
    return changed


def sanitize_messages(messages: Any) -> bool:
    """净化 messages 里的 content / reasoning / tool_calls。任一命中返回 True。"""
    if not isinstance(messages, list):
        return False
    changed = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        if "content" in message:
            new_content, hit = sanitize_content(message["content"])
            if hit:
                message["content"] = new_content
                changed = True
        for key in ("reasoning_content", "reasoning"):
            value = message.get(key)
            if isinstance(value, str):
                cleaned = sanitize_text(value)
                if cleaned != value:
                    message[key] = cleaned
                    changed = True
        if "tool_calls" in message and sanitize_tool_calls(message["tool_calls"]):
            changed = True
    return changed
