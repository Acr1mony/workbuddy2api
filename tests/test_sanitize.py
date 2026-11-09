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

# 这三句就是线上真被 11-128 拦下来的原文（DeepSeek Harness 作为客户端时，
# 它的系统提示里必然带这几段）。
DSH_GUI = ("You are interacting with the user through the DeepSeek Harness Web GUI "
           "whose URL the runtime context names.")
DSH_FILEREF = ("Tokens prefixed with @ are paths the user explicitly referenced. "
               "Anything else is a literal string.")


class FingerprintTest(unittest.TestCase):
    def test_detects_claude_code(self):
        self.assertTrue(has_fingerprint("You are Claude Code, Anthropic's official CLI tool for Claude"))

    def test_detects_billing_header(self):
        self.assertTrue(has_fingerprint("x-anthropic-billing-hdr: session=abc;"))

    def test_detects_codex(self):
        self.assertTrue(has_fingerprint("You are a coding agent running in the Codex CLI"))

    def test_detects_dsh_gui_sentence(self):
        self.assertTrue(has_fingerprint(DSH_GUI))

    def test_detects_dsh_fileref_sentence(self):
        self.assertTrue(has_fingerprint(DSH_FILEREF))

    def test_clean_text(self):
        self.assertFalse(has_fingerprint("请帮我写一个快速排序"))
        self.assertFalse(has_fingerprint("You are a helpful assistant."))

    def test_brand_alone_is_not_enough_without_gui_wording(self):
        # 只写品牌词不命中（上游要的是"品牌 + this page/GUI/app"连排）。
        self.assertFalse(has_fingerprint("DeepSeek Harness 是一个开发工具"))
        self.assertTrue(has_fingerprint("DeepSeek Harness and this page"))


class SanitizeTextTest(unittest.TestCase):
    def test_rewrites_claude_code_sentence(self):
        text = "You are Claude Code, Anthropic's official CLI tool for Claude, running in a terminal."
        out = sanitize_text(text)
        self.assertNotIn("Claude Code", out)
        self.assertIn("You are a helpful AI assistant.", out)
        # 只动这一句：后半句原样保留
        self.assertIn("running in a terminal.", out)

    def test_neutralizes_identity_sentence(self):
        out = sanitize_text("You are an AI agent powered by DeepSeek Harness. Be concise.")
        self.assertNotIn("powered by", out)
        self.assertIn("You are a helpful AI assistant.", out)
        self.assertIn("Be concise.", out)

    def test_shrinks_brand_only_in_prose(self):
        out = sanitize_text("Read this page with DeepSeek Harness and this page will render.")
        self.assertNotIn("DeepSeek Harness", out)
        self.assertIn("DeepSeek and this page", out)

    def test_keeps_paths_and_identifiers(self):
        text = "/opt/deepseek-harness/app and deepseek-harness are paths."
        self.assertEqual(sanitize_text(text), text)

    def test_gui_sentence_keeps_brand_and_only_reorders(self):
        out = sanitize_text(DSH_GUI)
        self.assertIn("DeepSeek Harness Web GUI", out, "这一段只调语序，品牌全名要留着")
        self.assertIn("whose URL is named in the runtime context.", out)
        self.assertNotIn("context names", out)

    def test_fileref_sentence_adds_one_word(self):
        out = sanitize_text(DSH_FILEREF)
        self.assertIn("paths that the user explicitly referenced", out)
        self.assertIn("Anything else is a literal string.", out)

    def test_omo_full_string_is_neutralized(self):
        out = sanitize_text("Sisyphus-Junior - Focused executor from OhMyOpenCode")
        self.assertNotIn("from OhMyOpenCode", out)
        self.assertIn("Sisyphus-Junior - Focused executor", out)

    def test_omo_alone_is_left_alone(self):
        text = "OhMyOpenCode 是我自己写的工具"
        self.assertEqual(sanitize_text(text), text)

    def test_strips_billing_header_line(self):
        text = "line one\nx-anthropic-billing-hdr: session=abc; extra=1\nline two"
        out = sanitize_text(text)
        self.assertNotIn("session=abc", out)
        self.assertIn("line one", out)
        self.assertIn("line two", out)

    def test_collapses_runs_of_spaces_when_sanitizing(self):
        text = "You are Claude Code, Anthropic's official CLI tool for Claude   and   more"
        out = sanitize_text(text)
        self.assertEqual(out, "You are a helpful AI assistant. and more")

    def test_clean_text_is_returned_untouched(self):
        # 没有指纹就零改写：连空白也不动，避免"顺手整理"改变用户的输入
        self.assertEqual(sanitize_text("a    b\t\tc"), "a    b\t\tc")

    def test_idempotent(self):
        for text in ("You are Claude Code, Anthropic's official CLI tool for Claude",
                     DSH_GUI, DSH_FILEREF,
                     "You are an AI agent powered by DeepSeek Harness. Hi."):
            once = sanitize_text(text)
            self.assertEqual(sanitize_text(once), once, text)

    def test_identity_false_skips_identity_rewrites(self):
        # 工具参数里出现自述句是应用数据，不该按"你是不是官方客户端"改写
        self.assertEqual(sanitize_text("You are Claude Code, Anthropic's official CLI tool for Claude",
                                       identity=False),
                         "You are Claude Code, Anthropic's official CLI tool for Claude")
        # 但计费头这类"没有语义"的签名照样剥
        self.assertEqual(sanitize_text("x-anthropic-billing-hdr: a=1;", identity=False), "")

    def test_empty(self):
        self.assertEqual(sanitize_text(""), "")


