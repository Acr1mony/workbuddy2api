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
from wb2api.service import GatewayService, ServiceError
from wb2api.store import Account, Store


class FlakyResponse(FakeResponse):
    """读到一半抛异常：模拟聚合阶段炸掉（上游断流、解析失败）。"""

    def iter_text(self, idle_timeout=300.0):
        for line in self.lines:
            yield line
        raise RuntimeError("boom")


class SlowTailResponse(FakeResponse):
    """出字之后沉默一会儿再结束：心跳应该出现在这段沉默里。"""

    def __init__(self, lines, delay=0.25):
        super().__init__(lines)
        self.delay = delay

    def iter_text(self, idle_timeout=300.0):
        for line in self.lines:
            if line.startswith("data: [DONE]"):
                time.sleep(self.delay)
            yield line


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
    def test_stream_heartbeat_when_upstream_goes_quiet(self):
        self.cfg.set("upstream.stream_heartbeat_seconds", 0.05)
        svc, _client = self.service([SlowTailResponse(sse_lines(chat_chunk("hi"), "data: [DONE]"))])
        resp = svc.chat_completions(self.request(self.body(stream=True)))
        frames = list(resp.stream)
        self.assertIn(b": keepalive\n\n", frames)
        # 心跳不能替代结尾帧：客户端还得收到 [DONE]
        self.assertTrue(any(b"[DONE]" in frame for frame in frames))

    def test_heartbeat_is_off_by_zero(self):
        self.cfg.set("upstream.stream_heartbeat_seconds", 0)
        svc, _client = self.service([SlowTailResponse(sse_lines(chat_chunk("hi"), "data: [DONE]"), delay=0.15)])
        resp = svc.chat_completions(self.request(self.body(stream=True)))
        self.assertNotIn(b": keepalive\n\n", list(resp.stream))

    def test_heartbeat_does_not_count_as_first_byte(self):
        self.cfg.set("upstream.stream_heartbeat_seconds", 0.05)
        svc, _client = self.service([SlowTailResponse(sse_lines(chat_chunk("hi"), "data: [DONE]"), delay=0.2)])
        resp = svc.chat_completions(self.request(self.body(stream=True)))
        list(resp.stream)
        event = svc.ledger.recent(1)[0]
        # 首字时间是"第一个真 token"，不是心跳间隔
        self.assertIsNotNone(event.get("ttft_ms"))
        self.assertGreaterEqual(float(event["ttft_ms"]), 0.0)

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


class PausedAccountTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-paused-")
        self.cfg = Config(None, {"data_dir": self.tmp, "panel": {"enabled": False}})
        self.store = Store(self.tmp)
        self.store.upsert_account(Account(uid="p1", id="p1", realm="cn", access_token="tok",
                                          refresh_token="rt", expires_at=time.time() + 86400))
        self.svc = GatewayService(self.cfg, self.store, client=FakeClient([ok_stream("hi")]))

    def test_paused_account_is_not_picked(self):
        self.store.get_account("p1").paused = True
        from wb2api.httpserver import Request

        request = Request("POST", "/v1/chat/completions", {}, {}, json.dumps({
            "model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}],
        }).encode(), ("1.2.3.4", 1))
        with self.assertRaises(Exception) as ctx:
            self.svc.chat_completions(request)
        self.assertEqual(getattr(ctx.exception, "code", ""), "no_account", "暂停不该变成别的原因")

    def test_paused_survives_restart_and_is_reported(self):
        account = self.store.get_account("p1")
        account.paused = True
        account.state = "paused"
        self.store.upsert_account(account)
        again = Store(self.tmp)
        again.load()
        self.assertTrue(again.get_account("p1").paused, "暂停要能重启后还记得")
        self.assertTrue(again.get_account("p1").masked()["paused"])

    def test_pool_reports_paused_separately_from_cooling(self):
        account = self.store.get_account("p1")
        account.paused = True
        self.store.upsert_account(account)
        stats = self.svc.pool.stats("cn")
        self.assertEqual(stats["paused"], 1)
        self.assertEqual(stats["cooling"], 0, "暂停不是冷却，面板上得分开说")

    def test_unavailable_reason_says_paused(self):
        from wb2api.pool import unavailable_reason

        account = self.store.get_account("p1")
        account.paused = True
        info = unavailable_reason(account)
        self.assertEqual(info["kind"], "paused")


class KeyConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-keyslot-")
        self.cfg = Config(None, {"data_dir": self.tmp, "panel": {"enabled": False}})
        self.store = Store(self.tmp)
        self.store.upsert_account(Account(uid="k1", id="k1", realm="cn", access_token="tok",
                                          refresh_token="rt", expires_at=time.time() + 86400))

    def test_second_request_over_the_limit_is_429(self):
        from wb2api.store import ApiKey
        from wb2api.httpserver import Request

        key = ApiKey(key="sk-lim", name="limited", realm="cn", max_concurrency=1)
        self.store.add_key(key)
        svc = GatewayService(self.cfg, self.store, client=FakeClient([ok_stream("a"), ok_stream("b")]))
        body = json.dumps({
            "model": "deepseek-v3", "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        first = Request("POST", "/v1/chat/completions", {}, {"Authorization": "Bearer sk-lim"}, body, ("1.2.3.4", 1))
        response = svc.chat_completions(first)  # 流没消费，名额还占着
        second = Request("POST", "/v1/chat/completions", {}, {"Authorization": "Bearer sk-lim"}, body, ("1.2.3.4", 2))
        with self.assertRaises(Exception) as ctx:
            svc.chat_completions(second)
        self.assertEqual(getattr(ctx.exception, "code", ""), "key_concurrency_exceeded")
        list(response.stream)
        third = Request("POST", "/v1/chat/completions", {}, {"Authorization": "Bearer sk-lim"}, body, ("1.2.3.4", 3))
        self.assertEqual(svc.chat_completions(third).status, 200, "上一次结束后名额要还回来")

    def test_no_limit_means_no_counter(self):
        from wb2api.httpserver import Request

        svc = GatewayService(self.cfg, self.store, client=FakeClient([ok_stream("a")]))
        self.assertEqual(svc.key_inflight("anything"), 0)
        request = Request("POST", "/v1/chat/completions", {}, {}, json.dumps({
            "model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}],
        }).encode(), ("1.2.3.4", 1))
        self.assertEqual(svc.chat_completions(request).status, 200)


class KeyRateTest(unittest.TestCase):
    """速率是滑动窗口：放过去的量必须真的按分钟算，不能变成"整分钟放两倍"。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-keyrate-")
        self.cfg = Config(None, {"data_dir": self.tmp, "panel": {"enabled": False}})
        self.store = Store(self.tmp)
        self.store.upsert_account(Account(uid="r1", id="r1", realm="cn", access_token="tok",
                                          refresh_token="rt", expires_at=time.time() + 86400))

    def _service(self, rate_limit):
        from wb2api.store import ApiKey

        key = ApiKey(key="sk-rate", name="rate", realm="cn", rate_limit=rate_limit)
        self.store.add_key(key)
        return GatewayService(self.cfg, self.store, client=FakeClient([ok_stream("a") for _ in range(10)]))

    def _request(self):
        from wb2api.httpserver import Request

        body = json.dumps({
            "model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        return Request("POST", "/v1/chat/completions", {}, {"Authorization": "Bearer sk-rate"}, body, ("1.2.3.4", 1))

    def test_over_rpm_is_429_with_retry_after(self):
        svc = self._service(rate_limit=2)
        self.assertEqual(svc.chat_completions(self._request()).status, 200)
        self.assertEqual(svc.chat_completions(self._request()).status, 200)
        with self.assertRaises(Exception) as ctx:
            svc.chat_completions(self._request())
        error = ctx.exception
        self.assertEqual(getattr(error, "code", ""), "key_rate_limited")
        self.assertGreater(float(getattr(error, "retry_after", 0) or 0), 0.0)

    def test_window_slides_not_fixed_bucket(self):
        svc = self._service(rate_limit=1)
        self.assertEqual(svc.chat_completions(self._request()).status, 200)
        # 把窗口里那次请求的时间戳往前推：滑动窗口该立刻放行，
        # 固定分钟桶会继续挡到下一个整分钟。
        svc._key_hits["sk-rate"] = [time.time() - 61]
        self.assertEqual(svc.chat_completions(self._request()).status, 200)

    def test_zero_means_unlimited(self):
        svc = self._service(rate_limit=0)
        for _ in range(5):
            self.assertEqual(svc.chat_completions(self._request()).status, 200)
        self.assertNotIn("sk-rate", svc._key_hits)


class WafWaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-waf-")
        self.cfg = Config(None, {"data_dir": self.tmp, "panel": {"enabled": False}})
        self.store = Store(self.tmp)
        for uid in ("w1", "w2", "w3"):
            self.store.upsert_account(Account(uid=uid, id=uid, realm="cn", access_token="tok-" + uid,
                                              refresh_token="rt-" + uid, expires_at=time.time() + 86400))

    def test_two_distinct_accounts_trip_the_wave(self):
        svc = GatewayService(self.cfg, self.store, client=FakeClient([ok_stream("a")]))
        self.assertFalse(svc.waf_wave("cn")["active"])
        svc._note_waf_hit("cn", self.store.get_account("w1"))
        self.assertFalse(svc.waf_wave("cn")["active"], "一个号踩到只是内容问题")
        svc._note_waf_hit("cn", self.store.get_account("w2"))
        wave = svc.waf_wave("cn")
        self.assertTrue(wave["active"])
        self.assertEqual(wave["uids"], 2)
        self.assertGreater(wave["for_seconds"], 0)

    def test_wave_short_circuits_before_touching_any_account(self):
        from wb2api.httpserver import Request

        client = FakeClient([ok_stream("a")])
        svc = GatewayService(self.cfg, self.store, client=client)
        svc._note_waf_hit("cn", self.store.get_account("w1"))
        svc._note_waf_hit("cn", self.store.get_account("w2"))
        request = Request("POST", "/v1/chat/completions", {}, {}, json.dumps({
            "model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}],
        }).encode(), ("1.2.3.4", 1))
        with self.assertRaises(Exception) as ctx:
            svc.chat_completions(request)
        self.assertEqual(getattr(ctx.exception, "code", ""), "content_blocked")
        self.assertEqual(client.calls, [], "波次生效时不该再打上游")

    def test_wave_is_per_realm(self):
        svc = GatewayService(self.cfg, self.store, client=FakeClient([ok_stream("a")]))
        account = self.store.get_account("w1")
        svc._note_waf_hit("cn", account)
        svc._note_waf_hit("cn", self.store.get_account("w2"))
        self.assertTrue(svc.waf_wave("cn")["active"])
        self.assertFalse(svc.waf_wave("global")["active"])


class ModelLockViewTest(unittest.TestCase):
    def test_pool_stats_aggregates_model_locks(self):
        tmp = tempfile.mkdtemp(prefix="wb2a-locks-")
        cfg = Config(None, {"data_dir": tmp, "panel": {"enabled": False}})
        store = Store(tmp)
        for uid in ("m1", "m2"):
            store.upsert_account(Account(uid=uid, id=uid, realm="cn", access_token="tok-" + uid,
                                         refresh_token="rt-" + uid, expires_at=time.time() + 86400))
        svc = GatewayService(cfg, store, client=FakeClient([]))
        svc.pool.block_model(store.get_account("m1"), "glm-4.6", "no service")
        svc.pool.block_model(store.get_account("m2"), "glm-4.6", "no service")
        locks = svc.pool.stats("cn")["model_locks"]
        self.assertIn("glm-4.6", locks)
        self.assertEqual(locks["glm-4.6"]["accounts"], 2)
        self.assertGreater(locks["glm-4.6"]["for_seconds"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ObservabilityTest(unittest.TestCase):
    """出口池、失败报文留存、逐请求日志：三者都是"出问题时能查"的基础设施。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-obs-")
        self.cfg = Config(None, {"data_dir": self.tmp, "panel": {"enabled": False}})

    def service(self, script, cfg=None):
        store = Store(self.tmp)
        for uid in ("u1",):
            store.upsert_account(Account(
                uid=uid, id=uid, realm="cn", access_token="tok-" + uid,
                refresh_token="rt-" + uid, expires_at=time.time() + 86400,
            ))
        client = FakeClient(script)
        return GatewayService(cfg or self.cfg, Store(self.tmp), client=client), client

    def request(self, payload=None):
        from wb2api.httpserver import Request

        body = {"model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}]}
        body.update(payload or {})
        return Request("POST", "/v1/chat/completions", {}, {}, json.dumps(body).encode(), ("1.2.3.4", 1))

    # -- 出口池 -------------------------------------------------------------
    def test_proxy_from_pool_is_used_and_counted(self):
        from wb2api.config import Config as C

        cfg = C(None, {"data_dir": self.tmp, "panel": {"enabled": False},
                       "pool": {"proxies": ["http://10.0.0.9:3128"]}})
        svc, client = self.service([ok_stream("hi")], cfg=cfg)
        svc.chat_completions(self.request())
        self.assertEqual(client.calls[0]["proxy"], "http://10.0.0.9:3128")
        self.assertEqual(svc.proxies.stats()["entries"][0]["used"], 1)

    def test_account_proxy_wins_over_pool(self):
        from wb2api.config import Config as C

        cfg = C(None, {"data_dir": self.tmp, "panel": {"enabled": False},
                       "pool": {"proxies": ["http://10.0.0.9:3128"]}})
        store = Store(self.tmp)
        store.upsert_account(Account(
            uid="u1", id="u1", realm="cn", access_token="tok", refresh_token="rt",
            expires_at=time.time() + 86400, proxy="socks5h://127.0.0.1:1080",
        ))
        client = FakeClient([ok_stream("hi")])
        svc = GatewayService(cfg, Store(self.tmp), client=client)
        svc.chat_completions(self.request())
        self.assertEqual(client.calls[0]["proxy"], "socks5h://127.0.0.1:1080")

    def test_empty_pool_means_direct(self):
        svc, client = self.service([ok_stream("hi")])
        svc.chat_completions(self.request())
        self.assertEqual(client.calls[0]["proxy"], "")

    # -- 失败报文 -----------------------------------------------------------
    def test_rejected_body_is_kept_and_redacted(self):
        from wb2api.upstream import UpstreamError

        err = UpstreamError("tool_choice invalid", kind="bad_request", status=400, code=11101,
                            body='{"code":11101,"message":"tool_choice invalid","key":"sk-abcdefghijklmnop"}')
        svc, _client = self.service([err])
        with self.assertRaises(ServiceError):
            svc.chat_completions(self.request())
        rows = svc.diag.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "11101")
        record = svc.diag.get(rows[0]["request_id"])
        self.assertIn("tool_choice invalid", record["body"])
        self.assertNotIn("abcdefghijklmnop", record["body"])
        self.assertEqual(record["request_headers"]["authorization"], "**redacted**")

    def test_timeout_without_body_is_not_dumped(self):
        from wb2api.upstream import UpstreamError

        svc, _client = self.service([UpstreamError("timeout", kind="timeout", status=0)])
        with self.assertRaises(ServiceError):
            svc.chat_completions(self.request())
        self.assertEqual(svc.diag.recent(), [])

    def test_diagnostics_can_be_disabled(self):
        from wb2api.upstream import UpstreamError

        cfg = Config(None, {"data_dir": self.tmp, "panel": {"enabled": False},
                            "diagnostics": {"enabled": False}})
        svc, _client = self.service([UpstreamError("bad", kind="bad_request", status=400,
                                                   code=11101, body="{bad}")], cfg=cfg)
        with self.assertRaises(ServiceError):
            svc.chat_completions(self.request())
        self.assertEqual(svc.diag.recent(), [])

    # -- 逐请求日志 ---------------------------------------------------------
    def test_request_log_keeps_client_context(self):
        svc, _client = self.service([ok_stream("hi")])
        svc.chat_completions(self.request())
        row = svc.reqlog.recent()[0]
        self.assertEqual(row["path"], "/v1/chat/completions")
        self.assertEqual(row["client_ip"], "1.2.3.4")
        self.assertTrue(row["request_id"].startswith("req_"))
        self.assertEqual(row["outcome"], "completed")

    def test_non_stream_has_no_ttft(self):
        # 非流式根本没有"首字"，记 0 会让面板的分位数和逐请求表格说谎。
        svc, _client = self.service([ok_stream("hi")])
        svc.chat_completions(self.request({"stream": False}))
        row = svc.reqlog.recent()[0]
        self.assertIsNone(row["ttft_ms"])
        self.assertGreaterEqual(row["elapsed_ms"], 0)


class GracefulShutdownTest(unittest.TestCase):
    """关机不该把正在写的响应掐断；等不到就记一条日志走人。"""

    def test_wait_active_reports_leftovers_then_zero(self):
        from wb2api.httpserver import Application, GatewayServer, Router

        server = GatewayServer(("127.0.0.1", 0), Application(Router()))
        try:
            self.assertEqual(server.active, 0)
            server.enter_request()
            self.assertEqual(server.active, 1)
            started = time.time()
            self.assertEqual(server.wait_active(0.15), 1, "还有请求在跑就该如实报数")
            self.assertGreaterEqual(time.time() - started, 0.1, "必须真的等满 grace")
            server.leave_request()
            self.assertEqual(server.wait_active(0.15), 0)
        finally:
            server.server_close()


class ActiveCounterTest(ServerCase):
    def test_request_does_not_leak_the_counter(self):
        status, _headers, _data = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        # 响应写完和"工作线程退出 finally"之间有极短的窗口，等一下再断言，
        # 否则测的是调度运气而不是计数逻辑。
        left = self.server.wait_active(2.0, step=0.02)
        self.assertEqual(left, 0, "请求结束后在途计数必须归零")
