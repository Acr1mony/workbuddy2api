"""网关追加提示词：接在 system 末尾，不覆盖、不参与 sanitize。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb2api.adapters import chat
from wb2api.config import Config


class AppendPromptTest(unittest.TestCase):
    def test_appends_to_existing_system(self):
        body = {
            "model": "glm-5.3",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
            ],
        }
        out = chat.prepare_request(body, append_prompt="用小写回答")
        self.assertEqual(out["messages"][0]["role"], "system")
        self.assertEqual(out["messages"][0]["content"], "be brief\n\n用小写回答")
        # 入参不能被改
        self.assertEqual(body["messages"][0]["content"], "be brief")

    def test_creates_system_when_absent(self):
        out = chat.prepare_request(
            {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
            append_prompt="回答用中文",
        )
        self.assertEqual(out["messages"][0], {"role": "system", "content": "回答用中文"})

    def test_blank_append_is_noop(self):
        out = chat.prepare_request(
            {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
            append_prompt="   ",
        )
        self.assertFalse(any(m.get("role") == "system" for m in out["messages"]))

    def test_appends_to_block_list_content(self):
        out = chat.prepare_request(
            {
                "model": "glm-5.3",
                "messages": [
                    {"role": "system", "content": [{"type": "text", "text": "be brief"}]},
                    {"role": "user", "content": "hi"},
                ],
            },
            append_prompt="加一句",
        )
        parts = out["messages"][0]["content"]
        self.assertEqual(parts[-1], {"type": "text", "text": "加一句"})

    def test_config_has_empty_default(self):
        cfg = Config(None, {"data_dir": "/tmp/x"})
        self.assertEqual(cfg.get("prompt.append"), "")


class AppendPromptSettingsTest(unittest.TestCase):
    def test_roundtrip_through_panel(self):
        from tests.test_app import ServerCase

        class Case(ServerCase):
            def runTest(self):
                pass

        case = Case()
        case.setUp()
        try:
            token = case.panel_token()
            status, _headers, _data = case.request(
                "POST", "/api/settings", {"prompt_append": "回答缩到两句以内"},
                headers={"X-Panel-Token": token},
            )
            self.assertEqual(status, 200)
            _status, _headers, data = case.request("GET", "/api/settings", headers={"X-Panel-Token": token})
            self.assertEqual(case.json_of(data)["prompt_append"], "回答缩到两句以内")
            # 太长要被挡掉，而不是悄悄截断
            status, _headers, _data = case.request(
                "POST", "/api/settings", {"prompt_append": "x" * 4100}, headers={"X-Panel-Token": token},
            )
            self.assertEqual(status, 400)
        finally:
            case.tearDown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