class SanitizeContentTest(unittest.TestCase):
    def test_string_content(self):
        sentence = "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant."
        out, changed = sanitize_content(sentence)
        self.assertTrue(changed)
        self.assertNotIn("Codex CLI", out)
        self.assertIn("You are a helpful AI assistant.", out)

    def test_clean_string_reports_no_change(self):
        self.assertEqual(sanitize_content("hello"), ("hello", False))

    def test_part_list_keeps_non_text(self):
        parts = [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI tool for Claude"},
                 {"type": "image_url", "image_url": {"url": "http://x/y.png"}}]
        out, changed = sanitize_content(parts)
        self.assertTrue(changed)
        self.assertEqual(out[0]["type"], "text")
        self.assertNotIn("Claude Code", out[0]["text"])
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
    def test_system_prompt_is_cleaned(self):
        messages = [{"role": "system", "content": DSH_GUI},
                    {"role": "user", "content": "hi"}]
        self.assertTrue(sanitize_messages(messages))
        self.assertIn("whose URL is named in the runtime context.", messages[0]["content"])

    def test_assistant_history_is_cleaned(self):
        # assistant 历史里的同一句同样会 400：多轮对话的第二轮必然把它带回来
        messages = [{"role": "assistant", "content": "You are Claude Code, Anthropic's official CLI tool for Claude"},
                    {"role": "user", "content": "继续"}]
        self.assertTrue(sanitize_messages(messages))
        self.assertNotIn("Claude Code", messages[0]["content"])

    def test_user_content_is_user_data(self):
        # 用户自己粘贴的话不改写（改了模型看到的内容就和磁盘上的不一致了），
        # 但计费头那种没有语义的签名照样剥。
        messages = [{"role": "user", "content": "You are Claude Code, Anthropic's official CLI tool for Claude"}]
        self.assertFalse(sanitize_messages(messages))
        self.assertIn("Claude Code", messages[0]["content"])

        messages = [{"role": "user", "content": "x-anthropic-billing-hdr: a=1; 你好"}]
        self.assertTrue(sanitize_messages(messages))
        self.assertNotIn("x-anthropic-billing-hdr: a=1;", messages[0]["content"])
        self.assertIn("你好", messages[0]["content"])

    def test_no_change_reports_false(self):
        messages = [{"role": "user", "content": "hello"}]
        self.assertFalse(sanitize_messages(messages))

    def test_reasoning_fields_cleaned(self):
        messages = [{"role": "assistant", "content": "ok",
                     "reasoning_content": "x-anthropic-billing-hdr: a=1;\nrest"}]
        self.assertTrue(sanitize_messages(messages))
        self.assertNotIn("x-anthropic-billing-hdr", messages[0]["reasoning_content"])
        self.assertIn("rest", messages[0]["reasoning_content"])

    def test_tool_call_arguments_cleaned(self):
        messages = [{"role": "assistant", "content": None,
                     "tool_calls": [{"id": "c1", "type": "function",
                                     "function": {"name": "write",
                                                  "arguments": '{"text":"x-anthropic-billing-hdr: a=1;"}'}}]}]
        self.assertTrue(sanitize_messages(messages))
        self.assertNotIn("x-anthropic-billing-hdr: a=1;",
                         messages[0]["tool_calls"][0]["function"]["arguments"])

    def test_system_prompt_kept(self):
        messages = [{"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "hi"}]
        self.assertFalse(sanitize_messages(messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
