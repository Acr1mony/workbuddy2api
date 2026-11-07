"""管理面新增能力的测试：调优参数、批量操作、日志尾巴。

这些接口支撑面板改版（一次点 40 个账号不能真的同时打上游），
所以除了"能返回 200"，更要断言"改配置真的改到了"和"非法值被挡住"。
"""
from __future__ import annotations

import json
import time
import unittest

from tests.test_app import ServerCase
from wb2api import routes_admin
from wb2api.store import Account


def make_account(uid: str, realm: str = "cn", disabled: bool = False) -> Account:
    account = Account(uid=uid, realm=realm, nickname=uid.upper(), access_token="tok-" + uid, disabled=disabled)
    account.ensure_ids()
    return account


class TestTunables(ServerCase):
    def _settings(self):
        token = self.panel_token()
        status, _headers, data = self.request("GET", "/api/settings", headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        return token, self.json_of(data)

    def _save(self, token, body):
        status, _headers, data = self.request(
            "POST", "/api/settings", body, headers={"X-Panel-Token": token}
        )
        return status, self.json_of(data)

    def test_lists_every_tunable_with_metadata(self):
        _token, payload = self._settings()
        items = payload["tunables"]
        self.assertEqual(len(items), len(routes_admin.TUNABLES))
        paths = [item["path"] for item in items]
        self.assertIn("pool.max_in_flight", paths)
        self.assertIn("pool.min_request_interval", paths)
        for item in items:
            self.assertIn(item["kind"], ("int", "float", "duration", "str"))
            self.assertIn("restart", item)
            self.assertIn("value", item)
            self.assertTrue(item["label"])

    def test_value_reflects_current_config(self):
        token, payload = self._settings()
        self.assertEqual(self._save(token, {"tunables": {"pool.max_in_flight": 7}})[0], 200)
        _token, payload = self._settings()
        value = [item for item in payload["tunables"] if item["path"] == "pool.max_in_flight"][0]["value"]
        self.assertEqual(int(value), 7)

    def test_duration_accepts_human_and_number(self):
        token, _payload = self._settings()
        self.assertEqual(self._save(token, {"tunables": {"pool.min_request_interval": "1.5s"}})[0], 200)
        self.assertEqual(self.cfg.get("pool.min_request_interval"), "1.5s")
        self.assertEqual(self._save(token, {"tunables": {"pool.waf_cooldown": 90}})[0], 200)
        self.assertEqual(self.cfg.get("pool.waf_cooldown"), "90s")

    def test_bad_duration_is_rejected_and_nothing_changes(self):
        token, _payload = self._settings()
        before = self.cfg.get("pool.min_request_interval")
        status, payload = self._save(token, {"tunables": {"pool.min_request_interval": "soon"}})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "bad_value")
        self.assertEqual(self.cfg.get("pool.min_request_interval"), before)

    def test_negative_int_is_rejected(self):
        token, _payload = self._settings()
        status, _payload = self._save(token, {"tunables": {"pool.max_in_flight": -1}})
        self.assertEqual(status, 400)

    def test_checkin_hour_upper_bound(self):
        token, _payload = self._settings()
        self.assertEqual(self._save(token, {"tunables": {"schedule.checkin_hour": 24}})[0], 400)
        self.assertEqual(self._save(token, {"tunables": {"schedule.checkin_hour": 7}})[0], 200)
        self.assertEqual(int(self.cfg.get("schedule.checkin_hour")), 7)

    def test_strategy_enum(self):
        token, _payload = self._settings()
        self.assertEqual(self._save(token, {"tunables": {"pool.strategy.cn": "random"}})[0], 400)
        self.assertEqual(self._save(token, {"tunables": {"pool.strategy.cn": "round_robin"}})[0], 200)
        self.assertEqual(self.cfg.get("pool.strategy.cn"), "round_robin")

    def test_unknown_path_is_ignored(self):
        """前向兼容：旧面板存下的键在新版本里消失时不该整单 400。"""
        token, _payload = self._settings()
        status, _payload = self._save(token, {"tunables": {"pool.gone_away": 1}})
        self.assertEqual(status, 200)


