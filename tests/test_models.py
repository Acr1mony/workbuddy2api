"""模型目录的解析与过滤。

这里最容易出的错不是崩溃，而是"悄悄少几个模型"或"把免费模型标成收费"，
所以测试盯的是**判定结果**，不是实现细节。
"""

import json
import os
import shutil
import tempfile
import time
import unittest

from wb2api import models


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


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def close(self):
        pass


class FakeClient:
    def __init__(self, responses):
        self.responses = responses  # url 后缀 -> payload 或异常
        self.calls = []

    def base_url(self, realm):
        return "https://%s.test" % (realm or "cn")

    def get(self, url, headers):
        self.calls.append(url)
        for suffix, payload in self.responses.items():
            if url.endswith(suffix):
                return FakeResponse(payload)
        return FakeResponse(KeyError("no route for %s" % url))


class FakeAccount:
    def __init__(self, realm="cn"):
        self.realm = realm
        self.uid = "u1"
        self.access_token = "tok"
        self.enterprise_id = ""
        self.domain = ""
        self.device_token = ""
        self.id = "u1"


class TestParseRate(unittest.TestCase):
    def test_known_shapes(self):
        self.assertAlmostEqual(models.parse_rate("x0.11"), 0.11)
        self.assertAlmostEqual(models.parse_rate("x0.34 credits"), 0.34)
        self.assertEqual(models.parse_rate("x0.00"), 0.0)
        self.assertEqual(models.parse_rate(0.5), 0.5)

    def test_unknown_is_not_free(self):
        # 未知必须返回 None：当成免费会让 credit_floor 彻底失效。
        for bad in ("", "   ", "free", None, {}, True, []):
            self.assertIsNone(models.parse_rate(bad), bad)

    def test_free_detected(self):
        info = models.ModelInfo("m", rate=models.parse_rate("x0.00"))
        self.assertTrue(info.free)
        self.assertFalse(models.ModelInfo("m", rate=0.2).free)
        self.assertFalse(models.ModelInfo("m", rate=None).free)


class TestChatCapable(unittest.TestCase):
    def test_prefixes_are_dropped(self):
        for model_id in ("nes-whatever", "completion-x", "codewise-y"):
            self.assertFalse(models.ModelInfo(model_id).chat_capable, model_id)

    def test_tiny_output_is_not_chat(self):
        self.assertFalse(models.ModelInfo("m", max_output=256).chat_capable)
        self.assertTrue(models.ModelInfo("m", max_output=1000).chat_capable)
        # 上游没给 max_output 时不能据此否定
        self.assertTrue(models.ModelInfo("m", max_output=0).chat_capable)

    def test_media_tags_are_dropped(self):
        self.assertFalse(models.ModelInfo("m", tags=["text-to-image"]).chat_capable)
        self.assertFalse(models.ModelInfo("m", tags=["embedding"]).chat_capable)
        self.assertTrue(models.ModelInfo("m", tags=["chat"]).chat_capable)


class TestParseEntries(unittest.TestCase):
    def test_data_models_envelope(self):
        payload = {"code": 0, "data": {"models": [{"id": "a"}, {"id": "b"}]}}
        self.assertEqual([i.id for i in models.parse_entries(payload)], ["a", "b"])

    def test_agents_cli_branch(self):
        payload = {
            "data": {
                "agents": [
                    {"name": "cli", "models": [{"id": "c1", "maxOutputTokens": 8192}]},
                    {"name": "ide", "models": [{"id": "ignore-me"}]},
                ]
            }
        }
        found = models.parse_entries(payload)
        self.assertEqual([i.id for i in found], ["c1"])
        self.assertEqual(found[0].max_output, 8192)

    def test_bare_list_and_aliases(self):
        payload = [{"model": "m1"}, {"name": "m2"}, {"id": ""}, "junk"]
        self.assertEqual({i.id for i in models.parse_entries(payload)}, {"m1", "m2"})

    def test_dedupe_keeps_first(self):
        payload = [{"id": "dup", "credits": "x0.10"}, {"id": "dup", "credits": "x9.99"}]
        found = models.parse_entries(payload)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0].rate, 0.10)

    def test_reasoning_shapes(self):
        new = models.parse_entries([{"id": "a", "reasoning": {"supportedEfforts": ["low", "high"]}}])[0]
        self.assertEqual(new.meta()["supportedEfforts"], ["low", "high"])
        self.assertTrue(new.meta()["reasoning"])
        legacy = models.parse_entries([{"id": "b", "reasoning_efforts": ["low"]}])[0]
        self.assertEqual(legacy.meta()["supportedEfforts"], ["low"])
        plain = models.parse_entries([{"id": "c"}])[0]
        self.assertFalse(plain.meta()["reasoning"])


