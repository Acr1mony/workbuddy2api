"""流式与记账：把上游 SSE 转给客户端，并把这次请求记下来。

从 ``service.py`` 按职责切出来的；``GatewayService`` 多继承把这些方法接回去，
调用方无感。
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from . import identity, sse, util
from .adapters import chat as chat_adapter
from .errors import (
    CODE_EFFORT_UNSUPPORTED,
    CODE_FINGERPRINT,
    CODE_MODEL_FORBIDDEN,
    CODE_MODEL_UNAVAILABLE,
    CODE_PROMPT_TOO_LONG,
    CODE_QUOTA,
    CODE_RESTRICTED,
    CODE_TOOL_PAIRING,
    CODE_UA_REJECTED,
    ServiceError,
    client_error,
    parse_reset_clock,
)
from .log import get_logger
from .markup import MARKUP_HINT
from .upstream import UpstreamError, is_restricted

log = get_logger("relay")

class RelayMixin:
    """转发 + 记账。宿主类需提供 cfg / ledger / metrics / reqlog / store。"""

    def _heartbeat(self) -> float:
        """上游静默多少秒就补一条 `: keepalive`。0（或负数）= 关闭。"""
        try:
            value = float(self.cfg.get("upstream.stream_heartbeat_seconds", 15))
        except (TypeError, ValueError):
            return 15.0
        return value if value > 0 else 0.0

    def _relay_messages(
        self,
        *,
        account: Any,
        response: Any,
        stream: Any,
        model: str,
        realm: str,
        key: Optional[ApiKey],
        started: float,
        stick: str = "",
        request_id: str = "",
        attempts: int = 1,
        alias: str = "",
        key_slot: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Iterator[bytes]:
        """把上游的 OpenAI 流重排成 Anthropic 事件流。"""
        from .adapters import messages as msg_adapter

        idle = float(self.cfg.get("upstream.idle_timeout_seconds", 300))
        usage: Dict[str, Any] = {}
        outcome = "completed"
        error_text = ""
        ttft: Optional[float] = None
        first = True
        try:
            for item in stream.start():
                yield msg_adapter.stream_frames([item]).__next__()
            for parsed in sse.iter_sse(response.iter_text(idle), heartbeat=self._heartbeat()):
                if parsed.event == sse.HEARTBEAT_EVENT:
                    yield sse.HEARTBEAT_BYTES
                    continue
                if parsed.event == "done" or (parsed.data or "").strip() == "[DONE]":
                    break
                raw = (parsed.data or "").strip()
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"] or usage
                    if first:
                        # 第一帧就有 usage 的情况很少，真有时至少别把 input_tokens 记成 0
                        first = False
                if first:
                    ttft = (time.time() - started) * 1000.0
                    first = False
                for item in stream.feed(chunk):
                    yield msg_adapter.stream_frames([item]).__next__()
            for item in stream.finish():
                yield msg_adapter.stream_frames([item]).__next__()
        except GeneratorExit:
            outcome = "client_aborted"
            raise
        except UpstreamError as err:
            outcome = "upstream_aborted"
            error_text = err.message or str(err)
            for item in stream.error("上游流中断：" + error_text[:200], "upstream_stream_error"):
                yield msg_adapter.stream_frames([item]).__next__()
        except Exception as exc:
            outcome = "failed"
            error_text = str(exc)
            log.exception("Messages 流处理失败 %s/%s", realm, model)
            for item in stream.error("网关转发失败：" + error_text[:200], "gateway_error"):
                yield msg_adapter.stream_frames([item]).__next__()
        finally:
            self.pool.release(account)
            self._release_key(key_slot)
            try:
                response.close()
            except Exception:
                pass
            if not usage.get("completion_tokens"):
                usage = dict(usage)
                usage["completion_tokens"] = stream.output_tokens
            if not usage.get("prompt_tokens"):
                usage = dict(usage)
                usage["prompt_tokens"] = stream.input_tokens
            try:
                self._record(
                    started=started, model=model, realm=realm, account=account, key=key,
                    stream=True, outcome=outcome, usage=usage,
                    status=200 if outcome == "completed" else 499, ttft=ttft,
                    error=error_text, request_id=request_id, attempts=attempts,
                    alias=alias, kind="messages", extra=extra,
                )
            except Exception:
                log.exception("记账失败（%s）", model)
            if outcome == "completed":
                try:
                    self.pool.note_success(account, model=model, session_key=stick)
                except Exception:
                    log.exception("标记成功失败（%s）", model)

    # -- 选号 + 出站 --------------------------------------------------------

    def _relay_stream(
        self,
        *,
        account: Any,
        response: Any,
        completion_id: str,
        model: str,
        realm: str,
        key: Optional[ApiKey],
        tool_names: List[str],
        started: float,
        stick: str = "",
        request_id: str = "",
        attempts: int = 1,
        alias: str = "",
        key_slot: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Iterator[bytes]:
        """把上游流翻译成客户端帧。

        进了这个生成器就说明响应头已经发出去了：**再失败也不能换号**，只能把错误
        作为一帧 ``upstream_stream_error`` 告诉客户端，并按已产生的用量记账
        （这些 token 上游已经计费了，装作没发生只会让额度悄悄漏掉）。
        """
        idle = float(self.cfg.get("upstream.idle_timeout_seconds", 300))
        ttft: Optional[float] = None
        first_byte_at = 0.0
        usage: Dict[str, Any] = {}
        outcome = "completed"
        error_text = ""
        try:
            filt = None
            for payload in chat_adapter.stream_frames(
                response.iter_text(idle), completion_id, model, tool_names, heartbeat=self._heartbeat()
            ):
                if payload == sse.HEARTBEAT_BYTES:
                    # 心跳不是"第一个字"：它只证明连接还活着。把它算进 TTFT，
                    # 面板上的首字延迟就会变成"心跳间隔"，越调越好看、越假。
                    yield payload
                    continue
                if not first_byte_at:
                    first_byte_at = time.time()
                    ttft = (first_byte_at - started) * 1000.0
                if _is_usage_frame(payload):
                    usage = _usage_of(payload) or usage
                yield payload
        except GeneratorExit:
            outcome = "client_aborted"
            raise
        except UpstreamError as err:
            outcome = "upstream_aborted"
            error_text = err.message or str(err)
            log.warning("流中断 %s/%s: %s", realm, model, error_text)
            yield sse.sse_bytes(
                {
                    "error": {
                        "message": "上游流中断：" + error_text[:200],
                        "type": "upstream_stream_error",
                        "code": "stream_interrupted",
                    }
                }
            )
        except Exception as exc:  # 解析/网络都可能炸，别让线程带着异常死掉
            outcome = "failed"
            error_text = str(exc)
            log.exception("流处理失败 %s/%s", realm, model)
            yield sse.sse_bytes(
                {
                    "error": {
                        "message": "网关转发失败：" + error_text[:200],
                        "type": "gateway_error",
                        "code": "stream_failed",
                    }
                }
            )
        finally:
            # 顺序很重要：先松开槽位，再关连接、记账。记账要落盘，磁盘满/权限问题
            # 抛出来时槽位已经还回去了，不会把账号永久占死。
            self.pool.release(account)
            self._release_key(key_slot)
            try:
                response.close()
            except Exception:
                pass
            try:
                self._record(
                    started=started, model=model, realm=realm, account=account, key=key,
                    stream=True, outcome=outcome, usage=usage, status=200,
                    ttft=ttft, error=error_text,
                    request_id=request_id, attempts=attempts, alias=alias,
                    extra=extra,
                )
            except Exception:
                log.exception("记账失败（%s）", model)
            if outcome == "completed":
                # 必须用**真实**会话键绑定亲和：流式是主路径，这里传空键等于
                # 从来没有亲和——同一段对话会被摊到每台"设备"上，既漏缓存也留关联痕迹。
                try:
                    self.pool.note_success(account, model=model, session_key=stick)
                except Exception:
                    log.exception("绑定会话亲和失败（%s）", model)

    # -- 记账 ---------------------------------------------------------------

    def _record(
        self,
        *,
        started: float,
        model: str,
        realm: str,
        account: Any,
        key: Optional[ApiKey],
        stream: bool,
        outcome: str,
        usage: Dict[str, Any],
        status: int,
        ttft: Optional[float] = None,
        error: str = "",
        request_id: str = "",
        attempts: int = 1,
        alias: str = "",
        kind: str = "chat.completions",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        elapsed = int((time.time() - started) * 1000)
        prompt = _num(usage.get("prompt_tokens"))
        completion = _num(usage.get("completion_tokens"))
        reasoning = _num(usage.get("completion_tokens_details", {}).get("reasoning_tokens") if isinstance(usage.get("completion_tokens_details"), dict) else usage.get("reasoning_tokens"))
        cached = _num((usage.get("prompt_tokens_details") or {}).get("cached_tokens") if isinstance(usage.get("prompt_tokens_details"), dict) else usage.get("cached_tokens"))
        total = _num(usage.get("total_tokens")) or (prompt + completion)
        # 上游 usage 里带 credit（这次请求扣了多少积分）。台账要记它，否则面板上
        # 永远显示"花了 0 积分"，用户没法知道钱去哪了。
        credit = _num(usage.get("credit"))
        if not credit:
            credit = _num(usage.get("credits")) or _num(usage.get("credit_cost"))
        event = {
            "at": int(started),
            "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
            "model": model,
            "stream": stream,
            "outcome": outcome,
            "kind": kind,
            "elapsed_ms": elapsed,
            # 非流式没有"首字"可言：写 0 会让分位数统计把一堆 0 算成"快得离谱"，
            # 写 None 让面板显示"—"、让 metrics 直接跳过这个样本。
            "ttft_ms": round(ttft, 1) if ttft is not None else None,
            "prompt_tokens": int(prompt),
            "completion_tokens": int(completion),
            "reasoning_tokens": int(reasoning),
            "cached_tokens": int(cached),
            "total_tokens": int(total),
            "credit": round(float(credit), 6),
            "realm": realm,
            "account": getattr(account, "uid", "") or getattr(account, "id", ""),
            "key": getattr(key, "key", "")[:12] if key is not None else "",
            "status": status,
            "request_id": request_id,
            "attempts": int(attempts),
        }
        if alias and alias != model:
            event["alias"] = alias
        if error:
            event["error"] = error[:200]
        if outcome == "failed":
            event["status"] = status or 502
        if extra:
            # 明细字段（客户端 IP / UA / 入口路径）只进逐请求日志，不进聚合台账：
            # 聚合表要小，明细表要全。
            for name, value in extra.items():
                if value not in (None, ""):
                    event[name] = value
        self.ledger.record(event)
        try:
            self.reqlog.record(event)
        except Exception:
            log.debug("逐请求日志写入失败", exc_info=True)
        # 指标只留最近 N 条（内存里），和台账的 7 天回放是两件事。
        try:
            self.metrics.observe(event)
        except Exception:
            log.debug("metrics observe failed", exc_info=True)
        if key is not None and hasattr(self.store, "touch_key"):
            self.store.touch_key(key.key, int(prompt), int(completion))

    # -- 辅助端点 -----------------------------------------------------------

def _tool_names(body: Dict[str, Any]) -> List[str]:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return []
    names = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.append(function["name"])
    return names

def _mentions_model(message: str) -> bool:
    lowered = message.lower()
    return "model" in lowered or "模型" in message

def _num(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0

def _is_usage_frame(payload: bytes) -> bool:
    return b'"usage"' in payload

def _usage_of(payload: bytes) -> Optional[Dict[str, Any]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            return obj["usage"]
    return None
