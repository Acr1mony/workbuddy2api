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


class TestPauseAndPoolViews(ServerCase):
    """暂停是"人工按住"：既不是停用（令牌还在），也不是冷却（不是上游给的）。"""

    def test_pause_and_resume_round_trip(self):
        token = self.panel_token()
        self.app.store.upsert_account(make_account("uid-pause"))
        status, _headers, data = self.request(
            "POST", "/api/accounts/uid-pause/pause", headers={"X-Panel-Token": token}
        )
        self.assertEqual(status, 200)
        self.assertTrue(self.json_of(data)["paused"])
        saved = self.app.store.get_account("uid-pause")
        self.assertTrue(saved.paused)
        self.assertEqual(saved.state, "paused")
        self.assertFalse(saved.disabled, "暂停不是停用：令牌和窗口都留着")

        status, _headers, data = self.request(
            "POST", "/api/accounts/uid-pause/resume", headers={"X-Panel-Token": token}
        )
        self.assertEqual(status, 200)
        self.assertFalse(self.app.store.get_account("uid-pause").paused)

    def test_bulk_pause_and_resume(self):
        token = self.panel_token()
        for uid in ("uid-b1", "uid-b2"):
            self.app.store.upsert_account(make_account(uid))
        status, _headers, data = self.request(
            "POST", "/api/accounts/bulk", {"action": "pause", "uids": ["uid-b1", "uid-b2"]},
            headers={"X-Panel-Token": token},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(data)["ok_count"], 2)
        self.assertTrue(all(self.app.store.get_account(u).paused for u in ("uid-b1", "uid-b2")))

    def test_pool_exposes_paused_count_model_locks_and_waf(self):
        token = self.panel_token()
        account = make_account("uid-lock")
        self.app.store.upsert_account(account)
        self.app.panel.pool.block_model(self.app.store.get_account("uid-lock"), "glm-4.6", "no service")
        paused = make_account("uid-held")
        paused.paused = True
        self.app.store.upsert_account(paused)

        status, _headers, data = self.request("GET", "/api/pool", headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        payload = self.json_of(data)
        self.assertEqual(payload["paused"], 1)
        self.assertIn("glm-4.6", payload["model_locks"])
        self.assertEqual(payload["model_locks"]["glm-4.6"]["accounts"], 1)
        self.assertIn("waf", payload, "面板要能显示出口被 WAF 盯上了")
        self.assertFalse(payload["waf"]["cn"]["active"])

    def test_key_concurrency_is_reported_and_validated(self):
        token = self.panel_token()
        status, _headers, data = self.request(
            "POST", "/api/settings",
            {"keys_add": [{"name": "limited", "max_concurrency": 2}]},
            headers={"X-Panel-Token": token},
        )
        self.assertEqual(status, 200)
        added = self.json_of(data)["added"][0]
        self.assertEqual(added["max_concurrency"], 2)

        _status, _headers, data = self.request("GET", "/api/settings", headers={"X-Panel-Token": token})
        keys = {item["name"]: item for item in self.json_of(data)["api_keys"]}
        self.assertEqual(keys["limited"]["max_concurrency"], 2)


class TestMetricsAndKeyRate(ServerCase):
    """指标页与 key 速率上限：面板要能读到刚发生的请求的延迟分布。"""

    def test_metrics_endpoint_shape(self):
        token = self.panel_token()
        status, _headers, data = self.request("GET", "/api/metrics", headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        payload = self.json_of(data)
        for key in ("window", "requests", "ttfb_ms", "elapsed_ms", "outcomes", "attempts", "models"):
            self.assertIn(key, payload)
        self.assertIn("p99", payload["ttfb_ms"])
        self.assertIn("error_rate", payload["requests"])

    def test_metrics_requires_auth(self):
        status, _headers, _data = self.request("GET", "/api/metrics")
        self.assertEqual(status, 401)

    def test_key_rate_limit_round_trip(self):
        token = self.panel_token()
        status, _headers, data = self.request(
            "POST", "/api/settings",
            {"keys_add": [{"name": "rate-limited", "rate_limit": 30}]},
            headers={"X-Panel-Token": token},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(data)["added"][0]["rate_limit"], 30)
        _status, _headers, data = self.request("GET", "/api/settings", headers={"X-Panel-Token": token})
        keys = {item["name"]: item for item in self.json_of(data)["api_keys"]}
        self.assertEqual(keys["rate-limited"]["rate_limit"], 30)


if __name__ == "__main__":
    unittest.main()



class TestObservability(ServerCase):
    """/api/requests、/api/dumps、/api/proxies：面板要能看见"刚才那条请求怎么了"。"""

    def _get(self, path, token):
        return self.request("GET", path, headers={"X-Panel-Token": token})

    def _post(self, path, body, token):
        return self.request("POST", path, body, headers={"X-Panel-Token": token})

    def test_request_log_endpoint_shapes(self):
        self.service.reqlog.record({
            "at": 1700000000, "model": "deepseek-v3", "account": "u1", "status": 200,
            "outcome": "completed", "path": "/v1/chat/completions", "request_id": "req_1",
            "prompt_tokens": 3, "completion_tokens": 4, "credit": 0.01,
        })
        token = self.panel_token()
        status, _headers, data = self._get("/api/requests", token)
        self.assertEqual(status, 200)
        payload = self.json_of(data)
        self.assertEqual(payload["items"][0]["request_id"], "req_1")
        self.assertIn("buffered", payload["stats"])

    def test_request_log_filters(self):
        for request_id, code in (("req_a", 200), ("req_b", 503)):
            self.service.reqlog.record({
                "at": 1700000000, "model": "glm-5.3", "account": "u1", "status": code,
                "outcome": "completed" if code == 200 else "failed", "request_id": request_id,
            })
        token = self.panel_token()
        status, _headers, data = self._get("/api/requests?status=5xx", token)
        self.assertEqual(status, 200)
        self.assertEqual([row["request_id"] for row in self.json_of(data)["items"]], ["req_b"])

    def test_dump_endpoint_and_unknown_id(self):
        self.service.diag.dump(request_id="req_x", model="deepseek-v3", status=400, code="11101",
                               body='{"code":11101}', request_headers={"Authorization": "Bearer t"})
        token = self.panel_token()
        status, _headers, data = self._get("/api/dumps", token)
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(data)["items"][0]["code"], "11101")

        status, _headers, data = self._get("/api/dumps/req_x", token)
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(data)["request_headers"]["authorization"], "**redacted**")

        status, _headers, data = self._get("/api/dumps/nope", token)
        self.assertEqual(status, 404)
        self.assertEqual(self.json_of(data)["error"]["code"], "unknown_dump")

    def test_dump_preview_shows_sanitized_text_without_calling_upstream(self):
        self.service.diag.dump(
            request_id="req_p", model="deepseek-v3", realm="cn", status=400, code="11-128",
            body='{"code":"11-128"}', request_headers={},
            request_body=json.dumps({
                "model": "deepseek-v3",
                "messages": [
                    {"role": "system", "content": "You are an AI agent powered by DeepSeek Harness."},
                    {"role": "user", "content": "hi"},
                ],
            }),
        )
        token = self.panel_token()
        from tests.test_service import FakeClient
        original = self.service.client
        fake = FakeClient([])
        self.service.client = fake
        try:
            status, _headers, data = self._get("/api/dumps/req_p/preview", token)
        finally:
            self.service.client = original
        self.assertEqual(status, 200)
        payload = self.json_of(data)
        self.assertTrue(payload["changed"])
        self.assertIn("helpful AI assistant", payload["system_after"][0])
        # 预览绝对不能打上游（只是看字面会变成什么）
        self.assertEqual(fake.calls, [])

    def test_dump_replay_unknown_id_is_404(self):
        token = self.panel_token()
        status, _headers, data = self._post("/api/dumps/nope/replay", {"sanitize": True}, token)
        self.assertEqual(status, 404)
        self.assertEqual(self.json_of(data)["error"]["code"], "unknown_dump")

    def test_proxies_crud_roundtrip(self):
        token = self.panel_token()
        # 老客户端还会带 realm，服务端应当忽略它而不是报错。
        status, _headers, data = self._post(
            "/api/proxies", {"action": "add", "url": "http://10.0.0.9:3128", "realm": "cn", "weight": 2}, token)
        self.assertEqual(status, 200)
        payload = self.json_of(data)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["entries"][0]["weight"], 2)
        self.assertEqual(payload["entries"][0]["accounts"], [], "刚加进来的出口还没绑号")
        self.assertEqual(payload["free"], 1)
        self.assertNotIn("realm", payload["entries"][0])

        status, _headers, data = self._get("/api/proxies", token)
        self.assertEqual(self.json_of(data)["total"], 1)

        status, _headers, data = self._post(
            "/api/proxies", {"action": "update", "url": "http://10.0.0.9:3128", "enabled": False}, token)
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(data)["ready"], 0)

        status, _headers, data = self._post(
            "/api/proxies", {"action": "remove", "url": "http://10.0.0.9:3128"}, token)
        self.assertEqual(self.json_of(data)["total"], 0)

    def test_proxies_reject_duplicate_and_unknown(self):
        token = self.panel_token()
        self._post("/api/proxies", {"action": "add", "url": "http://a:1"}, token)
        status, _headers, data = self._post("/api/proxies", {"action": "add", "url": "http://a:1"}, token)
        self.assertEqual(status, 400)
        self.assertEqual(self.json_of(data)["error"]["code"], "duplicate_proxy")
        status, _headers, data = self._post("/api/proxies", {"action": "remove", "url": "http://nope:1"}, token)
        self.assertEqual(status, 404)

    def test_account_rows_show_which_exit_they_use(self):
        """面板上要能直接看到"这个号走哪个出口"，否则出口池就是黑盒。"""
        token = self.panel_token()
        self.app.store.upsert_account(make_account("uid-exit"))
        self._post("/api/proxies", {"action": "add", "url": "http://exit:9"}, token)
        account = self.app.store.get_account("uid-exit")
        proxy = self.app.panel.service._egress_for(account)
        self.assertEqual(proxy, "http://exit:9")

        status, _headers, data = self._get("/api/accounts", token)
        self.assertEqual(status, 200)
        rows = {row["uid"]: row for row in self.json_of(data)["accounts"]}
        self.assertTrue(rows["uid-exit"]["egress"], "列表里要写出口（打码后的）")
        self.assertEqual(rows["uid-exit"]["egress_shared"], 1)

        status, _headers, data = self._get("/api/accounts/uid-exit", token)
        self.assertEqual(status, 200)
        detail = self.json_of(data)
        self.assertEqual(detail["account"]["egress"], rows["uid-exit"]["egress"])
        self.assertFalse(detail["account"]["egress_manual"])

        status, _headers, data = self._post("/api/proxies", {"action": "rebind"}, token)
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(data)["bound"], 0, "重新分配后绑定清空")
        self.assertFalse(self.app.store.get_account("uid-exit").extra.get("egress"))

    def test_observability_requires_login(self):
        for path in ("/api/requests", "/api/dumps", "/api/proxies"):
            status, _headers, _data = self.request("GET", path)
            self.assertEqual(status, 401, path)

