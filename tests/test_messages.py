"""Anthropic Messages 翻译层：请求摊平、响应成型、流式事件顺序。

这三件事错一个，Claude Code 那头就是"无响应 / 空回复 / 卡住"，而且很难从客户端
日志看出来。所以这里把每种块的形状和事件顺序都钉死。
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb2api.adapters import messages as m


class RequestTest(unittest.TestCase):
    def test_system_and_plain_text(self):
        chat = m.to_chat({
            "model": "claude-test",
            "max_tokens": 128,
            "system": "be brief",
            "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(chat["messages"][0], {"role": "system", "content": "be brief"})
        self.assertEqual(chat["messages"][1], {"role": "user", "content": "hi"})
        self.assertEqual(chat["max_tokens"], 128)
        self.assertFalse(chat["stream"])

    def test_system_blocks_are_flattened(self):
        chat = m.to_chat({
            "model": "m",
            "system": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
            "messages": [{"role": "user", "content": "x"}],
        })
        self.assertEqual(chat["messages"][0]["content"], "ab")

    def test_image_and_text_parts(self):
        chat = m.to_chat({
            "model": "m",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
                ],
            }],
        })
        parts = chat["messages"][0]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "what is this"})
        self.assertEqual(parts[1]["image_url"]["url"], "data:image/png;base64,AAA")

    def test_tool_use_and_tool_result(self):
        chat = m.to_chat({
            "model": "m",
            "messages": [
                {"role": "user", "content": "weather?"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "let me check"},
                    {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "sz"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "call_1", "content": [{"type": "text", "text": "sunny"}]},
                ]},
            ],
        })
        assistant = chat["messages"][1]
        self.assertEqual(assistant["content"], "let me check")
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_1")
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"]), {"city": "sz"})
        tool_message = chat["messages"][2]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_call_id"], "call_1")
        self.assertEqual(tool_message["content"], "sunny")

    def test_thinking_history_becomes_reasoning_content(self):
        chat = m.to_chat({
            "model": "m",
            "messages": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "answer"},
                ]},
            ],
        })
        self.assertEqual(chat["messages"][1]["content"], "answer")
        self.assertEqual(chat["messages"][1]["reasoning_content"], "hmm")

    def test_tools_and_choice_become_strings(self):
        chat = m.to_chat({
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"name": "t1", "description": "d", "input_schema": {"type": "object", "properties": {}}}],
            "tool_choice": {"type": "any"},
        })
        self.assertEqual(chat["tools"][0]["function"]["name"], "t1")
        self.assertEqual(chat["tool_choice"], "auto", "上游只吃字符串形式的 tool_choice")

    def test_stop_sequences_become_list(self):
        chat = m.to_chat({
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "stop_sequences": ["END"],
        })
        self.assertEqual(chat["stop"], ["END"])


class ResponseTest(unittest.TestCase):
    def test_text_message(self):
        message = m.from_chat({
            "id": "chatcmpl-abc",
            "model": "m",
            "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }, "m")
        self.assertEqual(message["type"], "message")
        self.assertEqual(message["content"], [{"type": "text", "text": "hi"}])
        self.assertEqual(message["stop_reason"], "end_turn")
        self.assertEqual(message["usage"], {"input_tokens": 10, "output_tokens": 2})

    def test_tool_calls_become_tool_use_blocks(self):
        message = m.from_chat({
            "choices": [{
                "message": {
                    "content": "",
                    "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": '{"a":1}'}}],
                },
                "finish_reason": "tool_calls",
            }],
        }, "m")
        block = message["content"][0]
        self.assertEqual(block["type"], "tool_use")
        self.assertEqual(block["input"], {"a": 1})
        self.assertEqual(message["stop_reason"], "tool_use")

    def test_length_maps_to_max_tokens(self):
        message = m.from_chat({"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]})
        self.assertEqual(message["stop_reason"], "max_tokens")


def events_of(items):
    return [(event, json.loads(data)) for event, data in items]


class StreamTest(unittest.TestCase):
    def test_event_order_for_plain_text(self):
        stream = m.MessageStream("m", message_id="msg_1", input_tokens=7)
        items = []
        items += stream.start()
        items += stream.feed({"choices": [{"delta": {"content": "He"}}]})
        items += stream.feed({"choices": [{"delta": {"content": "llo"}, "finish_reason": "stop"}]})
        items += stream.feed({"choices": [], "usage": {"completion_tokens": 2}})
        items += stream.finish()
        parsed = events_of(items)
        names = [name for name, _ in parsed]
        self.assertEqual(names[0], "message_start")
        self.assertEqual(parsed[0][1]["message"]["usage"]["input_tokens"], 7)
        self.assertEqual(names[1], "content_block_start")
        self.assertEqual(parsed[1][1]["content_block"]["type"], "text")
        deltas = [data["delta"]["text"] for name, data in parsed if name == "content_block_delta"]
        self.assertEqual("".join(deltas), "Hello")
        self.assertEqual(names[-2:], ["message_delta", "message_stop"])
        self.assertEqual(parsed[-2][1]["delta"]["stop_reason"], "end_turn")
        self.assertEqual(parsed[-2][1]["usage"]["output_tokens"], 2)

    def test_tool_arguments_stream_as_partial_json(self):
        stream = m.MessageStream("m")
        items = list(stream.start())
        items += stream.feed({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "f", "arguments": '{"a"'}},
        ]}}]})
        items += stream.feed({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": ":1}"}},
        ]}, "finish_reason": "tool_calls"}]})
        items += stream.finish()
        parsed = events_of(items)
        starts = [data for name, data in parsed if name == "content_block_start"]
        self.assertEqual(starts[0]["content_block"]["type"], "tool_use")
        partials = [data["delta"]["partial_json"] for name, data in parsed if name == "content_block_delta"]
        self.assertEqual("".join(partials), '{"a":1}')
        self.assertEqual(parsed[-2][1]["delta"]["stop_reason"], "tool_use")

    def test_reasoning_streams_as_thinking_block(self):
        stream = m.MessageStream("m")
        items = list(stream.start())
        items += stream.feed({"choices": [{"delta": {"reasoning_content": "think"}}]})
        items += stream.feed({"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]})
        items += stream.finish()
        parsed = events_of(items)
        types = [data["content_block"]["type"] for name, data in parsed if name == "content_block_start"]
        self.assertEqual(types, ["thinking", "text"])
        # 两个块各占一个 index，且块序号递增——Claude 客户端按 index 归并增量
        indexes = [data["index"] for name, data in parsed if name == "content_block_start"]
        self.assertEqual(indexes, [0, 1])

    def test_error_frame_never_pretends_success(self):
        stream = m.MessageStream("m")
        list(stream.start())
        items = stream.error("boom")
        parsed = events_of(items)
        self.assertEqual(parsed[0][0], "error")
        self.assertEqual(parsed[0][1]["error"]["message"], "boom")
        self.assertEqual(stream.finish(), [], "报过错就不能再发 message_stop")

    def test_count_tokens_is_positive_and_scales(self):
        small = m.count_tokens({"messages": [{"role": "user", "content": "hi"}]})
        big = m.count_tokens({"messages": [{"role": "user", "content": "hi" * 500}]})
        self.assertGreaterEqual(small, 1)
        self.assertGreater(big, small)


if __name__ == "__main__":
    unittest.main(verbosity=2)
