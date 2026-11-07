"""调度器：到点判定、开关、任务执行。

两个参考实现都在"到点判定"上翻过车（用 ``HH:MM == now`` 精确匹配，跳一秒就整天不跑），
所以这部分必须有测试盯着。
"""

import time
import unittest

from wb2api.scheduler import GRACE, KEEPALIVE_AHEAD, Job, Scheduler, _hour_mark


class FakeCfg:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, dotted, default=None):
        node = self.values
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class FakeAccount:
    def __init__(self, uid="u1", realm="cn", expires_at=0.0, enabled=True, refresh_token="rt"):
        self.uid = uid
        self.id = uid
        self.realm = realm
        self.expires_at = expires_at
        self.disabled = not enabled
        self.refresh_token = refresh_token
        self.access_token = "at"
        self.credit = 0.0
        self.credit_at = 0.0
        self.label = "@" + uid
        self.enterprise_id = ""
        self.product = ""
        self.extra = {}

    @property
    def enabled(self):
        return not self.disabled


class FakeAuth:
    def __init__(self, fail_for=()):
        self.refreshed = []
        self.fail_for = set(fail_for)

    def refresh(self, account, force=False):
        self.refreshed.append(account.uid)
        if account.uid in self.fail_for:
            raise RuntimeError("boom")


class FakePool:
    def __init__(self):
        self.dead = []
        self.revived = []
        self.flushed = 0

    def note_auth_dead(self, account, message):
        self.dead.append(account.uid)

    def revive(self, account, balance_changed=False):
        self.revived.append((account.uid, balance_changed))

    def flush(self, force=False):
        self.flushed += 1


class FakeAffinity:
    def gc(self):
        return 0

    def stats(self):
        return {"size": 3}


class FakeLedger:
    def prune(self):
        return 0


class FakeStore:
    def __init__(self, accounts):
        self.accounts = accounts
        self.saved = 0
        self.key_flushes = 0
        self.realm_queries = []

    def list_accounts(self, realm=""):
        self.realm_queries.append(realm)
        if not realm:
            return list(self.accounts)
        return [a for a in self.accounts if a.realm == realm]

    def save_accounts(self):
        self.saved += 1

    def flush_keys(self):
        self.key_flushes += 1


class FakeBillingResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def close(self):
        pass


class FakeClient:
    """billing 只用得到 base_url + request，返回 code=0 的空信封就够它当成功了。"""

    def __init__(self):
        self.calls = []

    def base_url(self, realm):
        return "https://%s.test" % (realm or "cn")

    def request(self, method, url, headers, raw_body=None, proxy=""):
        self.calls.append((method, url))
        return FakeBillingResponse({"code": 0, "msg": "OK", "data": {}})

    def post(self, url, headers, raw_body=None, proxy=""):
        return self.request("POST", url, headers, raw_body, proxy)

    def get(self, url, headers):
        return self.request("GET", url, headers)


class FakeService:
    def __init__(self, accounts, fail_refresh=()):
        self.store = FakeStore(accounts)
        self.auth = FakeAuth(fail_refresh)
        self.pool = FakePool()
        self.affinity = FakeAffinity()
        self.ledger = FakeLedger()
        self.client = FakeClient()


class TestJobDue(unittest.TestCase):
    def test_interval(self):
        job = Job("x", lambda: None, interval=60)
        self.assertTrue(job.due(1000), "从没跑过就该跑")
        job.last_run = 1000
        self.assertFalse(job.due(1030))
        self.assertTrue(job.due(1060))

    def test_no_interval_no_hour_never_runs(self):
        self.assertFalse(Job("x", lambda: None).due(time.time()))

    def test_at_hour_runs_within_grace(self):
        now = time.time()
        mark = _hour_mark(now, time.localtime(now).tm_hour)
        job = Job("daily", lambda: None, at_hour=time.localtime(now).tm_hour)
        self.assertTrue(job.due(mark + 60))
        job.last_run = mark + 61
        self.assertFalse(job.due(mark + 120), "同一天跑过就不再跑")

    def test_at_hour_skips_when_too_late(self):
        now = time.time()
        hour = time.localtime(now).tm_hour
        mark = _hour_mark(now, hour)
        job = Job("daily", lambda: None, at_hour=hour)
        self.assertFalse(job.due(mark + GRACE + 60), "超过 grace 就不再补跑")

    def test_hour_mark_never_in_future(self):
        now = time.time()
        for hour in (0, 9, 23):
            mark = _hour_mark(now, hour)
            self.assertLessEqual(mark, now + 1)
            self.assertEqual(time.localtime(mark).tm_hour, hour)


class TestRegistration(unittest.TestCase):
    def test_all_enabled_by_default(self):
        scheduler = Scheduler(FakeService([]), FakeCfg())
        self.assertEqual(set(scheduler.jobs), {"keepalive", "balance", "checkin", "report", "housekeeping", "flush"})

    def test_disabled_jobs_are_not_registered(self):
        cfg = FakeCfg({"schedule": {
            "token_refresh_enabled": False,
            "balance_refresh_enabled": False,
            "checkin_enabled": False,
            "report_enabled": False,
        }})
        scheduler = Scheduler(FakeService([]), cfg)
        self.assertEqual(set(scheduler.jobs), {"housekeeping", "flush"})

    def test_interval_from_config(self):
        cfg = FakeCfg({"schedule": {"keepalive_minutes": 5}})
        scheduler = Scheduler(FakeService([]), cfg)
        self.assertEqual(scheduler.jobs["keepalive"].interval, 300)

    def test_report_runs_at_its_hour(self):
        cfg = FakeCfg({"schedule": {"report_hour": 14}})
        scheduler = Scheduler(FakeService([]), cfg)
        self.assertEqual(scheduler.jobs["report"].at_hour, 14)


