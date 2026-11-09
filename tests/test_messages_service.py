"""``POST /v1/messages`` 的端到端（服务层）：翻译 + 选号 + 记账一起跑。

翻译层自己的单测在 ``tests/test_messages.py``；这里只验证"编排没接错线"：
流式事件能不能出来、非流式形状对不对、错误信封是不是 Anthropic 形状。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb2api.config import Config
from wb2api.service import GatewayService
from wb2api.store import Account, Store
from tests.test_service import FakeClient, FakeResponse, chat_chunk, frame, sse_lines


class MessagesServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-msg-")
        self.cfg = Config(None, {"data_dir": self.tmp, "panel": {"enabled": False}})
        store = Store(self.tmp)
        store.upsert_account(Account(uid="u1", id="u1", realm="cn", access_token="tok-u1",
                                     refresh_token="rt-u1", expires_at=time.time() + 86400))

    def service(self, script):
        client = FakeClient(script)
        return GatewayService(self.cfg, Store(self.tmp), client=client), client

    def request(self, payload):
        from wb2api.httpserver import Request

        return Request("POST", "/v1/messages", {}, {}, json.dumps(payload).encode(), ("1.2.3.4", 1))

    def body(self, **extra):
        payload = {
            "model": "deepseek-v3",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        }
        payload.update(extra)
        return payload

    def test_stream_produces_anthropic_events(self):
        svc, client = self.service([
            FakeResponse(sse_lines(chat_chunk("Hel"), chat_chunk("lo"),
                                   chat_chunk("", finish="stop"), frame("[DONE]"))),
        ])
        resp = svc.messages(self.request(self.body(stream=True)))
        text = b"".join(resp.stream).decode("utf-8")
        events = [line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("event: ")]
        self.assertEqual(events[0], "message_start")
        self.assertEqual(events[-1], "message_stop")
        self.assertIn("content_block_delta", events)
        self.assertIn('"text_delta"', text)
        # 上游收到的是 OpenAI 形状：max_tokens 原样、stream_options 由 chat 适配器补
        sent = json.loads(client.calls[0]["body"].decode("utf-8"))
        user = [item for item in sent["messages"] if item["role"] == "user"]
        self.assertEqual(user[0]["content"], "hi")
        self.assertTrue(sent["stream"])

    def test_non_stream_maps_to_message_shape(self):
        svc, _client = self.service([
            FakeResponse(sse_lines(
                chat_chunk("hi there"),
                chat_chunk("", finish="stop", usage={"prompt_tokens": 5, "completion_tokens": 3}),
                frame("[DONE]"),
            )),
        ])
        resp = svc.messages(self.request(self.body()))
        message = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(message["type"], "message")
        self.assertEqual(message["content"], [{"type": "text", "text": "hi there"}])
        self.assertEqual(message["usage"], {"input_tokens": 5, "output_tokens": 3})
        self.assertEqual(message["stop_reason"], "end_turn")

    def test_no_account_fails_with_service_error(self):
        empty = tempfile.mkdtemp(prefix="wb2a-msg-empty-")
        cfg = Config(None, {"data_dir": empty, "panel": {"enabled": False}})
        client = FakeClient([])
        svc = GatewayService(cfg, Store(empty), client=client)
        with self.assertRaises(Exception) as ctx:
            svc.messages(self.request(self.body()))
        self.assertEqual(getattr(ctx.exception, "code", ""), "no_account")


class AnthropicGuardTest(unittest.TestCase):
    def test_error_envelope_shape(self):
        from wb2api.routes_openai import anthropic_guard
        from wb2api.service import ServiceError

        @anthropic_guard
        def boom(req):
            raise ServiceError("bad things", 429, "rate_limited", "rate_limit_error", retry_after=3)

        response = boom(None)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload["type"], "error")
        self.assertEqual(payload["error"]["type"], "rate_limit_error")
        self.assertEqual(payload["error"]["message"], "bad things")
        self.assertEqual(response.headers.get("Retry-After"), "3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