class _FakeInfo(object):
    """只放 catalog_view 真正会读的字段，避免把 ModelInfo 的全套依赖拖进测试。"""

    def __init__(self, model_id, name, realm, max_output=0, context_length=0, alias_of=""):
        self.id = model_id
        self.name = name
        self.realm = realm
        self.max_output = max_output
        self.context_length = context_length
        self.alias_of = alias_of


class TestModelCatalog(ServerCase):
    """面板"限定模型"下拉要可选项：两个域合并、能按域过滤、隐藏模型不出现。"""

    def _stub(self):
        by_realm = {
            "cn": [
                _FakeInfo("deepseek-v4-flash", "DeepSeek V4 Flash", "cn", max_output=0),
                _FakeInfo("hidden-one", "Hidden", "cn"),
            ],
            "global": [
                _FakeInfo("glm-5.3", "GLM 5.3", "global", max_output=16000, context_length=200000),
                _FakeInfo("gpt-4o", "GPT-4o", "global", alias_of="deepseek-v4-flash"),
            ],
        }

        class _Catalog(object):
            def entries(self, realm, account=None, chat_only=True):
                return by_realm.get(realm, [])

            def hidden(self, model_id):
                return model_id == "hidden-one"

        self.service.catalog = _Catalog()
        self.service._ensure_catalog = lambda realm: None

    def test_requires_login(self):
        status, _headers, _data = self.request("GET", "/api/models")
        self.assertEqual(status, 401)

    def test_lists_both_realms_and_skips_hidden(self):
        self._stub()
        token = self.panel_token()
        status, _headers, data = self.request("GET", "/api/models", headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        payload = self.json_of(data)
        ids = [row["id"] for row in payload["models"]]
        self.assertNotIn("hidden-one", ids, "面板里藏起来的模型不该出现在下拉里")
        self.assertEqual(ids, ["deepseek-v4-flash", "glm-5.3", "gpt-4o"], "按 域→id 排序")
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["realms"], {"cn": 1, "global": 2})

    def test_keeps_capabilities_and_alias(self):
        self._stub()
        token = self.panel_token()
        _status, _headers, data = self.request("GET", "/api/models", headers={"X-Panel-Token": token})
        rows = {row["id"]: row for row in self.json_of(data)["models"]}
        self.assertEqual(rows["glm-5.3"]["max_output_tokens"], 16000)
        self.assertEqual(rows["glm-5.3"]["context_length"], 200000)
        self.assertEqual(rows["gpt-4o"]["alias_of"], "deepseek-v4-flash")
        self.assertNotIn("max_output_tokens", rows["deepseek-v4-flash"],
                         "0 表示不知道，不要编一个数字出来")

    def test_realm_filter(self):
        self._stub()
        token = self.panel_token()
        _status, _headers, data = self.request(
            "GET", "/api/models?realm=global", headers={"X-Panel-Token": token}
        )
        payload = self.json_of(data)
        self.assertEqual([row["id"] for row in payload["models"]], ["glm-5.3", "gpt-4o"])
        self.assertEqual(payload["realms"], {"global": 2})


