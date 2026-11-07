"""出口级退避：链路抖动不该罚账号，也不该让所有线程同一刻重试。

这几条都是"看着像小优化，其实决定可用性"的地方：
如果 5xx 记到账号头上，一次 CDN 抖动就会让整池同时熔断；
如果退避没有抖动，退避结束后所有请求会在同一毫秒一起回来。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb2api import egress, util


class FakeCfg:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, path, default=None):
        node = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class KeyTests(unittest.TestCase):
    def test_key_contains_host_and_proxy(self):
        key = egress.Egress.key_for("https://copilot.tencent.com/v2/chat/completions", "http://127.0.0.1:7890")
        self.assertIn("copilot.tencent.com", key)
        self.assertIn("127.0.0.1:7890", key)

    def test_direct_and_proxied_are_different_buckets(self):
        url = "https://copilot.tencent.com/v2/chat/completions"
        self.assertNotEqual(
            egress.Egress.key_for(url, ""), egress.Egress.key_for(url, "http://p:1"),
            "走代理和不走代理是两个出口，退避不能互相影响",
        )

    def test_garbage_url_does_not_raise(self):
        key = egress.Egress.key_for("not a url")
        self.assertIsInstance(key, str)


class BackoffTests(unittest.TestCase):
    def test_first_failure_waits_about_base(self):
        box = egress.Egress(base=1.0, max_wait=15.0, jitter=0.0)
        wait = box.note_failure("k")
        self.assertAlmostEqual(wait, 1.0, delta=0.01)
        self.assertGreater(box.wait_for("k"), 0.0)

    def test_backoff_doubles_then_caps(self):
        box = egress.Egress(base=1.0, max_wait=8.0, jitter=0.0)
        waits = [box.note_failure("k") for _ in range(6)]
        self.assertAlmostEqual(waits[0], 1.0, delta=0.01)
        self.assertAlmostEqual(waits[1], 2.0, delta=0.01)
        self.assertAlmostEqual(waits[2], 4.0, delta=0.01)
        self.assertAlmostEqual(waits[3], 8.0, delta=0.01)
        self.assertAlmostEqual(waits[5], 8.0, delta=0.01, msg="封顶后不能再涨")

    def test_jitter_stays_within_bounds(self):
        box = egress.Egress(base=10.0, max_wait=10.0, jitter=0.25)
        for _ in range(20):
            box.note_success("k")
            wait = box.note_failure("k")
            self.assertGreaterEqual(wait, 7.5 - 0.01)
            self.assertLessEqual(wait, 12.5 + 0.01)

    def test_success_clears(self):
        box = egress.Egress(base=1.0, max_wait=15.0)
        box.note_failure("k")
        box.note_success("k")
        self.assertEqual(box.wait_for("k"), 0.0)
        self.assertAlmostEqual(box.note_failure("k"), 1.0, delta=0.3, msg="成功后计数要清零")

    def test_stale_failures_reset(self):
        box = egress.Egress(base=1.0, max_wait=15.0, reset_after=60.0, jitter=0.0)
        box.note_failure("k")
        # 假装很久以前失败过：超过 reset_after 就该当没发生
        box._state["k"] = (9, util.now() - 3600, util.now() - 3600)
        self.assertEqual(box.wait_for("k"), 0.0)
        self.assertAlmostEqual(box.note_failure("k"), 1.0, delta=0.01)

    def test_buckets_are_independent(self):
        box = egress.Egress(base=1.0, max_wait=15.0, jitter=0.0)
        box.note_failure("a")
        self.assertEqual(box.wait_for("b"), 0.0, "一个出口抖动不该影响另一个出口")

    def test_snapshot_lists_only_live_entries(self):
        box = egress.Egress(base=1.0, max_wait=15.0)
        box.note_failure("a")
        box.note_failure("b")
        box.note_success("b")
        snap = box.snapshot()
        self.assertEqual(snap["count"], 1)
        self.assertIn("a", snap["egress"])

    def test_reset_clears_everything(self):
        box = egress.Egress()
        box.note_failure("a")
        box.reset()
        self.assertEqual(box.wait_for("a"), 0.0)


class FromConfigTests(unittest.TestCase):
    def test_reads_configured_values(self):
        cfg = FakeCfg({"upstream": {"egress_base_seconds": 2.0, "egress_max_seconds": 30.0,
                                    "egress_reset_seconds": 120.0, "egress_jitter": 0.5}})
        box = egress.Egress.from_config(cfg)
        self.assertEqual(box.base, 2.0)
        self.assertEqual(box.max_wait, 30.0)
        self.assertEqual(box.reset_after, 120.0)
        self.assertEqual(box.jitter, 0.5)

    def test_defaults_when_cfg_is_none(self):
        box = egress.Egress.from_config(None)
        self.assertEqual(box.base, egress.DEFAULT_BASE)
        self.assertEqual(box.max_wait, egress.DEFAULT_MAX)

    def test_bad_values_fall_back(self):
        cfg = FakeCfg({"upstream": {"egress_base_seconds": "abc"}})
        box = egress.Egress.from_config(cfg)
        self.assertEqual(box.base, egress.DEFAULT_BASE)

    def test_max_never_below_base(self):
        box = egress.Egress(base=5.0, max_wait=1.0)
        self.assertGreaterEqual(box.max_wait, box.base)


if __name__ == "__main__":
    unittest.main()
