"""Server-Sent Events plumbing.

Three responsibilities:

* ``iter_sse``      - parse an upstream SSE body into ``(event, data)`` pairs.
* ``sse_bytes``     - encode one client-facing SSE frame.
* ``OpenAIChunker`` - turn incremental text/tool fragments into OpenAI chunks.

The parser is deliberately forgiving: the upstream emits heartbeats, blank
lines, occasional multi-line ``data:`` payloads and, on some paths, a leading
BOM.  A parse error must never kill an in-flight completion, so malformed
frames are skipped and counted instead of raised.
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .log import get_logger

log = get_logger("sse")

DONE = "[DONE]"

# 心跳帧：SSE 注释行。客户端库会直接忽略它，但中间层（nginx / 负载均衡 / 浏览器）
# 看到的是"这条连接还活着"。上游思考几十秒不出字时，没有它就会有人替你超时。
HEARTBEAT_EVENT = "__heartbeat__"
HEARTBEAT_BYTES = b": keepalive\n\n"

_HEARTBEAT = object()
_EOF = object()


class SSEEvent:
    __slots__ = ("event", "data", "raw")

    def __init__(self, event: str = "", data: str = "", raw: str = "") -> None:
        self.event = event or "message"
        self.data = data
        self.raw = raw

    def json(self) -> Optional[Dict[str, Any]]:
        if not self.data or self.data == DONE:
            return None
        try:
            parsed = json.loads(self.data)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SSEEvent {self.event} {self.data[:60]!r}>"


def iter_sse(lines: Iterable[Any], heartbeat: float = 0.0) -> Iterator[SSEEvent]:
    """Yield SSE events from an iterable of byte/str lines.

    The iterable may be a socket file object (bytes) or a list of str; both are
    normalised to UTF-8 text.  ``[DONE]`` is yielded as its own event so the
    caller can see it, then iteration stops.

    ``heartbeat`` > 0 时，上游静默超过这么多秒就额外吐一个 ``HEARTBEAT_EVENT``
    事件，调用方把它写成 ``HEARTBEAT_BYTES`` 即可。上游读放在旁路线程里——
    "等下一行"和"该发心跳了"这两件事，靠一个阻塞的 for 循环没法同时判断。
    """
    yield from _iter_sse(_with_heartbeat(lines, heartbeat))


def _with_heartbeat(lines: Iterable[Any], heartbeat: float) -> Iterator[Any]:
    if not heartbeat or heartbeat <= 0:
        yield from lines
        return
    pending: "queue.Queue[Any]" = queue.Queue(maxsize=4096)

    def pump() -> None:
        try:
            for item in lines:
                pending.put(item)
        except BaseException as exc:  # 读失败要把异常还给消费方，而不是悄悄结束
            pending.put(exc)
        finally:
            pending.put(_EOF)

    threading.Thread(target=pump, name="sse-pump", daemon=True).start()
    while True:
        try:
            item = pending.get(timeout=heartbeat)
        except queue.Empty:
            yield _HEARTBEAT
            continue
        if item is _EOF:
            return
        if isinstance(item, BaseException):
            raise item
        yield item


def _iter_sse(lines: Iterable[Any]) -> Iterator[SSEEvent]:
    event_name = ""
    data_parts: List[str] = []
    raw_parts: List[str] = []

    def flush() -> Optional[SSEEvent]:
        nonlocal event_name, data_parts, raw_parts
        if not data_parts and not event_name:
            return None
        payload = "\n".join(data_parts)
        event = SSEEvent(event_name or "message", payload, "\n".join(raw_parts))
        event_name, data_parts, raw_parts = "", [], []
        return event

    for raw in lines:
        if raw is _HEARTBEAT:
            yield SSEEvent(HEARTBEAT_EVENT, "", ": keepalive")
            continue
        if isinstance(raw, (bytes, bytearray)):
            try:
                line = bytes(raw).decode("utf-8", "replace")
            except Exception:  # pragma: no cover - defensive
                continue
        else:
            line = str(raw)
        line = line.rstrip("\r\n")
        if not line:
            pending = flush()
            if pending is not None:
                yield pending
                if pending.data == DONE:
                    return
            continue
        if line.startswith(":"):
            # comment / heartbeat
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            raw_parts.append(line)
            continue
        if line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            data_parts.append(value)
            raw_parts.append(line)
            continue
        # Unknown field (id:, retry:, or a stray JSON line from a misbehaving
        # upstream).  Treat a bare ``{...}`` line as data so we still parse it.
        if line.startswith("{") and not data_parts:
            data_parts.append(line)
            raw_parts.append(line)

    pending = flush()
    if pending is not None:
        yield pending


def sse_bytes(payload: Any, event: Optional[str] = None) -> bytes:
    """Encode a single SSE frame.  ``payload`` may be a dict or a raw string."""
    if isinstance(payload, (dict, list)):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    else:
        body = str(payload)
    frame = ""
    if event:
        frame += f"event: {event}\n"
    frame += f"data: {body}\n\n"
    return frame.encode("utf-8")


def sse_done() -> bytes:
    return b"data: [DONE]\n\n"


class ChunkBuilder:
    """Accumulates OpenAI ``chat.completion.chunk`` objects.

    The gateway produces many tiny fragments (a token of text, a delta of a
    tool argument).  Emitting one SSE frame per fragment would triple the
    response size, so fragments are coalesced per flush window.
    """

    def __init__(self, completion_id: str, model: str, created: Optional[int] = None) -> None:
        self.id = completion_id
        self.model = model
        import time

        self.created = int(created if created is not None else time.time())
        self._text: List[str] = []
        self._reasoning: List[str] = []

    def text(self, fragment: str) -> None:
        if fragment:
            self._text.append(fragment)

    def reasoning(self, fragment: str) -> None:
        if fragment:
            self._reasoning.append(fragment)

    def _base(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
        }

    def delta(self, delta: Dict[str, Any], finish_reason: Optional[str] = None) -> Dict[str, Any]:
        chunk = self._base()
        chunk["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
        return chunk

    def flush(self) -> List[Dict[str, Any]]:
        """Drain pending text/reasoning into at most two chunks."""
        out: List[Dict[str, Any]] = []
        if self._reasoning:
            out.append(self.delta({"reasoning_content": "".join(self._reasoning)}))
            self._reasoning = []
        if self._text:
            out.append(self.delta({"content": "".join(self._text)}))
            self._text = []
        return out

    def finish(self, reason: str, usage: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        out = self.flush()
        chunk = self.delta({}, reason)
        if usage:
            chunk["usage"] = usage
        out.append(chunk)
        return out


def parse_model_list(payload: Any) -> List[Dict[str, Any]]:
    """Normalise the various upstream model-list envelopes into a list.

    The endpoints return ``{"data": {"models": [...]}}``, ``{"models": []}`` or
    a bare list depending on the realm; all three appear in the reference
    implementations.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("models", "data", "list", "items"):
        node = payload.get(key)
        if isinstance(node, list):
            return [item for item in node if isinstance(item, dict)]
        if isinstance(node, dict):
            nested = parse_model_list(node)
            if nested:
                return nested
    return []
