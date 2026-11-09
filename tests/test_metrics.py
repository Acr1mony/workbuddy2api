"""指标层：TTFB 分位、outcome 分布、重试直方图、按模型延迟。

这里最容易错的地方是"分位数算错但看起来合理"，所以断言用能手工算出来的样本：
1..100 的 TTFB，p50 就该是 ~50、p99 该是 ~99。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb2api.metrics import Metrics


def sample(**kw):
    base = {
        "model": "m1",
        "status": 200,
        "outcome": "completed",
        "ttfb_ms": 100,
        "elapsed_ms": 500,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "cached_tokens": 0,
        "attempts": 1,
    }
    base.update(kw)
    return base


class PercentileTest(unittest.TestCase):
    def test_p50_p90_p99_from_known_samples(self):
        metrics = Metrics(capacity=500)
        for value in range(1, 101):
            metrics.observe(sample(ttfb_ms=value, elapsed_ms=value * 10))
        snapshot = metrics.snapshot()
        self.assertAlmostEqual(snapshot["ttfb_ms"]["p50"], 50.0, delta=2.0)
        self.assertAlmostEqual(snapshot["ttfb_ms"]["p90"], 90.0, delta=2.0)
        self.assertAlmostEqual(snapshot["ttfb_ms"]["p99"], 99.0, delta=2.0)
        self.assertEqual(snapshot["ttfb_ms"]["max"], 100.0)
        self.assertAlmostEqual(snapshot["elapsed_ms"]["p50"], 500.0, delta=20.0)

    def test_empty_snapshot_is_zero_not_crash(self):
        snapshot = Metrics().snapshot()
        self.assertEqual(snapshot["requests"]["sampled"], 0)
        self.assertEqual(snapshot["ttfb_ms"]["p99"], 0.0)
        self.assertEqual(snapshot["requests"]["error_rate"], 0.0)


class OutcomeTest(unittest.TestCase):
    def test_outcomes_are_four_buckets(self):
        metrics = Metrics()
        metrics.observe(sample(outcome="completed"))
        metrics.observe(sample(outcome="client_aborted"))
        metrics.observe(sample(outcome="upstream_aborted"))
        metrics.observe(sample(outcome="failed", status=502))
        metrics.observe(sample(outcome="something-new"))  # 未知值归一成 failed
        outcomes = metrics.snapshot()["outcomes"]
        self.assertEqual(outcomes["completed"], 1)
        self.assertEqual(outcomes["client_aborted"], 1)
        self.assertEqual(outcomes["upstream_aborted"], 1)
        self.assertEqual(outcomes["failed"], 2)

    def test_attempts_histogram_buckets_4_plus(self):
        metrics = Metrics()
        for attempts in (1, 1, 2, 3, 4, 9):
            metrics.observe(sample(attempts=attempts))
        attempts = metrics.snapshot()["attempts"]
        self.assertEqual(attempts["1"], 2)
        self.assertEqual(attempts["2"], 1)
        self.assertEqual(attempts["3"], 1)
        self.assertEqual(attempts["4+"], 2)

    def test_status_classes(self):
        metrics = Metrics()
        metrics.observe(sample(status=200))
        metrics.observe(sample(status=429, outcome="failed"))
        metrics.observe(sample(status=503, outcome="failed"))
        metrics.observe(sample(status=None, outcome="failed"))
        status = metrics.snapshot()["status"]
        self.assertEqual(status["2xx"], 1)
        self.assertEqual(status["4xx"], 1)
        self.assertEqual(status["5xx"], 1)
        self.assertEqual(status["none"], 1)


class CacheAndModelTest(unittest.TestCase):
    def test_cache_hit_rate_only_counts_prompt_bearing_requests(self):
        metrics = Metrics()
        metrics.observe(sample(prompt_tokens=100, cached_tokens=50))
        metrics.observe(sample(prompt_tokens=100, cached_tokens=0))
        metrics.observe(sample(prompt_tokens=0, cached_tokens=0))  # 没带的样本不该拉低命中率
        snapshot = metrics.snapshot()
        self.assertAlmostEqual(snapshot["requests"]["cache_hit_rate"], 0.5, places=3)

    def test_per_model_rows_sorted_by_volume(self):
        metrics = Metrics()
        for _ in range(3):
            metrics.observe(sample(model="busy", ttfb_ms=200))
        metrics.observe(sample(model="rare", status=500, outcome="failed", ttfb_ms=900))
        rows = metrics.snapshot()["models"]
        self.assertEqual(rows[0]["model"], "busy")
        self.assertEqual(rows[0]["requests"], 3)
        self.assertAlmostEqual(rows[0]["avg_ttfb_ms"], 200.0)
        self.assertEqual(rows[1]["errors"], 1)

    def test_capacity_drops_old_samples(self):
        metrics = Metrics(capacity=50)
        for index in range(120):
            metrics.observe(sample(ttfb_ms=index))
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["window"]["samples"], 50)
        self.assertGreaterEqual(snapshot["ttfb_ms"]["p50"], 70.0, "只该留最后 50 条")

    def test_reset_clears_everything(self):
        metrics = Metrics()
        metrics.observe(sample())
        metrics.reset()
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["requests"]["sampled"], 0)
        self.assertEqual(snapshot["outcomes"]["completed"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
