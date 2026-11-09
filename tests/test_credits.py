"""余额的"钱"视角：包解析、到期分桶、变动台账。

上游把额度拆成几十个包，每个包到期日不同。这里最容易出的错是"总数算对了但
到期算错"——那会让面板在真归零那天显得毫无预兆，所以断言都盯时间。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb2api import credits

TZ_CN = timezone(timedelta(hours=8))


def stamp(days: float) -> str:
    moment = datetime.now(TZ_CN) + timedelta(days=days)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def payload_of(*rows):
    return {"data": {"Response": {"Data": {"Accounts": list(rows)}}}}


class ParsePackagesTest(unittest.TestCase):
    def test_reads_precise_then_coarse_fields(self):
        rows = credits.parse_packages(payload_of(
            {"Name": "precise", "CapacityRemainPrecise": "5.01000004", "CycleEndTime": stamp(5), "Status": 0},
            {"Name": "coarse", "CapacityRemain": 12, "CycleEndTime": stamp(40), "Status": 0},
        ))
        self.assertEqual([row["name"] for row in rows], ["precise", "coarse"])
        self.assertAlmostEqual(rows[0]["remain"], 5.01000004, places=6)
        self.assertEqual(rows[1]["remain"], 12.0)

    def test_package_without_any_capacity_field_is_skipped(self):
        rows = credits.parse_packages(payload_of(
            {"Name": "empty", "CycleEndTime": stamp(3)},
            {"Name": "ok", "CapacityRemain": 1, "CycleEndTime": stamp(3)},
        ))
        self.assertEqual(len(rows), 1, "读不到额度就跳过，绝不猜一个数字出来")
        self.assertEqual(rows[0]["name"], "ok")

    def test_status_marks_expired_only_without_expiry_time(self):
        rows = credits.parse_packages(payload_of(
            {"Name": "by-time", "CapacityRemain": 2, "CycleEndTime": stamp(-2), "Status": 0},
            {"Name": "by-status", "CapacityRemain": 3, "Status": 2},
        ))
        flags = {row["name"]: row["expired"] for row in rows}
        self.assertTrue(flags["by-time"])
        self.assertTrue(flags["by-status"])

    def test_unknown_shape_returns_empty(self):
        for payload in ({}, {"code": 0}, {"data": {"Response": {"Data": {}}}}, None, []):
            self.assertEqual(credits.parse_packages(payload), [])


class BucketsTest(unittest.TestCase):
    def test_buckets_split_by_expiry_distance(self):
        rows = credits.parse_packages(payload_of(
            {"CapacityRemain": 1, "CycleEndTime": stamp(1)},
            {"CapacityRemain": 2, "CycleEndTime": stamp(5)},
            {"CapacityRemain": 4, "CycleEndTime": stamp(20)},
            {"CapacityRemain": 8, "CycleEndTime": stamp(300)},
            {"CapacityRemain": 16, "CycleEndTime": stamp(-3)},
        ))
        info = credits.summarize(rows)
        self.assertEqual(info["buckets"]["d3"], 1.0)
        self.assertEqual(info["buckets"]["d7"], 2.0)
        self.assertEqual(info["buckets"]["d30"], 4.0)
        self.assertEqual(info["buckets"]["later"], 8.0)
        self.assertEqual(info["buckets"]["expired"], 16.0)
        self.assertEqual(info["total"], 15.0, "过期包不算可用额度")
        self.assertEqual(info["expiring_soon"], 3.0)
        self.assertGreater(info["soonest_expire_at"], time.time())

    def test_package_without_expiry_goes_to_later(self):
        rows = credits.parse_packages(payload_of({"CapacityRemain": 9}))
        info = credits.summarize(rows)
        self.assertEqual(info["buckets"]["later"], 9.0)
        self.assertEqual(info["soonest_expire_at"], 0.0)

    def test_parse_time_understands_cn_wall_clock(self):
        value = credits.parse_time("2026-11-13 23:59:59")
        self.assertAlmostEqual(value, 1794585599.0, delta=1.0)
        self.assertEqual(credits.parse_time(""), 0.0)
        self.assertEqual(credits.parse_time("not a time"), 0.0)


class CreditBookTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb2a-credits-")
        self.book = credits.CreditBook(self.dir, retention_days=30)

    def test_record_and_history(self):
        self.book.record("uid-1", "cn", {"total": 100.0, "expiring_soon": 5.0, "buckets": {"d7": 5.0}}, note="test")
        self.book.record("uid-2", "global", {"total": 7.0})
        rows = self.book.history("uid-1", 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["credits"], 100.0)
        self.assertEqual(rows[0]["buckets"], {"d7": 5.0})
        self.assertEqual(len(self.book.history("", 10)), 2)

    def test_delta_reports_spend(self):
        self.book.record("uid-1", "cn", {"total": 100.0})
        self.book.record("uid-1", "cn", {"total": 90.0})
        self.book.record("uid-1", "cn", {"total": 95.0})  # 签到 ++ 之类不该算消耗
        info = self.book.delta("uid-1", 7)
        self.assertEqual(info["samples"], 3)
        self.assertAlmostEqual(info["delta"], -5.0)
        self.assertAlmostEqual(info["spent"], 10.0)

    def test_prune_removes_old_days_only(self):
        os.makedirs(os.path.join(self.dir, "credits"), exist_ok=True)
        old = os.path.join(self.dir, "credits", "credit-2000-01-01.jsonl")
        fresh = os.path.join(self.dir, "credits", "credit-%s.jsonl" % datetime.now(TZ_CN).strftime("%Y-%m-%d"))
        for path in (old, fresh):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"at": 1, "uid": "x", "credits": 1}\n')
        self.assertEqual(self.book.prune(), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))


if __name__ == "__main__":
    unittest.main(verbosity=2)
