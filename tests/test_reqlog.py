"""逐请求明细：写入、过滤、保留期。"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from wb2api.reqlog import RequestLog


def event(**extra):
    base = {
        "at": time.time(),
        "model": "deepseek-v3",
        "account": "u1",
        "status": 200,
        "outcome": "completed",
        "path": "/v1/chat/completions",
        "request_id": "req_x",
        "elapsed_ms": 100,
    }
    base.update(extra)
    return base


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-req-")

    def log(self, **kwargs):
        return RequestLog(self.tmp, **kwargs)

    def test_writes_jsonl_and_keeps_recent(self):
        log = self.log()
        log.record(event(request_id="req_1"))
        log.record(event(request_id="req_2", status=429, outcome="failed"))
        rows = log.recent(10)
        self.assertEqual([row["request_id"] for row in rows], ["req_2", "req_1"])
        files = os.listdir(os.path.join(self.tmp, "requests"))
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.tmp, "requests", files[0]), encoding="utf-8") as handle:
            self.assertEqual(len(handle.readlines()), 2)

    def test_capacity_drops_oldest_from_memory(self):
        log = self.log(capacity=50)
        for index in range(80):
            log.record(event(request_id="req_%d" % index))
        self.assertEqual(len(log.recent(1000)), 50)
        self.assertEqual(log.recent(1)[0]["request_id"], "req_79")

    def test_disabled_still_buffers_but_writes_nothing(self):
        log = self.log(enabled=False)
        log.record(event(request_id="req_1"))
        self.assertEqual(len(log.recent()), 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "requests")))

    def test_survives_bad_json_lines(self):
        log = self.log()
        os.makedirs(os.path.join(self.tmp, "requests"), exist_ok=True)
        path = os.path.join(self.tmp, "requests", "req-2020-01-01.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(event(request_id="req_ok")) + "\n{bad json\n")
        fresh = self.log()
        fresh.load()
        self.assertEqual([row["request_id"] for row in fresh.recent()], ["req_ok"])


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-req-f-")
        self.log = RequestLog(self.tmp)
        self.log.record(event(request_id="a", status=200, model="deepseek-v3", account="u1"))
        self.log.record(event(request_id="b", status=429, model="glm-5.3", account="u2", outcome="failed"))
        self.log.record(event(request_id="c", status=503, model="glm-5.3", account="u1", outcome="failed"))

    def ids(self, rows):
        return [row["request_id"] for row in rows]

    def test_status_class_filter(self):
        self.assertEqual(self.ids(self.log.recent(status="5xx")), ["c"])
        self.assertEqual(sorted(self.ids(self.log.recent(status="error"))), ["b", "c"])
        self.assertEqual(self.ids(self.log.recent(status="200")), ["a"])

    def test_model_and_account_filters(self):
        self.assertEqual(sorted(self.ids(self.log.recent(model="glm"))), ["b", "c"])
        self.assertEqual(sorted(self.ids(self.log.recent(account="u1"))), ["a", "c"])

    def test_query_filter(self):
        self.assertEqual(self.ids(self.log.recent(query="429")), ["b"])


class PruneTest(unittest.TestCase):
    def test_prune_removes_old_files_only(self):
        tmp = tempfile.mkdtemp(prefix="wb2a-req-p-")
        log = RequestLog(tmp, retention_days=1)
        root = os.path.join(tmp, "requests")
        os.makedirs(root, exist_ok=True)
        old = os.path.join(root, "req-2020-01-01.jsonl")
        with open(old, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        self.assertEqual(log.prune(now=time.time()), 1)
        self.assertFalse(os.path.exists(old))

    def test_stats_shape(self):
        tmp = tempfile.mkdtemp(prefix="wb2a-req-s-")
        stats = RequestLog(tmp).stats()
        for key in ("enabled", "buffered", "files", "retention_days", "capacity"):
            self.assertIn(key, stats)


if __name__ == "__main__":
    unittest.main()
