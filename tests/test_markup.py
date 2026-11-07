"""markup.py 的测试：漏进正文的工具调用标记要能被完整抠出来。"""

from __future__ import annotations

import unittest

from wb2api import markup

BLOCK = (
    "<\uff5c\uff5cDSML\uff5c\uff5c calls>\n"
    '<\uff5c\uff5cDSML\uff5c\uff5c invoke name="exec_command">\n'
    '<\uff5c\uff5cDSML\uff5c\uff5c parameter name="cmd" string="true">ls -la</\uff5c\uff5cDSML\uff5c\uff5c parameter>\n'
    '<\uff5c\uff5cDSML\uff5c\uff5c parameter name="timeout" string="false">30</\uff5c\uff5cDSML\uff5c\uff5c parameter>\n'
    "</\uff5c\uff5cDSML\uff5c\uff5c invoke>\n"
    "</\uff5c\uff5cDSML\uff5c\uff5c calls>"
)


class MarkupTests(unittest.TestCase):
    def test_parse_block_string_and_number_params(self) -> None:
        calls = markup.parse_block(BLOCK, ["exec_command"])
        self.assertEqual(1, len(calls))
        call = calls[0]
        self.assertEqual("exec_command", call["function"]["name"])
        self.assertEqual("function", call["type"])
        self.assertTrue(call["id"].startswith("call_"))
        self.assertIn('"cmd": "ls -la"', call["function"]["arguments"])
        self.assertIn('"timeout": 30', call["function"]["arguments"])

    def test_parse_block_skips_unlisted_tools(self) -> None:
        self.assertEqual([], markup.parse_block(BLOCK, ["read_file"]))
        self.assertEqual([], markup.parse_block(BLOCK, ["exec_command;"]))

    def test_extract_keeps_surrounding_text(self) -> None:
        text = "先看看目录。\n" + BLOCK + "\n然后再说。"
        remaining, calls = markup.extract(text, ["exec_command"])
        self.assertEqual(1, len(calls))
        self.assertIn("先看看目录。", remaining)
        self.assertIn("然后再说。", remaining)
        self.assertNotIn("DSML", remaining)

    def test_extract_without_allowed_list_accepts_any_tool(self) -> None:
        text = "正文" + BLOCK
        remaining, calls = markup.extract(text)
        self.assertEqual(1, len(calls))
        self.assertNotIn("DSML", remaining)

    def test_extract_is_incremental_across_arbitrary_splits(self) -> None:
        clean = "你好，我来清理一下。"
        visible: list = []
        filt = markup.MarkupFilter(["exec_command"])
        for chunk in [clean[:3], clean[3:], BLOCK[:11], BLOCK[11:40], BLOCK[40:120], BLOCK[120:]]:
            visible.append(filt.feed(chunk))
        visible.append(filt.flush())
        self.assertEqual(clean, "".join(visible))
        self.assertEqual(1, len(filt.calls))

    def test_filter_holds_partial_marker_prefix(self) -> None:
        filt = markup.MarkupFilter(["exec_command"])
        self.assertEqual("x ", filt.feed("x <\uff5c\uff5cDSM"))  # 正文放行，标记前缀扣住
        self.assertEqual("", filt.feed("L\uff5c\uff5c calls>\n<\uff5c\uff5cDSML\uff5c\uff5c invoke name=\"exec_command\">"))
        self.assertEqual([], filt.calls)  # 还没闭合，调用未落地
        out = filt.feed(
            '\n<\uff5c\uff5cDSML\uff5c\uff5c parameter name="cmd" string="true">ls</\uff5c\uff5cDSML\uff5c\uff5c parameter>'
            "\n</\uff5c\uff5cDSML\uff5c\uff5c invoke>\n</\uff5c\uff5cDSML\uff5c\uff5c calls>"
        )
        self.assertEqual("", out)
        self.assertEqual(1, len(filt.calls))
        self.assertEqual("exec_command", filt.calls[0]["function"]["name"])

    def test_filter_releases_plain_html_once_closed(self) -> None:
        filt = markup.MarkupFilter(["exec_command"])
        self.assertEqual("hello ", filt.feed("hello <"))
        self.assertEqual("<b>粗体</b>", filt.feed("b>粗体</b>"))
        self.assertEqual([], filt.calls)

    def test_filter_flushes_unclosed_block_as_text(self) -> None:
        filt = markup.MarkupFilter(["exec_command"])
        partial = "<\uff5c\uff5cDSML\uff5c\uff5c calls>"
        filt.feed(partial)
        self.assertEqual(partial, filt.flush())

    def test_calls_to_delta_shape(self) -> None:
        calls = markup.parse_block(BLOCK, ["exec_command"])
        delta = markup.calls_to_delta(calls, index_base=2)
        self.assertEqual(1, len(delta["tool_calls"]))
        entry = delta["tool_calls"][0]
        self.assertEqual(2, entry["index"])
        self.assertEqual("exec_command", entry["function"]["name"])


if __name__ == "__main__":
    unittest.main()
