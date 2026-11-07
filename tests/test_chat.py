"""adapters/chat.py 的测试：每条改写都对应一次实测过的上游 400。"""

from __future__ import annotations

import json
import unittest

from wb2api import sse
from wb2api.adapters import chat

LEAK = (
    "<\uff5c\uff5cDSML\uff5c\uff5c calls>\n"
    '<\uff5c\uff5cDSML\uff5c\uff5c invoke name="get_weather">\n'
    '<\uff5c\uff5cDSML\uff5c\uff5c parameter name="city" string="true">\u5317\u4eac</\uff5c\uff5cDSML\uff5c\uff5c parameter>\n'
    "</\uff5c\uff5cDSML\uff5c\uff5c invoke>\n"
    "</\uff5c\uff5cDSML\uff5c\uff5c calls>"
)


def frames(*objs, done=True):
    """把若干上游分片编成 SSE 行（``iter_sse`` 靠空行切帧）。"""
    text = "".join("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n" for obj in objs)
    if done:
        text += "data: [DONE]\n\n"
    return text.splitlines()


def collect(raw_frames):
    out = []
    for payload in raw_frames:
        text = payload.decode("utf-8")
        for event in sse.iter_sse(text.splitlines()):
            if event.data == "[DONE]":
                continue
            out.append(json.loads(event.data))
    return out