class TestSettingsPageLayout(ServerCase):
    """设置页改版后的结构：左侧目录 + 每项一行，两者缺一个都没法用。"""

    def test_settings_page_has_nav_and_blocks(self):
        token = self.panel_token()
        status, _headers, data = self.request("GET", "/panel/", headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        html = data.decode("utf-8")
        self.assertIn('id="settings-nav"', html)
        for block in ("set-gateway", "set-keys", "set-prompt", "set-proxies", "set-tunables", "set-guide"):
            self.assertIn('data-target="%s"' % block, html)
            self.assertIn('id="%s"' % block, html)

    def test_model_picker_markup(self):
        token = self.panel_token()
        _status, _headers, data = self.request("GET", "/panel/", headers={"X-Panel-Token": token})
        html = data.decode("utf-8")
        for anchor in ("key-models-control", "key-models-chips", "key-models-search",
                       "key-models-menu", "key-models-list", "key-models-all", "key-models-none"):
            self.assertIn('id="%s"' % anchor, html)

    def test_proxy_hint_says_socks5(self):
        token = self.panel_token()
        _status, _headers, data = self.request("GET", "/panel/", headers={"X-Panel-Token": token})
        html = data.decode("utf-8")
        self.assertIn("socks5://", html)
        self.assertNotIn("socks5h", html, "socks5h 是 curl 的写法，用户填进来会连不上")
