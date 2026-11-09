"""长期运行的"咬人"点：槽位泄漏、坏文件静默降级、安全头、别名、请求 id。

这些用例覆盖的都不是"新功能"，而是"跑着跑着突然全挂"的那一类问题——
它们不写测试的话，只有真机跑几天才会以 503 的形式暴露出来。
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_app import ServerCase
from tests.test_service import FakeClient, FakeResponse, ok_stream, sse_lines, chat_chunk
from wb2api import models, util
from wb2api.config import Config
from wb2api.service import GatewayService
from wb2api.store import Account, Store


class FlakyResponse(FakeResponse):
    """读到一半抛异常：模拟聚合阶段炸掉（上游断流、解析失败）。"""

    def iter_text(self, idle_timeout=300.0):
        for line in self.lines:
            yield line
        raise RuntimeError("boom")


class ServiceHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-hard-")
        self.cfg = Config(None, {"data_dir": self.tmp, "panel": {"enabled": False}})
        store = Store(self.tmp)
        for uid in ("u1", "u2"):
            store.upsert_account(Account(
                uid=uid, id=uid, realm="cn", access_token="tok-" + uid,
                refresh_token="rt-" + uid, expires_at=time.time() + 86400,
            ))

    def service(self, script, cfg=None):
        client = FakeClient(script)
        return GatewayService(cfg or self.cfg, Store(self.tmp), client=client), client

    def request(self, payload):
        from wb2api.httpserver import Request

        return Request("POST", "/v1/chat/completions", {}, {},
                       json.dumps(payload).encode(), ("1.2.3.4", 1))

    def body(self, **extra):
        payload = {"model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}]}
        payload.update(extra)
        return payload

    # -- 槽位泄漏 -----------------------------------------------------------
    def test_slot_released_when_aggregation_fails(self):
        """聚合阶段抛异常也必须把 in_flight 还回去。

        泄漏一次就少一个并发额度（默认 3），泄漏三次这个域就永远是 503
        no_account——只有重启能救。
        """
        svc, _client = self.service([FlakyResponse(sse_lines(chat_chunk("a")))])
        with self.assertRaises(RuntimeError):
            svc.chat_completions(self.request(self.body()))
        self.assertEqual(sum(svc.pool.in_flight.values()) if hasattr(svc.pool, "in_flight") else 0, 0)
        self.assertEqual(svc.store.list_accounts("cn")[0].in_flight, 0)
        for account in svc.store.list_accounts("cn"):
            self.assertEqual(account.in_flight, 0, "账号 %s 的槽位没还" % account.uid)

    def test_slot_released_after_stream(self):
        svc, _client = self.service([ok_stream("hi")])
        resp = svc.chat_completions(self.request(self.body(stream=True)))
        list(resp.stream)
        for account in svc.store.list_accounts("cn"):
            self.assertEqual(account.in_flight, 0)

    def test_slot_released_when_client_aborts_stream(self):
        svc, _client = self.service([ok_stream("hi")])
        resp = svc.chat_completions(self.request(self.body(stream=True)))
        gen = resp.stream
        next(gen)          # 假装客户端读了一帧就走
        gen.close()
        for account in svc.store.list_accounts("cn"):
            self.assertEqual(account.in_flight, 0)

    # -- 请求 id + 别名 -----------------------------------------------------
    def test_request_id_echoed_and_recorded(self):
        svc, _client = self.service([ok_stream("hi")])
        resp = svc.chat_completions(self.request(self.body()))
        request_id = resp.headers.get("X-Request-Id")
        self.assertTrue(request_id and request_id.startswith("req_"))
        event = svc.ledger.recent(1)[0]
        self.assertEqual(event.get("request_id"), request_id)
        self.assertEqual(event.get("attempts"), 1)

    def test_model_alias_is_mapped_upstream(self):
        cfg = Config(None, {
            "data_dir": self.tmp,
            "panel": {"enabled": False},
            "models": {"aliases": {"GPT-4o": "deepseek-v3"}},
        })
        svc, client = self.service([ok_stream("hi")], cfg=cfg)
        resp = svc.chat_completions(self.request(self.body(model="gpt-4o")))
        self.assertEqual(json.loads(client.calls[0]["body"])["model"], "deepseek-v3")
        self.assertEqual(resp.headers.get("X-Model-Alias"), "gpt-4o")
        self.assertEqual(svc.ledger.recent(1)[0].get("alias"), "gpt-4o")

    def test_alias_respects_key_whitelist_by_either_name(self):
        """白名单里写别名或写真名都该放行，否则"配了别名却用不了"。"""
        from wb2api.store import ApiKey

        cfg = Config(None, {
            "data_dir": self.tmp,
            "panel": {"enabled": False},
            "models": {"aliases": {"sonnet": "deepseek-v3"}},
        })
        svc, _client = self.service([ok_stream("hi")], cfg=cfg)
        key = ApiKey(key="sk-a", models=["sonnet"])
        svc.store.add_key(key)
        request = self.request(self.body(model="sonnet"))
        request.headers = {"Authorization": "Bearer sk-a"}
        resp = svc.chat_completions(request)
        self.assertEqual(json.loads(resp.body)["object"], "chat.completion")


class CatalogAliasTest(unittest.TestCase):
    def cfg(self, **models_cfg):
        base = {"data_dir": "", "models": dict(models_cfg)}
        return Config(None, base)

    def test_openai_list_includes_alias_entries(self):
        catalog = models.ModelCatalog(None, self.cfg(aliases={"gpt-4o": "deepseek-v3"}))
        catalog._entries["cn"] = [
            models.ModelInfo(id="deepseek-v3", name="DeepSeek V3", realm="cn", max_output=8000),
        ]
        catalog._fetched_at["cn"] = time.time()
        listing = catalog.openai_list("cn")
        by_id = {item["id"]: item for item in listing["data"]}
        self.assertIn("gpt-4o", by_id)
        self.assertEqual(by_id["gpt-4o"]["alias_of"], "deepseek-v3")
        self.assertEqual(by_id["gpt-4o"]["max_output_tokens"], 8000, "别名继承真名的能力")

    def test_alias_hidden_when_target_absent(self):
        catalog = models.ModelCatalog(None, self.cfg(aliases={"gpt-4o": "nope"}))
        catalog._entries["cn"] = [models.ModelInfo(id="deepseek-v3", realm="cn")]
        catalog._fetched_at["cn"] = time.time()
        ids = [item["id"] for item in catalog.openai_list("cn")["data"]]
        self.assertNotIn("gpt-4o", ids)

    def test_context_length_from_config(self):
        catalog = models.ModelCatalog(None, self.cfg(context_lengths={"deepseek-v3": 128000}))
        catalog._entries["cn"] = [models.ModelInfo(id="deepseek-v3", realm="cn")]
        catalog._fetched_at["cn"] = time.time()
        item = [x for x in catalog.openai_list("cn")["data"] if x["id"] == "deepseek-v3"][0]
        self.assertEqual(item["context_length"], 128000)

    def test_unknown_context_length_is_omitted(self):
        catalog = models.ModelCatalog(None, self.cfg())
        catalog._entries["cn"] = [models.ModelInfo(id="deepseek-v3", realm="cn")]
        catalog._fetched_at["cn"] = time.time()
        item = catalog.openai_list("cn")["data"][0]
        self.assertNotIn("context_length", item, "不知道就别编")


class CorruptStateTest(unittest.TestCase):
    def test_strict_read_raises_and_keeps_backup(self):
        tmp = tempfile.mkdtemp(prefix="wb2a-corrupt-")
        path = os.path.join(tmp, "accounts.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"accounts": [ }')
        with self.assertRaises(util.CorruptFile) as ctx:
            util.read_json(path, {}, strict=True)
        self.assertTrue(ctx.exception.backup)
        self.assertTrue(os.path.exists(ctx.exception.backup), "坏文件必须留一份")
        self.assertFalse(os.path.exists(path))

    def test_lenient_read_still_returns_default(self):
        tmp = tempfile.mkdtemp(prefix="wb2a-corrupt2-")
        path = os.path.join(tmp, "x.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not json")
        self.assertEqual(util.read_json(path, {"ok": 1}), {"ok": 1})

    def test_store_refuses_to_start_on_corrupt_accounts(self):
        tmp = tempfile.mkdtemp(prefix="wb2a-corrupt3-")
        with open(os.path.join(tmp, "accounts.json"), "w", encoding="utf-8") as handle:
            handle.write('{"version": 1, "accounts": [ {"uid": } ]}')
        with self.assertRaises(util.CorruptFile):
            Store(tmp).load()

    def test_config_refuses_to_load_corrupt_file(self):
        tmp = tempfile.mkdtemp(prefix="wb2a-corrupt4-")
        path = os.path.join(tmp, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ bad json")
        with self.assertRaises(util.CorruptFile):
            Config.load(path)


class PanelSecurityHeaderTest(ServerCase):
    def test_panel_page_has_lockdown_headers(self):
        status, headers, _body = self.request("GET", "/panel/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-frame-options"), "DENY")
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertIn("frame-ancestors 'none'", headers.get("content-security-policy", ""))
        self.assertNotIn("access-control-allow-origin", headers, "面板页面不该有跨源许可")

    def test_api_keeps_cors_for_client_devices(self):
        status, headers, _body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("access-control-allow-origin"), "*")
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")


class ForwardedForTest(unittest.TestCase):
    def test_xff_ignored_unless_trusted(self):
        from wb2api.httpserver import Request

        req = Request("GET", "/", {}, {"X-Forwarded-For": "9.9.9.9"}, b"", ("1.2.3.4", 5))
        self.assertEqual(req.client_ip, "1.2.3.4", "默认不认 XFF：带了头就能绕过登录限流")
        req.trust_proxy = True
        self.assertEqual(req.client_ip, "9.9.9.9")


class RandHexTest(unittest.TestCase):
    def test_rand_hex_is_hex_and_length_exact(self):
        value = util.rand_hex(36)
        self.assertEqual(len(value), 36)
        int(value, 16)  # 不是十六进制会抛 ValueError


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