class PrepareRequestTests(unittest.TestCase):
    def test_stream_is_forced(self) -> None:
        self.assertTrue(chat.prepare_request({"model": "m", "stream": False})["stream"])
        self.assertTrue(chat.prepare_request({"model": "m"})["stream"])

    def test_stream_options_default_only_when_missing(self) -> None:
        self.assertEqual({"include_usage": True}, chat.prepare_request({"model": "m"})["stream_options"])
        kept = chat.prepare_request({"model": "m", "stream_options": {"include_usage": False}})
        self.assertEqual({"include_usage": False}, kept["stream_options"])

    def test_max_completion_tokens_translated(self) -> None:
        out = chat.prepare_request({"model": "m", "max_completion_tokens": 512})
        self.assertEqual(512, out["max_tokens"])
        self.assertNotIn("max_completion_tokens", out)

    def test_explicit_max_tokens_wins(self) -> None:
        out = chat.prepare_request({"model": "m", "max_tokens": 64, "max_completion_tokens": 512})
        self.assertEqual(64, out["max_tokens"])

    def test_gpt_small_max_tokens_clamped(self) -> None:
        out = chat.prepare_request({"model": "gpt-4o", "max_tokens": 4})
        self.assertEqual(chat.GPT_MIN_MAX_TOKENS, out["max_tokens"])

    def test_stop_string_becomes_list(self) -> None:
        self.assertEqual(["END"], chat.prepare_request({"model": "m", "stop": "END"})["stop"])
        self.assertEqual(["a", "b"], chat.prepare_request({"model": "m", "stop": ["a", "", "b"]})["stop"])
        self.assertNotIn("stop", chat.prepare_request({"model": "m", "stop": 5}))
        self.assertNotIn("stop", chat.prepare_request({"model": "m", "stop": []}))

    def test_tool_choice_object_becomes_string(self) -> None:
        out = chat.prepare_request({
            "model": "m",
            "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
            "tool_choice": {"type": "function", "function": {"name": "f"}},
        })
        self.assertEqual("auto", out["tool_choice"])

    def test_tool_choice_for_dropped_tool_falls_back_to_auto(self) -> None:
        out = chat.prepare_request({
            "model": "m",
            "tools": [{"type": "function", "function": {"name": "bad name", "parameters": {"type": "object"}}}],
            "tool_choice": "bad name",
        })
        self.assertNotIn("tools", out)
        self.assertNotIn("tool_choice", out)

    def test_tool_names_and_duplicates_filtered(self) -> None:
        out = chat.prepare_request({
            "model": "m",
            "tools": [
                {"type": "function", "function": {"name": "good", "parameters": {"type": "object"}}},
                {"type": "function", "function": {"name": "good", "parameters": {"type": "object"}}},
                {"type": "function", "function": {"name": "has space", "parameters": {"type": "object"}}},
                {"type": "function", "function": {"name": "x" * 65, "parameters": {"type": "object"}}},
            ],
        })
        self.assertEqual(["good"], [t["function"]["name"] for t in out["tools"]])

    def test_schema_repair(self) -> None:
        out = chat.prepare_request({
            "model": "m",
            "tools": [{
                "type": "function",
                "function": {
                    "name": "f",
                    "parameters": {
                        "type": "dict",
                        "properties": {
                            "a": {"type": "str"},
                            "b": {"type": "list", "items": {"type": "int"}},
                        },
                        "required": ["a", "missing"],
                    },
                },
            }],
        })
        params = out["tools"][0]["function"]["parameters"]
        self.assertEqual("object", params["type"])
        self.assertEqual("string", params["properties"]["a"]["type"])
        self.assertEqual("array", params["properties"]["b"]["type"])
        self.assertEqual("integer", params["properties"]["b"]["items"]["type"])
        self.assertEqual(["a"], params["required"])

    def test_underscore_keys_dropped(self) -> None:
        out = chat.prepare_request({"model": "m", "_client": {"trace": 1}, "temperature": 0.2})
        self.assertNotIn("_client", out)
        self.assertEqual(0.2, out["temperature"])

    def test_developer_role_becomes_system(self) -> None:
        out = chat.prepare_request({"model": "m", "messages": [{"role": "developer", "content": "hi"}]})
        self.assertEqual("system", out["messages"][0]["role"])
        self.assertEqual("system", out["messages"][0]["role"])

    def test_default_system_prompt_for_reasoning_models(self) -> None:
        out = chat.prepare_request({"model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual("system", out["messages"][0]["role"])
        self.assertEqual(chat.DEFAULT_SYSTEM_PROMPT, out["messages"][0]["content"])

    def test_default_system_prompt_not_injected_for_plain_models(self) -> None:
        out = chat.prepare_request({"model": "composer-1", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual("user", out["messages"][0]["role"])

    def test_thinking_and_effort_injected_together(self) -> None:
        out = chat.prepare_request({"model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual({"type": "enabled"}, out["thinking"])
        self.assertEqual(chat.DEFAULT_REASONING_EFFORT, out["reasoning_effort"])

    def test_thinking_off_is_respected(self) -> None:
        out = chat.prepare_request({
            "model": "deepseek-v3",
            "reasoning_effort": "off",
            "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertNotIn("thinking", out)
        self.assertNotIn("reasoning_effort", out)

    def test_thinking_cannot_be_disabled_by_metadata(self) -> None:
        meta = {"deepseek-v3": {"reasoning": True, "canDisableThinking": False, "defaultEffort": "medium"}}
        out = chat.prepare_request({
            "model": "deepseek-v3",
            "reasoning_effort": "off",
            "messages": [{"role": "user", "content": "hi"}],
        }, meta)
        self.assertEqual({"type": "enabled"}, out["thinking"])
        self.assertEqual("medium", out["reasoning_effort"])

    def test_plain_model_strips_thinking(self) -> None:
        out = chat.prepare_request({
            "model": "composer-1",
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertNotIn("thinking", out)
        self.assertNotIn("reasoning_effort", out)

    def test_effort_degrades_to_supported(self) -> None:
        meta = {"deepseek-v3": {"reasoning": True, "supportedEfforts": ["low", "medium"]}}
        out = chat.prepare_request({
            "model": "deepseek-v3",
            "reasoning_effort": "high",
            "messages": [{"role": "user", "content": "hi"}],
        }, meta)
        self.assertEqual("medium", out["reasoning_effort"])

    def test_assistant_history_gets_reasoning_backfilled(self) -> None:
        out = chat.prepare_request({
            "model": "deepseek-v3",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "again"},
            ],
        })
        assistant = out["messages"][2]
        self.assertEqual("assistant", assistant["role"])
        self.assertEqual(" ", assistant["reasoning_content"])
        self.assertEqual(" ", assistant["reasoning"])

    def test_existing_reasoning_is_preserved(self) -> None:
        out = chat.prepare_request({
            "model": "deepseek-v3",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello", "reasoning_content": "thinking..."},
            ],
        })
        self.assertEqual("thinking...", out["messages"][2]["reasoning"])

    def test_orphan_tool_results_removed(self) -> None:
        out = chat.prepare_request({
            "model": "m",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "tool", "tool_call_id": "call_x", "content": "result"},
            ],
        })
        self.assertEqual(["user"], [m["role"] for m in out["messages"]])

    def test_input_is_not_mutated(self) -> None:
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "tools": []}
        snapshot = json.dumps(body, sort_keys=True)
        chat.prepare_request(body)
        self.assertEqual(snapshot, json.dumps(body, sort_keys=True))


class StreamTests(unittest.TestCase):
    def test_content_frames_pass_through(self) -> None:
        raws = list(chat.stream_frames(
            frames(
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "he"}}]},
                {"choices": [{"index": 0, "delta": {"content": "llo"}, "finish_reason": "stop"}]},
            ),
            "cmpl-1", "m",
        ))
        chunks = collect(raws)
        self.assertEqual("he", chunks[0]["choices"][0]["delta"]["content"])
        self.assertEqual("llo", chunks[1]["choices"][0]["delta"]["content"])

    def test_leaked_markup_becomes_tool_calls(self) -> None:
        raws = list(chat.stream_frames(
            frames(
                {"choices": [{"index": 0, "delta": {"content": LEAK[:40]}}]},
                {"choices": [{"index": 0, "delta": {"content": LEAK[40:]}, "finish_reason": "stop"}]},
            ),
            "cmpl-1", "m", tool_names=["get_weather"],
        ))
        chunks = collect(raws)
        deltas = [c["choices"][0]["delta"] for c in chunks if c.get("choices")]
        tool_deltas = [d for d in deltas if d.get("tool_calls")]
        self.assertEqual(1, len(tool_deltas))
        call = tool_deltas[0]["tool_calls"][0]
        self.assertEqual("get_weather", call["function"]["name"])
        self.assertIn("北京", call["function"]["arguments"])
        finish = [c["choices"][0]["finish_reason"] for c in chunks if c.get("choices")]
        self.assertIn("tool_calls", finish)
        visible = "".join(d.get("content") or "" for d in deltas)
        self.assertNotIn("DSML", visible)

    def test_empty_stream_still_valid(self) -> None:
        chunks = collect(chat.stream_frames(frames(), "cmpl-1", "m"))
        self.assertEqual("stop", chunks[-1]["choices"][0]["finish_reason"])

    def test_tool_call_arguments_accumulate(self) -> None:
        chunks = collect(chat.stream_frames(
            frames(
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "c1", "function": {"name": "f", "arguments": ""}}]}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": '{"a"'}}]}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": ":1}"}}]}}]},
            ),
            "cmpl-1", "m",
        ))
        calls = [d for c in chunks for d in [c["choices"][0]["delta"] if c.get("choices") else {}] if d.get("tool_calls")]
        self.assertEqual(3, len(calls))


