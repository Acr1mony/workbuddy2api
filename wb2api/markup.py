"""把漏进正文里的工具调用标记抠出来。

上游偶尔不回 ``delta.tool_calls``，而是把调用以标记文本的形式写进 ``content``：

    <｜｜DSML｜｜ calls>
    <｜｜DSML｜｜ invoke name="exec_command">
    <｜｜DSML｜｜ parameter name="cmd" string="true">ls -la</｜｜DSML｜｜ parameter>
    </｜｜DSML｜｜ invoke>
    </｜｜DSML｜｜ calls>

这段文本会原样出现在用户界面上，客户端也不会执行它——功能上等于"模型说它要调用
工具，但什么都没发生"。所以网关要把这段标记翻译成标准的 ``tool_calls``。

两个细节不能省：

* **流式必须增量过滤**。标记可能被切成任意片段（``<｜｜`` / ``DSM`` / ``L｜｜ calls>``），
  必须"尾巴看上去还可能变成标记就先扣住"，等确认不是再吐出来，否则用户会先看到
  半截标记再被补删，闪烁得像 bug。
* **识别要宽松，翻译要严格**。不同版本用的标签名不完全一样（``calls`` /
  ``tool_calls`` / ``function_calls``，竖线可能是全角也可能是 ASCII），但工具名必须
  对得上客户端声明过的名字，否则宁可按文本处理——凭空造一个工具调用会让客户端
  直接崩溃。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# 标记前缀里那条竖线可能是全角 U+FF5C 也可能是 ASCII，DSML 前后还可能有别的字。
TAG_START = r"<[^>]*DSML[^>]*"
CALLS_RE = re.compile(TAG_START + r"(?:calls|tool_calls|function_calls)>", re.DOTALL)
INVOKE_RE = re.compile(TAG_START + r"invoke\s+name=[\"']([^\"']+)[\"'][^>]*>")
PARAM_RE = re.compile(TAG_START + r"parameter\s+name=[\"']([^\"']+)[\"']([^>]*)>")
CLOSE_CALLS_RE = re.compile(r"</[^>]*(?:calls|tool_calls|function_calls)>", re.DOTALL)
CLOSE_INVOKE_RE = re.compile(r"</[^>]*invoke>", re.DOTALL)
CLOSE_PARAM_RE = re.compile(r"</[^>]*parameter>", re.DOTALL)

# 名字必须像工具名：宽松但拒绝空白与引号，避免把正文里的 "<b>" 当成调用。
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}$")
STRING_TRUE_RE = re.compile(r"string\s*=\s*[\"']?true[\"']?", re.IGNORECASE)

MARKUP_HINT = "DSML"
MAX_HOLD = 256 * 1024  # 超过这个量还等不到闭合，就当它是普通文本
MAX_TAG = 256  # 单个起始标签不可能这么长，超了就说明它压根不是标记


def _call_id(index: int) -> str:
    return "call_%d_%d" % (int(time.time() * 1e6), index)


def _clean_value(raw: str, is_string: bool) -> Any:
    if is_string:
        return raw
    text = raw.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return raw


def _name_ok(name: str, allowed: Optional[Sequence[str]]) -> bool:
    if not name or not NAME_RE.match(name):
        return False
    if allowed:
        return name in allowed
    return True


def parse_block(block: str, allowed: Optional[Sequence[str]] = None, start_index: int = 0) -> List[Dict[str, Any]]:
    """解析一段完整的 ``<… calls>…</… calls>`` 文本，返回标准 tool_calls。"""
    calls: List[Dict[str, Any]] = []
    for match in INVOKE_RE.finditer(block):
        name = match.group(1).strip()
        if not _name_ok(name, allowed):
            continue
        body_start = match.end()
        close = CLOSE_INVOKE_RE.search(block, body_start)
        body = block[body_start: close.start() if close else len(block)]
        params: Dict[str, Any] = {}
        for pmatch in PARAM_RE.finditer(body):
            pname = pmatch.group(1).strip()
            attrs = pmatch.group(2) or ""
            pstart = pmatch.end()
            pclose = CLOSE_PARAM_RE.search(body, pstart)
            raw = body[pstart: pclose.start() if pclose else len(body)]
            params[pname] = _clean_value(raw, bool(STRING_TRUE_RE.search(attrs)))
        arguments = json.dumps(params, ensure_ascii=False) if params else "{}"
        calls.append({
            "id": _call_id(start_index + len(calls)),
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    return calls


def extract(text: str, allowed: Optional[Sequence[str]] = None) -> Tuple[str, List[Dict[str, Any]]]:
    """非流式用：一次抠完。返回 (剩余正文, tool_calls)。"""
    if not text or "DSML" not in text:
        return text, []
    calls: List[Dict[str, Any]] = []
    kept: List[str] = []
    cursor = 0
    for match in CALLS_RE.finditer(text):
        close = CLOSE_CALLS_RE.search(text, match.end())
        if close is None:
            continue  # 不完整的块留在正文里，让 MarkupFilter 去处理
        kept.append(text[cursor: match.start()])
        calls.extend(parse_block(text[match.end(): close.start()], allowed, len(calls)))
        cursor = close.end()
    kept.append(text[cursor:])
    remaining = "".join(kept)
    if calls:
        return remaining, calls
    return text, []


class MarkupFilter:
    """流式增量过滤器：既能吐正文，又能在结尾交出解析出的 tool_calls。"""

    def __init__(self, allowed: Optional[Sequence[str]] = None) -> None:
        self.allowed = list(allowed) if allowed else None
        self.buffer = ""
        self.calls: List[Dict[str, Any]] = []

    def feed(self, text: str) -> str:
        """喂一段正文，返回现在就可以安全发给客户端的那部分。"""
        if not text:
            return ""
        self.buffer += text
        out: List[str] = []
        while True:
            match = CALLS_RE.search(self.buffer)
            if match is None:
                break
            close = CLOSE_CALLS_RE.search(self.buffer, match.end())
            if close is None:
                break  # 块还没闭合：整段扣住
            out.append(self.buffer[: match.start()])
            self.calls.extend(parse_block(self.buffer[match.end(): close.start()], self.allowed, len(self.calls)))
            self.buffer = self.buffer[close.end():]
        hold = self._hold_start()
        if hold < 0:
            out.append(self.buffer)
            self.buffer = ""
        elif hold > 0:
            out.append(self.buffer[:hold])
            self.buffer = self.buffer[hold:]
        if len(self.buffer) > MAX_HOLD:
            out.append(self.buffer)
            self.buffer = ""
        return "".join(out)

    def _hold_start(self) -> int:
        """返回应该开始扣住的位置；``-1`` 表示整段都可以放行。"""
        buffer = self.buffer
        if not buffer:
            return -1
        if CALLS_RE.search(buffer) is not None:
            # 已经出现块起始标签但还没闭合：从标签起扣住（前面是正文，可以放）
            return max(CALLS_RE.search(buffer).start(), 0)
        last_lt = buffer.rfind("<")
        if last_lt < 0:
            return -1
        tail = buffer[last_lt:]
        if ">" in tail:
            return -1  # 完整标签（<b>…</b>），不是标记
        if len(tail) > MAX_TAG:
            return -1  # 太长，不可能是标签，放行
        return last_lt  # 末尾可能是标记开头的片段，先扣住

    def flush(self) -> str:
        """流结束时调用：把扣住的正文放出来。"""
        rest, self.buffer = self.buffer, ""
        if "DSML" in rest:
            # 收尾时还没闭合的块：能解析多少算多少，剩下的当正文
            text, calls = extract(rest, self.allowed)
            if calls:
                self.calls.extend(calls)
                return text
        return rest

    def pending(self) -> bool:
        return bool(self.buffer)


def calls_to_delta(calls: Sequence[Dict[str, Any]], index_base: int = 0) -> Dict[str, Any]:
    """把解析出的调用变成一帧 ``delta.tool_calls``（每个调用完整给出一次）。"""
    return {
        "tool_calls": [
            {
                "index": index_base + i,
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                },
            }
            for i, call in enumerate(calls)
        ]
    }
