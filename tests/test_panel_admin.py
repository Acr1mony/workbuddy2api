"""管理面新增能力的测试：调优参数、批量操作、日志尾巴。

这些接口支撑面板改版（一次点 40 个账号不能真的同时打上游），
所以除了"能返回 200"，更要断言"改配置真的改到了"和"非法值被挡住"。
"""
from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