class AggregateTests(unittest.TestCase):
    def test_aggregate_builds_message(self) -> None:
        out = chat.aggregate(
            frames(
                {"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "think"}}]},
                {"choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": "stop"}]},
                {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 4}}},
            ),
            "cmpl-1", "m",
        )
        message = out["choices"][0]["message"]
        self.assertEqual("hello", message["content"])
        self.assertEqual("think", message["reasoning_content"])
        self.assertEqual(15, out["usage"]["total_tokens"])
        self.assertEqual(4, out["usage"]["prompt_tokens_details"]["cached_tokens"])

    def test_aggregate_converts_leaked_markup(self) -> None:
        out = chat.aggregate(
            frames({"choices": [{"index": 0, "delta": {"content": "let me check\n" + LEAK}, "finish_reason": "stop"}]}),
            "cmpl-1", "m", tool_names=["get_weather"],
        )
        message = out["choices"][0]["message"]
        self.assertEqual("let me check\n", message["content"])
        self.assertEqual("tool_calls", out["choices"][0]["finish_reason"])
        self.assertEqual("get_weather", message["tool_calls"][0]["function"]["name"])

    def test_aggregate_cached_token_aliases(self) -> None:
        out = chat.aggregate(
            frames({"choices": [{"index": 0, "delta": {"content": "x"}}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 1, "prompt_cache_hit_tokens": 5}}),
            "cmpl-1", "m",
        )
        self.assertEqual(5, out["usage"]["prompt_tokens_details"]["cached_tokens"])


if __name__ == "__main__":
    unittest.main()