class TestCatalog(unittest.TestCase):
    def _catalog(self, responses, cfg=None):
        client = FakeClient(responses)
        return models.ModelCatalog(client, cfg or FakeCfg()), client

    def test_falls_back_to_second_source(self):
        catalog, client = self._catalog(
            {
                models.CONFIG_PATH: RuntimeError("403"),
                models.CONSOLE_PATH: {"data": {"models": [{"id": "ok-1"}]}},
            }
        )
        found = catalog.fetch(FakeAccount(), "cn")
        self.assertEqual([i.id for i in found], ["ok-1"])
        self.assertEqual(len(client.calls), 2)

    def test_cache_avoids_second_fetch(self):
        catalog, client = self._catalog({models.CONFIG_PATH: {"data": {"models": [{"id": "a"}]}}})
        account = FakeAccount()
        catalog.fetch(account, "cn")
        catalog.fetch(account, "cn")
        self.assertEqual(len(client.calls), 1, "第二次应命中缓存")

    def test_negative_cache_after_total_failure(self):
        catalog, client = self._catalog({})
        account = FakeAccount()
        self.assertEqual(catalog.fetch(account, "cn"), [])
        first = len(client.calls)
        self.assertEqual(first, 3, "三个来源都要试过")
        catalog.fetch(account, "cn")
        self.assertEqual(len(client.calls), first, "负缓存期内不该再打上游")

    def test_hidden_patterns(self):
        cfg = FakeCfg({"models": {"hidden": ["secret-*", "exact-model"]}})
        catalog, _ = self._catalog(
            {models.CONFIG_PATH: {"data": {"models": [
                {"id": "secret-1"}, {"id": "exact-model"}, {"id": "public"},
            ]}}},
            cfg,
        )
        catalog.fetch(FakeAccount(), "cn")
        self.assertEqual(catalog.names("cn"), ["public"])
        self.assertEqual([i["id"] for i in catalog.openai_list("cn")["data"]], ["public"])

    def test_rate_falls_back_to_config(self):
        cfg = FakeCfg({"pool": {"model_rates": {"m": 0.25}}})
        catalog, _ = self._catalog({models.CONFIG_PATH: {"data": {"models": [{"id": "m"}]}}}, cfg)
        catalog.fetch(FakeAccount(), "cn")
        self.assertAlmostEqual(catalog.rate("m", "cn"), 0.25)

    def test_openai_shape(self):
        catalog, _ = self._catalog(
            {models.CONFIG_PATH: {"data": {"models": [{"id": "m", "maxOutputTokens": 4096}]}}}
        )
        catalog.fetch(FakeAccount(), "cn")
        item = catalog.openai_list("cn")["data"][0]
        self.assertEqual(item["id"], "m")
        self.assertEqual(item["object"], "model")
        self.assertEqual(item["max_output_tokens"], 4096)
        self.assertNotIn("context_length", item, "上游没给就别瞎填")