class TestBulk(ServerCase):
    def _seed(self):
        self.app.store.upsert_account(make_account("uid-a"))
        self.app.store.upsert_account(make_account("uid-b"))
        self.app.store.upsert_account(make_account("uid-c", realm="global"))
        return self.panel_token()

    def _bulk(self, token, body):
        status, _headers, data = self.request(
            "POST", "/api/accounts/bulk", body, headers={"X-Panel-Token": token}
        )
        return status, self.json_of(data)

    def test_rejects_empty_selection(self):
        status, payload = self._bulk(self._seed(), {"action": "disable", "uids": []})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "empty_selection")

    def test_rejects_unknown_action(self):
        status, payload = self._bulk(self._seed(), {"action": "explode", "all": True})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "bad_action")

    def test_disable_touches_only_the_listed_uids(self):
        token = self._seed()
        status, payload = self._bulk(token, {"action": "disable", "uids": ["uid-a", "uid-b"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["ok_count"], 2)
        self.assertTrue(self.app.store.get_account("uid-a").disabled)
        self.assertTrue(self.app.store.get_account("uid-b").disabled)
        self.assertFalse(self.app.store.get_account("uid-c").disabled)

    def test_enable_undoes_disable(self):
        token = self._seed()
        self._bulk(token, {"action": "disable", "all": True})
        status, payload = self._bulk(token, {"action": "enable", "uids": ["uid-a"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["ok_count"], 1)
        self.assertFalse(self.app.store.get_account("uid-a").disabled)
        self.assertTrue(self.app.store.get_account("uid-b").disabled)

    def test_all_respects_realm(self):
        token = self._seed()
        status, payload = self._bulk(token, {"action": "disable", "all": True, "realm": "global"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 1)
        self.assertTrue(self.app.store.get_account("uid-c").disabled)
        self.assertFalse(self.app.store.get_account("uid-a").disabled)

    def test_unknown_uid_reports_failure_without_stopping(self):
        token = self._seed()
        status, payload = self._bulk(token, {"action": "disable", "uids": ["nope", "uid-a"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["ok_count"], 1)
        failed = [row for row in payload["results"] if not row["ok"]]
        self.assertEqual(len(failed), 1)
        self.assertTrue(self.app.store.get_account("uid-a").disabled)

    def test_checkin_calls_billing_once_per_account(self):
        token = self._seed()
        calls = []

        def fake_checkin(account, realm):
            calls.append(account.uid)
            return True, "签到成功"

        self.app.panel.billing.checkin = fake_checkin
        status, payload = self._bulk(token, {"action": "checkin", "uids": ["uid-a", "uid-b"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["ok_count"], 2)
        self.assertEqual(sorted(calls), ["uid-a", "uid-b"])
        self.assertEqual(payload["results"][0]["detail"], "签到成功")

    def test_report_skips_enterprise_accounts_without_calling_upstream(self):
        token = self._seed()
        enterprise = make_account("uid-ent")
        enterprise.enterprise_id = "ent-1"
        self.app.store.upsert_account(enterprise)
        sent = []

        def fake_report(account, realm, *args, **kwargs):
            sent.append(account.uid)
            return True

        self.app.panel.billing.report = fake_report
        status, payload = self._bulk(token, {"action": "report", "uids": ["uid-a", "uid-ent"]})
        self.assertEqual(status, 200)
        self.assertEqual(sent, ["uid-a"], "企业号上报是零收益，不该白打一次请求")
        rows = {row["uid"]: row for row in payload["results"]}
        self.assertTrue(rows["uid-a"]["ok"])
        self.assertFalse(rows["uid-ent"]["ok"])
        self.assertIn("企业号", rows["uid-ent"]["detail"])


class TestLogs(ServerCase):
    def test_returns_lines_instead_of_raw_text(self):
        token = self.panel_token()
        status, _headers, data = self.request("GET", "/api/logs?lines=5", headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        payload = self.json_of(data)
        self.assertIsInstance(payload["lines"], list)
        self.assertLessEqual(len(payload["lines"]), 5)

    def test_lines_argument_is_clamped(self):
        token = self.panel_token()
        status, _headers, data = self.request("GET", "/api/logs?lines=99999", headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        self.assertIsInstance(self.json_of(data)["lines"], list)


class TestBackup(ServerCase):
    """备份的价值全在"能把号搬回来"，所以导出的必须是能 restore 的真凭据。"""

    def _seed(self):
        self.app.store.upsert_account(make_account("uid-a"))
        return self.panel_token()

    def test_export_contains_tokens_and_forces_download(self):
        token = self._seed()
        status, headers, data = self.request("GET", "/api/accounts/export", headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        lowered = {str(k).lower(): v for k, v in dict(headers).items()}
        self.assertIn("attachment", lowered.get("content-disposition", ""))
        self.assertEqual(lowered.get("cache-control"), "no-store")
        payload = self.json_of(data)
        self.assertEqual(len(payload["accounts"]), 1)
        row = payload["accounts"][0]
        self.assertEqual(row["access_token"], "tok-uid-a", "脱敏导出等于假备份")
        self.assertIn("refresh token", payload["warning"])

    def test_export_requires_auth(self):
        self._seed()
        status, _headers, _data = self.request("GET", "/api/accounts/export")
        self.assertEqual(status, 401)

    def test_import_merges_and_skips_garbage(self):
        token = self._seed()
        body = {"accounts": [
            {"uid": "uid-a", "realm": "cn", "access_token": "new-token", "nickname": "改名了"},
            {"uid": "uid-b", "realm": "global", "access_token": "tok-b"},
            {"realm": "cn"},          # 没有任何凭据 → 跳过
            "not-an-object",          # 结构不对 → 跳过
        ]}
        status, _headers, data = self.request(
            "POST", "/api/accounts/import", body, headers={"X-Panel-Token": token}
        )
        self.assertEqual(status, 200)
        payload = self.json_of(data)
        self.assertEqual(payload["added"], 1)
        self.assertEqual(payload["updated"], 1)
        self.assertEqual(payload["skipped"], 2)
        self.assertEqual(self.app.store.get_account("uid-a").access_token, "new-token")
        self.assertIsNotNone(self.app.store.get_account("uid-b"))

    def test_import_rejects_non_list(self):
        token = self._seed()
        status, _headers, data = self.request(
            "POST", "/api/accounts/import", {"accounts": "nope"}, headers={"X-Panel-Token": token}
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.json_of(data)["error"]["code"], "bad_request")


class TestAccountDetail(ServerCase):
    """单号详情：列表一行讲不完的因果（窗口、模型封禁、这个号花掉的）。"""

    def _seed(self, uid="uid-detail"):
        token = self.panel_token()
        account = make_account(uid)
        account.cooldown_until = time.time() + 600
        account.cooldown_reason = "429 scoped"
        account.model_cooldowns["glm-4.6"] = {"until": time.time() + 300, "reason": "model blocked"}
        self.app.store.upsert_account(account)
        return token, account

    def test_unknown_account_is_404(self):
        token = self.panel_token()
        status, _headers, data = self.request(
            "GET", "/api/accounts/nobody", headers={"X-Panel-Token": token}
        )
        self.assertEqual(status, 404)
        self.assertEqual(self.json_of(data)["error"]["code"], "unknown_account")

    def test_detail_explains_the_window_and_usage(self):
        token, account = self._seed()
        status, _headers, data = self.request(
            "GET", "/api/accounts/" + account.uid, headers={"X-Panel-Token": token}
        )
        self.assertEqual(status, 200)
        payload = self.json_of(data)
        row = payload["account"]
        self.assertEqual(row["uid"], account.uid)
        self.assertGreater(row["throttle_for"], 0, "冷却中就该报剩余秒数")
        self.assertTrue(row["throttle_reason"])
        self.assertIn("usage", payload)
        self.assertIn("recent", payload)
        cooldowns = payload["model_cooldowns"]
        self.assertIn("glm-4.6", cooldowns)
        self.assertGreater(cooldowns["glm-4.6"]["for"], 0)
        self.assertNotIn("tok-" + account.uid, json.dumps(payload), "详情里绝不能出现令牌明文")
        self.assertTrue(row["has_access_token"])

    def test_export_route_still_wins_over_detail(self):
        token = self.panel_token()
        status, headers, _data = self.request(
            "GET", "/api/accounts/export", headers={"X-Panel-Token": token}
        )
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("content-disposition", ""))

    def test_requires_panel_token(self):
        _token, account = self._seed()
        status, _headers, _data = self.request("GET", "/api/accounts/" + account.uid)
        self.assertEqual(status, 401)


class TestPoolAffinity(ServerCase):
    """池状态里带上会话亲和：命中率低 = 粘滞没起作用（TTFT 会很难看）。"""

    def test_pool_reports_affinity(self):
        token = self.panel_token()
        status, _headers, data = self.request("GET", "/api/pool", headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        payload = self.json_of(data)
        self.assertIn("ready", payload)
        aff = payload["affinity"]
        self.assertIn("sessions", aff)
        self.assertIn("hit_rate", aff)
        self.assertIn("page_size", aff)
        self.assertEqual(aff["page_size"], self.app.service.affinity.page_size)

    def test_affinity_counts_a_bind(self):
        token = self.panel_token()
        self.app.service.affinity.bind("conv-1", "uid-a")
        status, _headers, data = self.request("GET", "/api/pool", headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(data)["affinity"]["sessions"], 1)


class TestReportAction(ServerCase):
    """活跃上报：企业号面板上直接说不做，别让人以为"点过了"。"""

    class _Billing:
        def __init__(self):
            self.calls = []

        def report(self, account, realm, payload=None, **kw):
            self.calls.append((account.uid, realm))
            return True

    def _stub(self):
        stub = self._Billing()
        self.app.panel.billing = stub
        return stub

    def test_report_marks_the_account(self):
        token = self.panel_token()
        account = make_account("uid-report")
        self.app.store.upsert_account(account)
        stub = self._stub()
        status, _headers, data = self.request(
            "POST", "/api/accounts/uid-report/report", headers={"X-Panel-Token": token}
        )
        self.assertEqual(status, 200)
        self.assertTrue(self.json_of(data)["ok"])
        self.assertEqual(stub.calls, [("uid-report", "cn")])
        saved = self.app.store.get_account("uid-report")
        self.assertTrue(saved.extra.get("reported_at"), "上报成功要留痕，否则第二天看不出做过没有")

    def test_enterprise_account_is_refused_with_a_reason(self):
        token = self.panel_token()
        account = make_account("uid-ent")
        account.enterprise_id = "ent-42"
        self.app.store.upsert_account(account)
        stub = self._stub()
        status, _headers, data = self.request(
            "POST", "/api/accounts/uid-ent/report", headers={"X-Panel-Token": token}
        )
        self.assertEqual(status, 200)
        payload = self.json_of(data)
        self.assertFalse(payload["ok"])
        self.assertIn("企业", payload["message"])
        self.assertEqual(stub.calls, [], "企业号上报零收益，一次都不该发")

    def test_report_unknown_account_is_404(self):
        token = self.panel_token()
        self._stub()
        status, _headers, _data = self.request(
            "POST", "/api/accounts/nobody/report", headers={"X-Panel-Token": token}
        )
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
