"""usage.py 台账：按天 JSONL、聚合、以及"重放一次"这条硬约束。

台账的所有视图都来自磁盘回放 + 增量累加，于是"同一行被算两次"是最容易犯、也最
难在面板上发现的错误（数字看起来只是偏大）。这里专门盯住 record→load、load→load
几种顺序：每条记录只能算一次。
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb2api.usage import UsageLedger


def day_of(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class UsageBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def ledger(self, retention_days=7):
        return UsageLedger(self.dir, retention_days=retention_days)

    def event(self, **kw):
        base = {
            "status": 200,
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cached_tokens": 40,
            "credits": 0.5,
            "latency_ms": 100,
            "key_name": "k1",
            "account": "u1",
            "model": "deepseek-v3",
        }
        base.update(kw)
        return base

    def read_lines(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return [line for line in handle if line.strip()]


class DayFileTest(UsageBase):
    def test_record_lands_in_per_day_file(self):
        ledger = self.ledger()
        ts = time.time()
        ledger.record(self.event(ts=ts))
        expected_day = day_of(ts)
        path = ledger.file_for(expected_day)
        self.assertTrue(os.path.exists(path), "记录必须落在 usage-<day>.jsonl 里")
        lines = self.read_lines(path)
        self.assertEqual(len(lines), 1)
        stored = json.loads(lines[0])
        self.assertEqual(stored["ts"], ts)
        self.assertEqual(stored["day"], expected_day)
        self.assertEqual(stored["model"], "deepseek-v3")

    def test_day_follows_ts_not_now(self):
        # 补记昨天的数据（比如进程刚重启时刷出的延迟记录）要进昨天的文件。
        ledger = self.ledger()
        ts = time.time() - 86400
        ledger.record(self.event(ts=ts))
        self.assertTrue(os.path.exists(ledger.file_for(day_of(ts))))
        self.assertFalse(os.path.exists(ledger.file_for(day_of(time.time()))))

    def test_append_across_instances(self):
        first = self.ledger()
        ts = time.time()
        first.record(self.event(ts=ts))
        second = self.ledger()  # 模拟重启
        second.record(self.event(ts=ts))
        lines = self.read_lines(first.file_for(day_of(ts)))
        self.assertEqual(len(lines), 2, "重启后要追加，不能覆盖")

    def test_record_does_not_lose_extra_fields(self):
        ledger = self.ledger()
        ledger.record(self.event(ts=time.time(), extra="x"))
        stored = json.loads(self.read_lines(ledger.file_for(day_of(time.time())))[0])
        self.assertEqual(stored["extra"], "x")


class AggregateTest(UsageBase):
    def test_singular_credit_key_is_counted(self):
        # service 写的是 "credit"（上游 usage 字段名），这里必须认得，
        # 否则面板"消耗积分"永远是 0。
        ledger = self.ledger()
        ledger.record(self.event(credit=0.02, credits=None))
        self.assertAlmostEqual(ledger.summary()["total"]["credits"], 0.02)
    def test_totals_and_groups(self):
        ledger = self.ledger()
        base = time.time()
        ledger.record(self.event(ts=base, key_name="k1", account="u1", model="m1", prompt_tokens=100,
                                 completion_tokens=10, cached_tokens=30, latency_ms=100))
        ledger.record(self.event(ts=base, key_name="k2", account="u1", model="m2", prompt_tokens=200,
                                 completion_tokens=20, cached_tokens=10, latency_ms=300))
        ledger.record(self.event(ts=base, key_name="k1", account="u2", model="m1", status=500,
                                 prompt_tokens=50, completion_tokens=5, cached_tokens=0, latency_ms=200))
        summary = ledger.summary()
        total = summary["total"]
        self.assertEqual(total["requests"], 3)
        self.assertEqual(total["ok"], 2)
        self.assertEqual(total["errors"], 1, "500 必须记进 errors")
        self.assertEqual(total["prompt_tokens"], 350)
        self.assertEqual(total["completion_tokens"], 35)
        self.assertEqual(total["cached_tokens"], 40)
        self.assertEqual(total["latency_ms"], 600)

        self.assertEqual(set(summary["by_key"]), {"k1", "k2"})
        self.assertEqual(summary["by_key"]["k1"]["requests"], 2)
        self.assertEqual(summary["by_key"]["k1"]["errors"], 1)
        self.assertEqual(summary["by_key"]["k2"]["requests"], 1)

        self.assertEqual(set(summary["by_account"]), {"u1", "u2"})
        self.assertEqual(summary["by_account"]["u1"]["requests"], 2)
        self.assertEqual(set(summary["by_model"]), {"m1", "m2"})
        self.assertEqual(summary["by_model"]["m1"]["requests"], 2)
        self.assertEqual(sum(b["requests"] for b in summary["by_day"].values()), 3)

    def test_missing_optional_fields_default_to_zero(self):
        ledger = self.ledger()
        ledger.record({"ts": time.time(), "status": 200})
        total = ledger.summary()["total"]
        self.assertEqual(total["requests"], 1)
        self.assertEqual(total["prompt_tokens"], 0)
        self.assertEqual(total["credits"], 0)

    def test_status_boundaries(self):
        ledger = self.ledger()
        ts = time.time()
        for status in (200, 201, 302, 399, 400, 404, 500):
            ledger.record({"ts": ts, "status": status})
        total = ledger.summary()["total"]
        self.assertEqual(total["ok"], 4, "2xx/3xx 都算成功（302 也在内）")
        self.assertEqual(total["errors"], 3)


class DoubleCountTest(UsageBase):
    """历史 bug：new 实例 load() 把 record 已经加过的行又回放了一遍。"""

    def test_new_instance_load_counts_each_once(self):
        first = self.ledger()
        ts = time.time()
        for i in range(3):
            first.record(self.event(ts=ts, key_name="k%d" % i))
        second = self.ledger()
        second.load()
        summary = second.summary()
        self.assertEqual(summary["total"]["requests"], 3,
                         "同一个文件只能回放一次，否则 requests 会翻倍")
        self.assertEqual(summary["total"]["prompt_tokens"], 300)
        self.assertEqual(sum(b["requests"] for b in summary["by_day"].values()), 3)

    def test_load_twice_is_idempotent(self):
        first = self.ledger()
        first.record(self.event(ts=time.time()))
        second = self.ledger()
        second.load()
        second.load()
        self.assertEqual(second.summary()["total"]["requests"], 1,
                         "load 要能安全地调两次（网关重连/面板刷新都会触发）")

    def test_record_then_load_same_instance_counts_once(self):
        # record() 已经 _apply 过一次，随后 load() 再回放同一天就是双计。
        ledger = self.ledger()
        ledger.record(self.event(ts=time.time()))
        ledger.load()
        self.assertEqual(ledger.summary()["total"]["requests"], 1)

    def test_load_then_record_counts_once(self):
        first = self.ledger()
        ts = time.time()
        first.record(self.event(ts=ts))
        second = self.ledger()
        second.load()
        second.record(self.event(ts=ts))
        self.assertEqual(second.summary()["total"]["requests"], 2,
                         "load 之后再来一条新记录，总数应当是 2 而不是 3/4")

    def test_day_replay_when_file_appears_midrun(self):
        # 第一天记录后，另一个进程写了同一天的新行；重启的进程 load 一次即可全部认到。
        first = self.ledger()
        ts = time.time()
        first.record(self.event(ts=ts))
        with open(first.file_for(day_of(ts)), "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": ts, "day": day_of(ts), "status": 200}) + "\n")
        second = self.ledger()
        second.load()
        self.assertEqual(second.summary()["total"]["requests"], 2)


class SummaryTest(UsageBase):
    def test_avg_latency_from_elapsed_ms(self):
        # 服务层记的是 elapsed_ms；台账两种字段名都得认。
        ledger = self.ledger()
        ts = time.time()
        ledger.record({"ts": ts, "status": 200, "elapsed_ms": 100})
        ledger.record({"ts": ts, "status": 200, "elapsed_ms": 200})
        total = ledger.summary()["total"]
        self.assertEqual(total["latency_ms"], 300)
        self.assertEqual(total["avg_latency_ms"], 150.0)

    def test_avg_latency_prefers_latency_ms(self):
        ledger = self.ledger()
        ledger.record({"ts": time.time(), "status": 200, "latency_ms": 50, "elapsed_ms": 999})
        self.assertEqual(ledger.summary()["total"]["latency_ms"], 50)

    def test_cache_hit_rate(self):
        ledger = self.ledger()
        ledger.record({"ts": time.time(), "status": 200, "prompt_tokens": 1000, "cached_tokens": 250})
        ledger.record({"ts": time.time(), "status": 200, "prompt_tokens": 1000, "cached_tokens": 0})
        self.assertEqual(ledger.summary()["total"]["cache_hit_rate"], 0.125)

    def test_cache_hit_rate_without_prompt_tokens_is_zero(self):
        ledger = self.ledger()
        ledger.record({"ts": time.time(), "status": 200})
        self.assertEqual(ledger.summary()["total"]["cache_hit_rate"], 0.0)

    def test_group_buckets_have_avg_latency(self):
        ledger = self.ledger()
        ts = time.time()
        ledger.record({"ts": ts, "status": 200, "latency_ms": 100, "model": "m1"})
        ledger.record({"ts": ts, "status": 200, "latency_ms": 300, "model": "m1"})
        bucket = ledger.summary()["by_model"]["m1"]
        self.assertEqual(bucket["avg_latency_ms"], 200.0)
        self.assertEqual(bucket["requests"], 2)

    def test_retention_days_surfaced(self):
        self.assertEqual(self.ledger(retention_days=3).summary()["retention_days"], 3)

    def test_summary_does_not_expose_internal_mutable_state(self):
        ledger = self.ledger()
        ledger.record({"ts": time.time(), "status": 200})
        summary = ledger.summary()
        summary["total"]["requests"] = 999
        self.assertEqual(ledger.summary()["total"]["requests"], 1,
                         "summary 必须给副本，面板改数字不能污染台账")


class RecentTest(UsageBase):
    def test_recent_is_newest_first(self):
        ledger = self.ledger()
        base = time.time()
        for i in range(3):
            ledger.record({"ts": base + i, "status": 200, "mark": i})
        recent = ledger.recent(10)
        self.assertEqual([item["mark"] for item in recent], [2, 1, 0])

    def test_recent_limit(self):
        ledger = self.ledger()
        for i in range(5):
            ledger.record({"status": 200, "mark": i})
        self.assertEqual(len(ledger.recent(2)), 2)

    def test_recent_keeps_last_500(self):
        ledger = self.ledger()
        for i in range(505):
            ledger.record({"status": 200, "mark": i})
        kept = ledger.recent(1000)
        self.assertEqual(len(kept), 500, "内存里只留最近 500 条，面板拉不了更早的")
        self.assertEqual(kept[0]["mark"], 504)
        self.assertEqual(kept[-1]["mark"], 5)

    def test_summary_recent_capped_at_50(self):
        ledger = self.ledger()
        for i in range(60):
            ledger.record({"status": 200})
        self.assertEqual(len(ledger.summary()["recent"]), 50)


class PruneTest(UsageBase):
    def write_day(self, day):
        path = os.path.join(self.dir, "usage", "usage-%s.jsonl" % day)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": time.time(), "day": day, "status": 200}) + "\n")
        return path

    def test_prune_deletes_only_expired_days(self):
        ledger = self.ledger(retention_days=7)
        now = time.time()
        old_day = day_of(now - 30 * 86400)
        recent_day = day_of(now - 2 * 86400)
        today = day_of(now)
        old_path = self.write_day(old_day)
        recent_path = self.write_day(recent_day)
        today_path = self.write_day(today)
        removed = ledger.prune()
        self.assertEqual(removed, 1, "只该删掉 30 天前那一个文件")
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(recent_path), "保留期内的一天都不能动")
        self.assertTrue(os.path.exists(today_path))

    def test_prune_returns_zero_when_nothing_to_do(self):
        ledger = self.ledger(retention_days=7)
        self.write_day(day_of(time.time()))
        self.assertEqual(ledger.prune(), 0)

    def test_prune_ignores_non_usage_files(self):
        ledger = self.ledger(retention_days=1)
        os.makedirs(os.path.join(self.dir, "usage"), exist_ok=True)
        with open(os.path.join(self.dir, "usage", "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("keep me")
        with open(os.path.join(self.dir, "usage", "usage-not-a-date.jsonl"), "w",
                  encoding="utf-8") as handle:
            handle.write("{}\n")
        self.assertEqual(ledger.prune(), 0)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "usage", "notes.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "usage", "usage-not-a-date.jsonl")),
                        "日期解析不了的文件名不能删——可能是人工留的备份")

    def test_load_purges_expired_files(self):
        ledger = self.ledger(retention_days=7)
        old_path = self.write_day(day_of(time.time() - 30 * 86400))
        ledger.load()
        self.assertFalse(os.path.exists(old_path), "启动回放前顺手清过期文件")


class CorruptLineTest(UsageBase):
    def test_corrupt_line_is_skipped(self):
        ledger = self.ledger()
        day = day_of(time.time())
        path = ledger.file_for(day)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        good = json.dumps({"ts": time.time(), "day": day, "status": 200, "prompt_tokens": 10})
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(good + "\n")
            handle.write("this is not json\n")
            handle.write('{"broken": \n')
            handle.write(json.dumps({"ts": time.time(), "day": day, "status": 200,
                                     "prompt_tokens": 20}) + "\n")
        ledger.load()
        total = ledger.summary()["total"]
        self.assertEqual(total["requests"], 2, "坏行跳过，前后的好行要都算上")
        self.assertEqual(total["prompt_tokens"], 30)

    def test_non_dict_line_is_skipped(self):
        ledger = self.ledger()
        day = day_of(time.time())
        path = ledger.file_for(day)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("[1, 2, 3]\n")
            handle.write(json.dumps({"ts": time.time(), "day": day, "status": 200}) + "\n")
        ledger.load()
        self.assertEqual(ledger.summary()["total"]["requests"], 1)

    def test_blank_lines_are_skipped(self):
        ledger = self.ledger()
        day = day_of(time.time())
        path = ledger.file_for(day)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n\n" + json.dumps({"ts": time.time(), "day": day, "status": 200}) + "\n\n")
        ledger.load()
        self.assertEqual(ledger.summary()["total"]["requests"], 1)


class RecentAfterRestartTest(UsageBase):
    """重启后"最近请求"不能是空的——总数有值而列表为空，最像 bug。"""

    def test_replayed_records_show_up_in_recent(self):
        first = self.ledger()
        first.record(self.event(model="m-1"))
        first.record(self.event(model="m-2"))

        revived = self.ledger()
        revived.load()
        recent = revived.recent(10)
        self.assertEqual([item["model"] for item in recent], ["m-2", "m-1"], "最新在前")
        self.assertEqual(revived.summary()["total"]["requests"], 2, "回放仍然只算一次")

    def test_recent_survives_a_fresh_day(self):
        ledger = self.ledger()
        ledger.record(self.event(ts=time.time() - 86400, day=day_of(time.time() - 86400)))
        ledger.record(self.event())
        self.assertEqual([item["model"] for item in ledger.recent(5)], ["deepseek-v3", "deepseek-v3"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
