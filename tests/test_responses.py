"""``/v1/responses``（OpenAI Responses 协议）的翻译与编排。

分成两层测：翻译是纯函数（好断言），编排只验证"接没接错线"——事件能不能出来、
非流式形状对不对、没有账号时错误信封是不是 OpenAI 的。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb2api.adapters import responses as adapter
from wb2api.config import Config
from wb2api.service import GatewayService
from wb2api.store import Account, Store
from tests.test_service import FakeClient, FakeResponse, chat_chunk, frame, sse_lines


class ToChatTest(unittest.TestCase):
    def test_instructions_become_system(self):
        chat = adapter.to_chat({
            "model": "m",
            "instructions": "你是助手",
            "input": "你好",
        })
        self.assertEqual(chat["messages"][0], {"role": "system", "content": "你是助手"})
        self.assertEqual(chat["messages"][1], {"role": "user", "content": "你好"})

    def test_input_items_are_flattened(self):
        chat = adapter.to_chat({
            "model": "m",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "看这个"}]},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "看到"}]},
                {"type": "message", "role": "user", "content": "继续"},
            ],
        })
        self.assertEqual([m["role"] for m in chat["messages"]], ["user", "assistant", "user"])
        self.assertEqual(chat["messages"][0]["content"], "看这个")

    def test_function_call_and_output_round_trip(self):
        chat = adapter.to_chat({
            "model": "m",
            "input": [
                {"type": "message", "role": "user", "content": "天气"},
                {"type": "function_call", "call_id": "call_1", "name": "get_weather",
                 "arguments": "{\"city\":\"北京\"}"},
                {"type": "function_call_output", "call_id": "call_1", "output": "晴"},
            ],
        })
        assistant = [m for m in chat["messages"] if m["role"] == "assistant"][0]
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "get_weather")
        tool = [m for m in chat["messages"] if m["role"] == "tool"][0]
        self.assertEqual(tool["tool_call_id"], "call_1")
        self.assertEqual(tool["content"], "晴")

    def test_function_call_merges_into_previous_assistant_message(self):
        chat = adapter.to_chat({
            "model": "m",
            "input": [
                {"type": "message", "role": "user", "content": "x"},
                {"type": "message", "role": "assistant", "content": "让我查"},
                {"type": "function_call", "call_id": "c1", "name": "f", "arguments": {}},
            ],
        })
        assistants = [m for m in chat["messages"] if m["role"] == "assistant"]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0]["content"], "让我查")
        self.assertEqual(assistants[0]["tool_calls"][0]["function"]["arguments"], "{}")

    def test_limits_and_tools(self):
        chat = adapter.to_chat({
            "model": "m",
            "input": "hi",
            "max_output_tokens": 128,
            "temperature": 0.3,
            "tools": [{"type": "function", "name": "f", "description": "d",
                       "parameters": {"type": "object", "properties": {"a": {"type": "string"}}}}],
            "tool_choice": {"type": "function", "name": "f"},
            "text": {"format": {"type": "json_object"}},
            "reasoning": {"effort": "low"},
        })
        self.assertEqual(chat["max_tokens"], 128)
        self.assertEqual(chat["temperature"], 0.3)
        self.assertEqual(chat["tools"][0]["function"]["name"], "f")
        self.assertEqual(chat["tool_choice"], {"type": "function", "function": {"name": "f"}})
        self.assertEqual(chat["response_format"], {"type": "json_object"})
        self.assertEqual(chat["reasoning_effort"], "low")

    def test_unknown_tool_types_are_skipped(self):
        chat = adapter.to_chat({"model": "m", "input": "hi",
                                "tools": [{"type": "web_search"}, {"type": "function", "name": "ok"}]})
        self.assertEqual(len(chat["tools"]), 1)


class FromChatTest(unittest.TestCase):
    def _completion(self, **message):
        body = {"choices": [{"message": message, "finish_reason": message.pop("finish_reason", "stop")}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 5,
                          "completion_tokens_details": {"reasoning_tokens": 3},
                          "prompt_tokens_details": {"cached_tokens": 2}}}
        return body

    def test_text_output(self):
        result = adapter.from_chat(self._completion(content="你好"), "m", response_id="resp_1")
        self.assertEqual(result["object"], "response")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output_text"], "你好")
        self.assertEqual(result["output"][0]["content"][0]["type"], "output_text")
        self.assertEqual(result["usage"]["input_tokens"], 7)
        self.assertEqual(result["usage"]["input_tokens_details"]["cached_tokens"], 2)
        self.assertEqual(result["usage"]["output_tokens_details"]["reasoning_tokens"], 3)

    def test_reasoning_item_comes_first(self):
        result = adapter.from_chat(
            self._completion(content="答案", reasoning_content="想一想"), "m", response_id="resp_1"
        )
        self.assertEqual([item["type"] for item in result["output"]], ["reasoning", "message"])
        self.assertEqual(result["output"][0]["summary"][0]["text"], "想一想")

    def test_tool_calls_become_function_call_items(self):
        result = adapter.from_chat(
            self._completion(content="", tool_calls=[
                {"id": "call_1", "type": "function",
                 "function": {"name": "f", "arguments": "{\"a\":1}"}},
            ], finish_reason="tool_calls"),
            "m",
            response_id="resp_1",
        )
        kinds = [item["type"] for item in result["output"]]
        self.assertIn("function_call", kinds)
        call = [item for item in result["output"] if item["type"] == "function_call"][0]
        self.assertEqual(call["call_id"], "call_1")
        self.assertEqual(call["arguments"], "{\"a\":1}")

    def test_length_means_incomplete(self):
        result = adapter.from_chat(
            {"choices": [{"message": {"content": "半截"}, "finish_reason": "length"}], "usage": {}},
            "m",
            response_id="resp_1",
        )
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["incomplete_details"], {"reason": "max_output_tokens"})


class StreamTest(unittest.TestCase):
    def _names(self, events):
        return [name for name, _data in events]

    def test_text_sequence(self):
        stream = adapter.ResponseStream("m", response_id="resp_abcdefghijkl", created_at=1)
        events = list(stream.start())
        events += stream.feed({"choices": [{"delta": {"content": "你"}}]})
        events += stream.feed({"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
        events += stream.finish("stop")
        names = self._names(events)
        self.assertEqual(names[0], "response.created")
        self.assertEqual(names[1], "response.in_progress")
        self.assertIn("response.output_text.delta", names)
        self.assertEqual(names[-1], "response.completed")
        self.assertEqual([n for n in names if n == "response.output_text.delta"], ["response.output_text.delta"] * 2)
        # 序号必须单调，客户端靠它排序
        seqs = [data["sequence_number"] for _n, data in events]
        self.assertEqual(seqs, sorted(seqs))
        final = events[-1][1]["response"]
        self.assertEqual(final["usage"]["input_tokens"], 3)
        self.assertEqual(final["usage"]["output_tokens"], 2)

    def test_reasoning_opens_its_own_item(self):
        stream = adapter.ResponseStream("m", response_id="resp_x")
        list(stream.start())
        events = stream.feed({"choices": [{"delta": {"reasoning_content": "想"}}]})
        events += stream.feed({"choices": [{"delta": {"content": "答"}}]})
        events += stream.finish("stop")
        names = self._names(events)
        self.assertIn("response.reasoning_summary_text.delta", names)
        self.assertLess(names.index("response.reasoning_summary_text.delta"),
                        names.index("response.output_text.delta"))
        done = [d for n, d in events if n == "response.output_item.done"]
        self.assertEqual([item["item"]["type"] for item in done], ["reasoning", "message"])

    def test_tool_call_arguments_accumulate(self):
        stream = adapter.ResponseStream("m", response_id="resp_x")
        list(stream.start())
        events = stream.feed({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "f", "arguments": "{\"a\""}}]}}]})
        events += stream.feed({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": ":1}"}}]}, "finish_reason": "tool_calls"}]})
        events += stream.finish("tool_calls")
        names = self._names(events)
        self.assertEqual(len([n for n in names if n == "response.function_call_arguments.delta"]), 2)
        done = [d for n, d in events if n == "response.output_item.done"][0]
        self.assertEqual(done["item"]["arguments"], "{\"a\":1}")

    def test_error_never_pretends_success(self):
        stream = adapter.ResponseStream("m", response_id="resp_x")
        list(stream.start())
        events = stream.error("上游炸了", "upstream_error")
        events += stream.finish("stop")  # 出错之后再 finish 必须什么都不发
        self.assertEqual(self._names(events), ["response.failed"])
        self.assertEqual(events[0][1]["response"]["error"]["message"], "上游炸了")

    def test_frames_are_sse(self):
        stream = adapter.ResponseStream("m", response_id="resp_x")
        raw = b"".join(adapter.stream_frames(stream.start()))
        self.assertIn(b"event: response.created\n", raw)
        self.assertNotIn(b"[DONE]", raw)


class ResponsesServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-resp-")
        self.cfg = Config(None, {"data_dir": self.tmp, "panel": {"enabled": False}})
        store = Store(self.tmp)
        store.upsert_account(Account(uid="u1", id="u1", realm="cn", access_token="tok-u1",
                                     refresh_token="rt-u1", expires_at=time.time() + 86400))

    def service(self, script):
        client = FakeClient(script)
        return GatewayService(self.cfg, Store(self.tmp), client=client), client

    def request(self, payload, path="/v1/responses"):
        from wb2api.httpserver import Request

        return Request("POST", path, {}, {}, json.dumps(payload).encode(), ("1.2.3.4", 1))

    def test_stream_emits_responses_events(self):
        svc, client = self.service([
            FakeResponse(sse_lines(chat_chunk("Hel"), chat_chunk("lo"),
                                   chat_chunk("", finish="stop"), frame("[DONE]"))),
        ])
        resp = svc.responses(self.request({"model": "deepseek-v3", "input": "hi", "stream": True}))
        text = b"".join(resp.stream).decode("utf-8")
        events = [line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("event: ")]
        self.assertEqual(events[0], "response.created")
        self.assertEqual(events[-1], "response.completed")
        self.assertIn("response.output_text.delta", events)
        sent = json.loads(client.calls[0]["body"].decode("utf-8"))
        self.assertTrue(sent["stream"])
        self.assertEqual([m for m in sent["messages"] if m["role"] == "user"][0]["content"], "hi")

    def test_non_stream_shape(self):
        svc, _client = self.service([
            FakeResponse(sse_lines(chat_chunk("hi there"),
                                   chat_chunk("", finish="stop", usage={"prompt_tokens": 4,
                                                                        "completion_tokens": 3}),
                                   frame("[DONE]"))),
        ])
        resp = svc.responses(self.request({"model": "deepseek-v3", "input": "hi"}))
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["output_text"], "hi there")
        self.assertEqual(payload["usage"]["output_tokens"], 3)

    def test_no_account_is_503(self):
        svc = GatewayService(self.cfg, Store(tempfile.mkdtemp(prefix="wb2a-empty-")), client=FakeClient([]))
        from wb2api.service import ServiceError

        with self.assertRaises(ServiceError) as ctx:
            svc.responses(self.request({"model": "deepseek-v3", "input": "hi"}))
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, "no_account")

    def test_openai_error_envelope_on_route(self):
        from wb2api.httpserver import Request, Router
        from wb2api.routes_openai import register

        svc = GatewayService(self.cfg, Store(tempfile.mkdtemp(prefix="wb2a-empty2-")), client=FakeClient([]))
        router = Router()
        register(router, svc)
        handler, params, status = router.match("POST", "/v1/responses")
        self.assertEqual(status, 200)
        req = Request("POST", "/v1/responses", {}, {}, b'{"model":"m","input":"hi"}', ("1.2.3.4", 1))
        resp = handler(req, **(params or {}))
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(resp.status, 503)
        self.assertEqual(payload["error"]["code"], "no_account")


class RetryAfterTest(unittest.TestCase):
    """池子里有号、但全在冷却：503 要告诉客户端等多久。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-retry-")
        self.cfg = Config(None, {"data_dir": self.tmp, "panel": {"enabled": False}})

    def test_earliest_ready_reports_shortest_wait(self):
        store = Store(self.tmp)
        now = time.time()
        store.upsert_account(Account(uid="a", id="a", realm="cn", access_token="t",
                                     expires_at=now + 86400, cooldown_until=now + 120))
        store.upsert_account(Account(uid="b", id="b", realm="cn", access_token="t",
                                     expires_at=now + 86400, cooldown_until=now + 30))
        pool = GatewayService(self.cfg, store, client=FakeClient([])).pool
        wait = pool.earliest_ready("cn", "m")
        self.assertTrue(25 <= wait <= 35, wait)

    def test_earliest_ready_none_when_no_candidates(self):
        store = Store(self.tmp)
        store.upsert_account(Account(uid="a", id="a", realm="cn", access_token="t", disabled=True))
        pool = GatewayService(self.cfg, store, client=FakeClient([])).pool
        self.assertIsNone(pool.earliest_ready("cn", "m"))

    def test_no_account_carries_retry_after_header(self):
        from wb2api.httpserver import Request, Router
        from wb2api.routes_openai import register
        from wb2api.service import ServiceError

        store = Store(self.tmp)
        store.upsert_account(Account(uid="a", id="a", realm="cn", access_token="t",
                                     expires_at=time.time() + 86400,
                                     cooldown_until=time.time() + 90))
        svc = GatewayService(self.cfg, store, client=FakeClient([]))
        with self.assertRaises(ServiceError) as ctx:
            svc.chat_completions(Request("POST", "/v1/chat/completions", {}, {},
                                         b'{"model":"deepseek-v3","messages":[{"role":"user","content":"hi"}]}',
                                         ("1.2.3.4", 1)))
        self.assertEqual(ctx.exception.code, "no_account")
        self.assertTrue(60 <= ctx.exception.retry_after <= 95, ctx.exception.retry_after)

        router = Router()
        register(router, svc)
        handler, params, _status = router.match("POST", "/v1/chat/completions")
        resp = handler(Request("POST", "/v1/chat/completions", {}, {},
                              b'{"model":"deepseek-v3","messages":[{"role":"user","content":"hi"}]}',
                              ("1.2.3.4", 1)), **(params or {}))
        headers = {k.lower(): v for k, v in resp.headers.items()}
        self.assertIn("retry-after", headers)
        self.assertGreaterEqual(int(headers["retry-after"]), 60)


if __name__ == "__main__":
    unittest.main()