class TestDiskCache(unittest.TestCase):
    """重启不该等于"再打一次上游"；但缓存过期了就必须忘掉它。"""

    PAYLOAD = {
        models.CONFIG_PATH: {"data": {"models": [
            {"id": "m1", "maxOutputTokens": 4096, "credits": "x0.11"},
            {"id": "m2"},
        ]}}
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-catalog-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self):
        return FakeCfg({"data_dir": self.tmp})

    def _catalog(self, responses, cfg=None):
        client = FakeClient(responses)
        return models.ModelCatalog(client, cfg or self._cfg()), client

    def _path(self, realm="cn"):
        return os.path.join(self.tmp, "catalog-%s.json" % realm)

    def _write_stale(self, age=7200.0):
        payload = {
            "realm": "cn",
            "fetched_at": time.time() - age,
            "models": [{"id": "old-model"}],
        }
        with open(self._path(), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def test_fetch_writes_file(self):
        catalog, _ = self._catalog(self.PAYLOAD, self._cfg())
        catalog.fetch(FakeAccount(), "cn")
        self.assertTrue(os.path.exists(self._path()), "抓到了就该落盘")
        with open(self._path(), encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual([item["id"] for item in payload["models"]], ["m1", "m2"])
        self.assertNotIn("raw", payload["models"][0], "上游原文不进缓存")

    def test_restart_reuses_file_without_upstream(self):
        catalog, client = self._catalog(self.PAYLOAD, self._cfg())
        catalog.fetch(FakeAccount(), "cn")
        self.assertEqual(len(client.calls), 1)

        client2 = FakeClient({})
        revived = models.ModelCatalog(client2, self._cfg())
        self.assertEqual([i.id for i in revived.entries("cn", chat_only=False)], ["m1", "m2"])
        self.assertEqual(client2.calls, [], "新鲜缓存不该再打上游")
        self.assertAlmostEqual(revived.rate("m1", "cn"), 0.11)
        self.assertEqual(revived.get("m1", "cn").max_output, 4096)

    def test_stale_file_is_ignored(self):
        self._write_stale()
        client = FakeClient(self.PAYLOAD)
        catalog = models.ModelCatalog(client, self._cfg())
        self.assertEqual(catalog.entries("cn", chat_only=False), [], "过期的清单不能用")
        catalog.fetch(FakeAccount(), "cn")
        self.assertEqual(len(client.calls), 1)

    def test_corrupt_file_is_tolerated(self):
        with open(self._path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        client = FakeClient(self.PAYLOAD)
        catalog = models.ModelCatalog(client, self._cfg())
        catalog.fetch(FakeAccount(), "cn")
        self.assertEqual(len(catalog.entries("cn", chat_only=False)), 2)

    def test_invalidate_drops_file(self):
        catalog, _ = self._catalog(self.PAYLOAD, self._cfg())
        catalog.fetch(FakeAccount(), "cn")
        catalog.invalidate("cn")
        self.assertFalse(os.path.exists(self._path()), "内存清了磁盘也得清")
        self.assertEqual(catalog.entries("cn", chat_only=False), [])

    def test_no_data_dir_means_no_file(self):
        catalog, _ = self._catalog(self.PAYLOAD)  # FakeCfg 没给 data_dir
        catalog.fetch(FakeAccount(), "cn")
        self.assertFalse(os.path.exists("catalog-cn.json"), "没配数据目录就不该往 cwd 写文件")


class TestRefreshThrottle(unittest.TestCase):
    """force 刷新不是按钮：连点不该变成一串 /v3/config。"""

    PAYLOAD = {"data": {"models": [{"id": "m1"}]}}

    def test_second_force_is_dropped(self):
        client = FakeClient({models.CONFIG_PATH: self.PAYLOAD})
        catalog = models.ModelCatalog(client, FakeCfg())
        catalog.fetch(FakeAccount(), "cn", force=True)
        catalog.fetch(FakeAccount(), "cn", force=True)
        self.assertEqual(len(client.calls), 1, "30 秒内的第二次 force 直接用缓存")
        self.assertEqual([info.id for info in catalog.entries("cn", chat_only=False)], ["m1"])

    def test_window_can_be_turned_off(self):
        client = FakeClient({models.CONFIG_PATH: self.PAYLOAD})
        catalog = models.ModelCatalog(client, FakeCfg({"models": {"min_refresh_seconds": 0}}))
        catalog.fetch(FakeAccount(), "cn", force=True)
        catalog.fetch(FakeAccount(), "cn", force=True)
        self.assertEqual(len(client.calls), 2, "0 = 关闭限流，真需要时能强制刷新")

    def test_after_window_it_fetches_again(self):
        client = FakeClient({models.CONFIG_PATH: self.PAYLOAD})
        catalog = models.ModelCatalog(client, FakeCfg({"models": {"min_refresh_seconds": 30}}))
        catalog.fetch(FakeAccount(), "cn", force=True)
        catalog._attempted_at["cn"] = time.time() - 31
        catalog.fetch(FakeAccount(), "cn", force=True)
        self.assertEqual(len(client.calls), 2)


if __name__ == "__main__":
    unittest.main()
