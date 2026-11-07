"""sanitize 的行为：只改必须改的字，别把用户的话改坏。

上游审核是逐字匹配，所以这些函数是"能过审"与"400 11-128"之间唯一的挡板；
同时它们是改写用户输入的代码，改写范围必须可解释、可回滚。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb2api.sanitize import (has_fingerprint, sanitize_content, sanitize_messages,
                             sanitize_text, sanitize_tool_calls)


class FingerprintTest(unittest.TestCase):
    def test_detects_claude_code(self):
        self.assertTrue(has_fingerprint("You are Claude Code, Anthropic's official CLI for Claude"))

    def test_detects_billing_header(self):
        self.assertTrue(has_fingerprint("x-anthropic-billing-hdr: session=abc;"))

    def test_detects_codex(self):
        self.assertTrue(has_fingerprint("You are a coding agent running in the Codex CLI"))

    def test_clean_text(self):
        self.assertFalse(has_fingerprint("请帮我写一个快速排序"))
        self.assertFalse(has_fingerprint("You are a helpful assistant."))


class SanitizeTextTest(unittest.TestCase):
    def test_rewrites_claude_code_sentence(self):
        text = "You are Claude Code, Anthropic's official CLI for Claude, running in a terminal."
        out = sanitize_text(text)
        self.assertNotIn("Anthropic's official CLI for Claude", out)
        self.assertIn("official CLI tool for Claude", out)
        # 只换一个词：句子其余部分必须原样保留
        self.assertIn("running in a terminal.", out)

    def test_strips_billing_header_line(self):
        text = "line one\nx-anthropic-billing-hdr: session=abc; extra=1\nline two"
        out = sanitize_text(text)
        self.assertNotIn("session=abc", out)
        self.assertIn("line one", out)
        self.assertIn("line two", out)

    def test_collapses_runs_of_spaces_when_sanitizing(self):
        text = "You are Claude Code, Anthropic's official CLI for Claude   and   more"
        out = sanitize_text(text)
        self.assertEqual(out, "You are Claude Code, Anthropic's official CLI tool for Claude and more")

    def test_clean_text_is_returned_untouched(self):
        # 没有指纹就零改写：连空白也不动，避免"顺手整理"改变用户的输入
        self.assertEqual(sanitize_text("a    b\t\tc"), "a    b\t\tc")

    def test_idempotent(self):
        text = "You are Claude Code, Anthropic's official CLI for Claude"
        once = sanitize_text(text)
        self.assertEqual(sanitize_text(once), once)

    def test_empty(self):
        self.assertEqual(sanitize_text(""), "")


class SanitizeContentTest(unittest.TestCase):
    def test_string_content(self):
        sentence = "You are a coding agent running in the Codex CLI, a terminal-based coding assistant."
        out, changed = sanitize_content(sentence)
        self.assertTrue(changed)
        self.assertNotIn("Codex CLI, a terminal", out)
        self.assertIn("Codex CLI tool", out)

    def test_clean_string_reports_no_change(self):
        self.assertEqual(sanitize_content("hello"), ("hello", False))

    def test_part_list_keeps_non_text(self):
        parts = [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude"},
                 {"type": "image_url", "image_url": {"url": "http://x/y.png"}}]
        out, changed = sanitize_content(parts)
        self.assertTrue(changed)
        self.assertEqual(out[0]["type"], "text")
        self.assertNotIn("official CLI for Claude", out[0]["text"])
        self.assertEqual(out[1]["image_url"]["url"], "http://x/y.png")

    def test_other_types_untouched(self):
        self.assertEqual(sanitize_content(None), (None, False))


class SanitizeToolCallsTest(unittest.TestCase):
    def test_arguments_string_is_cleaned(self):
        call = {"id": "c1", "type": "function",
                "function": {"name": "run", "arguments": '{"cmd":"x-anthropic-billing-hdr: a=1;"}'}}
        changed = sanitize_tool_calls([call])
        self.assertTrue(changed)
        self.assertNotIn("x-anthropic-billing-hdr: a=1;", call["function"]["arguments"])
        self.assertEqual(call["function"]["name"], "run")

    def test_clean_arguments_untouched(self):
        call = {"function": {"name": "run", "arguments": '{"cmd":"ls"}'}}
        self.assertFalse(sanitize_tool_calls([call]))

    def test_empty(self):
        self.assertFalse(sanitize_tool_calls([]))
        self.assertFalse(sanitize_tool_calls(None))


class SanitizeMessagesTest(unittest.TestCase):
    def test_reports_change(self):
        messages = [{"role": "user", "content": "You are Claude Code, Anthropic's official CLI for Claude"}]
        changed = sanitize_messages(messages)
        self.assertTrue(changed)
        self.assertNotIn("Anthropic's official CLI for Claude", messages[0]["content"])

    def test_no_change_reports_false(self):
        messages = [{"role": "user", "content": "hello"}]
        self.assertFalse(sanitize_messages(messages))

    def test_reasoning_fields_cleaned(self):
        messages = [{"role": "assistant", "content": "ok",
                     "reasoning_content": "x-anthropic-billing-hdr: a=1;\nrest"}]
        self.assertTrue(sanitize_messages(messages))
        self.assertNotIn("x-anthropic-billing-hdr", messages[0]["reasoning_content"])
        self.assertIn("rest", messages[0]["reasoning_content"])

    def test_system_prompt_kept(self):
        messages = [{"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "hi"}]
        self.assertFalse(sanitize_messages(messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
