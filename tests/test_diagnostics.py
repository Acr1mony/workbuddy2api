"""失败报文留存：脱敏、限大小、限数量。"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from wb2api.diagnostics import Diagnostics, redact_headers, redact_text


class RedactTest(unittest.TestCase):
    def test_sensitive_headers_are_masked(self):
        out = redact_headers({"Authorization": "Bearer secret", "X-Refresh-Token": "rt", "Content-Type": "application/json"})
        self.assertEqual(out["authorization"], "**redacted**")
        self.assertEqual(out["x-refresh-token"], "**redacted**")
        self.assertEqual(out["content-type"], "application/json")

    def test_token_shapes_in_body_are_masked(self):
        text = redact_text('{"key":"sk-abcdefghijklmnop1234","note":"ok"}')
        self.assertNotIn("abcdefghijklmnop1234", text)
        self.assertIn("ok", text)


class DumpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-diag-")

    def diag(self, **kwargs):
        return Diagnostics(self.tmp, **kwargs)

    def test_dump_then_read_back(self):
        diag = self.diag()
        path = diag.dump(
            request_id="req_1", model="deepseek-v3", realm="cn", status=400,
            code="11101", message="tool_choice invalid",
            body='{"code":11101,"message":"bad"}',
            request_headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
        )
        self.assertTrue(path and os.path.exists(path))
        record = diag.get("req_1")
        self.assertIsNotNone(record)
        self.assertEqual(record["code"], "11101")
        self.assertIn("tool_choice invalid", record["message"])
        self.assertEqual(record["request_headers"]["authorization"], "**redacted**")
        self.assertIsNone(diag.get("nope"))

    def test_recent_lists_summaries(self):
        diag = self.diag()
        for index in range(3):
            diag.dump(request_id="req_%d" % index, model="m", status=400, code="11148", body="x" * 10)
        rows = diag.recent(2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("body" not in row for row in rows))

    def test_body_is_truncated(self):
        diag = self.diag(max_body_bytes=256)
        diag.dump(request_id="req_big", body="a" * 5000)
        record = diag.get("req_big")
        self.assertTrue(record["truncated"])
        self.assertLessEqual(len(record["body"]), 256)

    def test_daily_cap(self):
        diag = self.diag(daily_max=2)
        self.assertTrue(diag.dump(request_id="a", body="1"))
        self.assertTrue(diag.dump(request_id="b", body="2"))
        self.assertEqual(diag.dump(request_id="c", body="3"), "")
        self.assertEqual(diag.stats()["today"], 2)

    def test_disabled_writes_nothing(self):
        diag = self.diag(enabled=False)
        self.assertEqual(diag.dump(request_id="x", body="y"), "")
        self.assertEqual(diag.recent(), [])

    def test_no_data_dir_is_disabled(self):
        diag = Diagnostics("")
        self.assertFalse(diag.enabled)

    def test_prune_removes_old_days(self):
        diag = self.diag(retention_days=1)
        os.makedirs(diag.root, exist_ok=True)
        old = os.path.join(diag.root, "dump-2020-01-01.jsonl")
        with open(old, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"request_id": "old"}) + "\n")
        self.assertEqual(diag.prune(now=time.time()), 1)
        self.assertFalse(os.path.exists(old))

    def test_bytes_body_is_decoded(self):
        diag = self.diag()
        diag.dump(request_id="req_b", body=b'{"code":11115}')
        record = diag.get("req_b")
        self.assertIn("11115", record["body"])


if __name__ == "__main__":
    unittest.main()