class TestReportActivity(unittest.TestCase):
    """活跃上报：企业号零收益要跳过，禁用号不发，成功才记 time。"""

    def setUp(self):
        # 账号之间的 1~3 秒错峰是防封号的，不是被测逻辑：这里把它替换成空操作，
        # 否则每个用例都要真睡两秒。
        from unittest import mock

        patcher = mock.patch("wb2api.scheduler._spread", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reports_personal_accounts_in_both_realms(self):
        accounts = [FakeAccount("cn1", realm="cn"), FakeAccount("g1", realm="global")]
        service = FakeService(accounts)
        scheduler = Scheduler(service, FakeCfg())
        result = scheduler.report_activity()
        self.assertIn("reported=2", result)
        self.assertEqual(len(service.client.calls), 2)
        self.assertTrue(all(url.endswith("/v2/report") for _m, url in service.client.calls))
        self.assertTrue(all(a.extra.get("reported_at") for a in accounts))

    def test_enterprise_accounts_are_skipped(self):
        personal = FakeAccount("p1")
        corporate = FakeAccount("e1")
        corporate.enterprise_id = "ent-1"
        service = FakeService([personal, corporate])
        result = Scheduler(service, FakeCfg()).report_activity()
        self.assertIn("reported=1", result)
        self.assertIn("skipped_enterprise=1", result)
        self.assertEqual(len(service.client.calls), 1, "企业号上报是零收益，不该多发一次请求")

    def test_disabled_and_tokenless_are_skipped(self):
        disabled = FakeAccount("d1", enabled=False)
        tokenless = FakeAccount("t1")
        tokenless.access_token = ""
        service = FakeService([disabled, tokenless])
        result = Scheduler(service, FakeCfg()).report_activity()
        self.assertIn("reported=0", result)
        self.assertEqual(service.client.calls, [])

    def test_realm_filter_from_config(self):
        accounts = [FakeAccount("cn1", realm="cn"), FakeAccount("g1", realm="global")]
        cfg = FakeCfg({"schedule": {"report_realms": ["cn"]}})
        service = FakeService(accounts)
        Scheduler(service, cfg).report_activity()
        self.assertEqual(len(service.client.calls), 1)


class TestRunOnce(unittest.TestCase):
    def test_runs_due_jobs_and_records_result(self):
        service = FakeService([FakeAccount()])
        scheduler = Scheduler(service, FakeCfg())
        results = scheduler.run_once()
        self.assertIn("keepalive", results)
        self.assertIn("renewed=1", results["keepalive"])
        # 第二轮：interval 没到，不该再跑
        self.assertNotIn("keepalive", scheduler.run_once())

    def test_error_is_contained(self):
        scheduler = Scheduler(FakeService([]), FakeCfg())

        def boom():
            raise RuntimeError("炸了")

        scheduler.add(Job("boom", boom, interval=1))
        results = scheduler.run_once()
        self.assertEqual(scheduler.jobs["boom"].errors, 1)
        self.assertIn("error", results["boom"])
        self.assertIn("keepalive", results, "一个任务炸了不该影响其它任务")

    def test_trigger_unknown_returns_false(self):
        scheduler = Scheduler(FakeService([]), FakeCfg())
        self.assertFalse(scheduler.trigger("nope"))
        self.assertTrue(scheduler.trigger("keepalive"))


class TestKeepalive(unittest.TestCase):
    def test_only_renews_near_expiry(self):
        soon = time.time() + 3600
        service = FakeService([
            FakeAccount("soon", expires_at=soon),
            FakeAccount("far", expires_at=time.time() + KEEPALIVE_AHEAD + 3600),
            FakeAccount("unknown", expires_at=0),
            FakeAccount("disabled", expires_at=soon, enabled=False),
            FakeAccount("no-rt", expires_at=soon, refresh_token=""),
        ])
        scheduler = Scheduler(service, FakeCfg())
        result = scheduler.keepalive()
        self.assertEqual(sorted(service.auth.refreshed), ["soon", "unknown"],
                         "快过期的要续；到期时间未知(0)的也续一次，把真实 expiresIn 拿回来")
        self.assertIn("renewed=2", result)

    def test_failure_does_not_stop_the_batch(self):
        soon = time.time() + 60
        service = FakeService(
            [FakeAccount("bad", expires_at=soon), FakeAccount("good", expires_at=soon)],
            fail_refresh=("bad",),
        )
        scheduler = Scheduler(service, FakeCfg())
        result = scheduler.keepalive()
        self.assertEqual(service.auth.refreshed, ["bad", "good"])
        self.assertIn("failed=1", result)
        self.assertIn("renewed=1", result)


class TestCheckin(unittest.TestCase):
    def test_only_configured_realms(self):
        service = FakeService([FakeAccount("cn1", realm="cn"), FakeAccount("g1", realm="global")])
        scheduler = Scheduler(service, FakeCfg({"schedule": {"checkin_realms": ["cn"]}}))
        scheduler.checkin()
        self.assertIn("cn", service.store.realm_queries)
        self.assertNotIn("global", service.store.realm_queries)

    def test_default_is_cn_only(self):
        service = FakeService([FakeAccount("cn1", realm="cn")])
        scheduler = Scheduler(service, FakeCfg())
        scheduler.checkin()
        self.assertEqual([r for r in service.store.realm_queries if r], ["cn"])


if __name__ == "__main__":
    unittest.main()
