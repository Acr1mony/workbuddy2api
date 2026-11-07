"""config 的加载、覆盖与对外暴露。

配置失效方式很"安静"：类型没转对（"false" 变成真值）、密钥被原样吐给面板、
非法 listen 等到启动才炸。测试盯住这三类，而不是逐个断言默认值。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from wb2api import util
from wb2api.config import DEFAULTS, ENV_OVERRIDES, Config, realm_value


class LoadTests(unittest.TestCase):
    def test_missing_file_yields_defaults(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wb2a-cfg-") as tmp:
            path = os.path.join(tmp, "config.json")
            cfg = Config.load(path)
            self.assertFalse(os.path.exists(path), "读配置不该顺手创建文件")
        self.assertEqual(cfg.path, os.path.abspath(path))
        self.assertEqual(cfg.get("listen"), DEFAULTS["listen"])
        self.assertEqual(cfg.get("panel.session_hours"), 12)

    def test_defaults_are_deep_copied(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wb2a-cfg-") as tmp:
            cfg = Config.load(os.path.join(tmp, "missing.json"))
        cfg.set("panel.password", "changed")
        cfg.set("pool.strategy.cn", "round_robin")
        cfg.get("pool.model_rates")["glm-4"] = 0.5
        self.assertEqual(DEFAULTS["panel"]["password"], "admin", "DEFAULTS 被实例写脏了")
        self.assertEqual(DEFAULTS["pool"]["strategy"]["cn"], "weighted_random")
        self.assertEqual(DEFAULTS["pool"]["model_rates"], {})
        self.assertEqual(Config(None).get("panel.password"), "admin", "新实例也看到了上一实例的修改")

    def test_file_overrides_merge_with_defaults(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wb2a-cfg-") as tmp:
            path = os.path.join(tmp, "config.json")
            util.atomic_write_json(
                path,
                {
                    "listen": "0.0.0.0:9000",
                    "panel": {"password": "s3cret"},
                    "future": {"flag": True},
                },
            )
            cfg = Config.load(path)
        self.assertEqual(cfg.get("listen"), "0.0.0.0:9000")
        self.assertEqual(cfg.get("panel.password"), "s3cret")
        self.assertEqual(cfg.get("panel.session_hours"), 12, "同级默认值不能被整块覆盖")
        self.assertTrue(cfg.get("future.flag"), "未知键要保留，降级才不丢配置")

    def test_env_overrides_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wb2a-cfg-") as tmp:
            path = os.path.join(tmp, "config.json")
            util.atomic_write_json(path, {"listen": "127.0.0.1:1111"})
            with mock.patch.dict(os.environ, {"WB_LISTEN": "127.0.0.1:2222"}, clear=True):
                cfg = Config.load(path)
        self.assertEqual(cfg.get("listen"), "127.0.0.1:2222", "环境变量优先级最高")


class DottedAccessTests(unittest.TestCase):
    def test_get_and_set_dotted_paths(self) -> None:
        cfg = Config(None)
        cfg.set("a.b.c", 1)
        self.assertEqual(cfg.get("a.b.c"), 1)
        self.assertEqual(cfg.get("a.b"), {"c": 1})
        self.assertEqual(cfg.get("a.b.missing", "fallback"), "fallback")
        self.assertIsNone(cfg.get("nope.deep"))
        cfg.set("a.b.c", 2)
        self.assertEqual(cfg.get("a.b.c"), 2)

    def test_set_creates_missing_branches(self) -> None:
        cfg = Config(None)
        cfg.set("brand.new.key", "v")
        self.assertEqual(cfg.values["brand"], {"new": {"key": "v"}})

    def test_as_dict_is_a_copy(self) -> None:
        cfg = Config(None)
        snapshot = cfg.as_dict()
        snapshot["listen"] = "x"
        self.assertNotEqual(cfg.get("listen"), "x")


class ApplyEnvTests(unittest.TestCase):
    def test_env_values_are_coerced_to_default_types(self) -> None:
        cfg = Config(None)
        cfg.apply_env(
            {
                "WB_LISTEN": "0.0.0.0:9000",
                "WB_MAX_IN_FLIGHT": "7",
                "WB_PANEL_ENABLED": "off",
                "WB_UPSTREAM_TIMEOUT": "not-a-number",
            }
        )
        self.assertEqual(cfg.get("listen"), "0.0.0.0:9000")
        self.assertEqual(cfg.get("pool.max_in_flight"), 7, "int 键不能留成字符串")
        self.assertIs(cfg.get("panel.enabled"), False, "WB_PANEL_ENABLED=off 必须变布尔 false")
        self.assertEqual(cfg.get("upstream.timeout_seconds"), 120, "坏数值回落默认而不是崩")
        self.assertEqual(cfg._env_applied["pool.max_in_flight"], "WB_MAX_IN_FLIGHT")

    def test_every_override_points_at_a_real_key(self) -> None:
        # ENV_OVERRIDES 里写错一个域名，环境变量就永远不生效——这种错没人会发现。
        cfg = Config(None)
        for env_name, dotted in ENV_OVERRIDES.items():
            self.assertIsNotNone(cfg.get(dotted), "%s -> %s 指向不存在的键" % (env_name, dotted))

    def test_empty_env_is_ignored(self) -> None:
        cfg = Config(None)
        cfg.apply_env({"WB_API_KEY": "", "WB_PANEL_PASSWORD": ""})
        self.assertEqual(cfg.get("api_key"), "")
        self.assertNotIn("api_key", cfg._env_applied)
        self.assertEqual(cfg.get("panel.password"), "admin")

    def test_unknown_bool_text_keeps_default(self) -> None:
        cfg = Config(None)
        cfg.apply_env({"WB_PANEL_ENABLED": "sometimes"})
        self.assertIs(cfg.get("panel.enabled"), True, "认不出的布尔值不要瞎猜")

    def test_bool_spellings(self) -> None:
        for text, expected in (("1", True), ("yes", True), ("ON", True), ("0", False), ("No", False)):
            cfg = Config(None)
            cfg.apply_env({"WB_PANEL_ENABLED": text})
            self.assertIs(cfg.get("panel.enabled"), expected, text)


class RealmValueTests(unittest.TestCase):
    def test_new_key_wins_over_legacy(self) -> None:
        cfg = Config(None, {"pool": {"strategy": {"cn": "rr", "domestic": "old"}}})
        self.assertEqual(realm_value(cfg, "pool.strategy", "cn", "fallback"), "rr")

    def test_legacy_keys_still_work(self) -> None:
        # 旧版本写的是 domestic / international；升级后不能要求用户改配置文件。
        cfg = Config(
            None,
            {
                "pool": {"strategy": {"cn": "", "domestic": "rr"}},
                "upstream": {"hosts": {"international": "x.test"}},
            },
        )
        self.assertEqual(realm_value(cfg, "pool.strategy", "cn", "fallback"), "rr")
        self.assertEqual(realm_value(cfg, "upstream.hosts", "global", ""), "x.test")

    def test_missing_and_empty_fall_back_to_default(self) -> None:
        cfg = Config(None, {"pool": {"strategy": {"cn": ""}}})
        self.assertEqual(realm_value(cfg, "pool.strategy", "cn", "fallback"), "fallback")
        self.assertEqual(realm_value(cfg, "nope", "cn", "fallback"), "fallback")
        self.assertIsNone(realm_value(None, "pool.strategy", "cn"))

    def test_plain_dict_with_dotted_keys(self) -> None:
        self.assertEqual(realm_value({"pool.strategy.cn": "rr"}, "pool.strategy", "cn", ""), "rr")


class PublicDictTests(unittest.TestCase):
    def test_secrets_are_not_exposed(self) -> None:
        cfg = Config(None)
        cfg.set("api_key", "sk-live-0123456789")
        cfg.set("panel.password", "hunter2")
        cfg.set("upstream.device_token", "device-secret")

        public = cfg.public_dict()
        blob = json.dumps(public, ensure_ascii=False)
        for secret in ("sk-live-0123456789", "hunter2", "device-secret"):
            self.assertNotIn(secret, blob, "面板视图泄露了 %s" % secret)
        self.assertEqual(public["api_key"], "")
        self.assertEqual(public["panel"]["password"], "")
        self.assertEqual(public["upstream"]["device_token"], "")
        self.assertTrue(public["api_key_set"])
        self.assertTrue(public["panel"]["password_set"])
        self.assertTrue(public["upstream"]["device_token_set"])

        # 面板视图是副本，不能把运行中的配置改掉。
        self.assertEqual(cfg.get("api_key"), "sk-live-0123456789")
        self.assertEqual(cfg.get("panel.password"), "hunter2")

    def test_unset_secrets_are_reported_as_unset(self) -> None:
        cfg = Config(None)
        cfg.set("api_key", "")
        cfg.set("panel.password", "")
        public = cfg.public_dict()
        self.assertFalse(public["api_key_set"])
        self.assertFalse(public["panel"]["password_set"])
        self.assertNotIn("device_token_set", public["upstream"])


class ValidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wb2a-cfg-")
        self.cfg = Config(None)
        self.cfg.set("data_dir", self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def problems(self) -> list:
        return self.cfg.validate()

    def test_shipped_defaults_are_valid(self) -> None:
        self.assertEqual(self.problems(), [], "默认配置必须开箱即用")

    def test_bad_listen_is_reported(self) -> None:
        self.cfg.set("listen", "not-a-host")
        self.assertTrue(any("listen is not host:port" in p for p in self.problems()))
        self.cfg.set("listen", "0.0.0.0:99999")
        self.assertTrue(any("listen port out of range: 99999" in p for p in self.problems()))

    def test_bad_duration_is_reported(self) -> None:
        self.cfg.set("cooldown.soft_rate", "soon")
        self.assertTrue(any("cooldown.soft_rate is not a duration" in p for p in self.problems()))
        self.cfg.set("cooldown.soft_rate", "5x")
        self.assertTrue(
            any("cooldown.soft_rate is not a duration" in p for p in self.problems()),
            "未知单位同样算非法，不能被 amount*default 蒙混过关",
        )
        self.cfg.set("cooldown.soft_rate", "5m")
        self.assertEqual(self.problems(), [])

    def test_zero_in_flight_and_negative_credit_floor(self) -> None:
        self.cfg.set("pool.max_in_flight", 0)
        self.cfg.set("pool.credit_floor", -1)
        problems = self.problems()
        self.assertTrue(any("pool.max_in_flight" in p for p in problems))
        self.assertTrue(any("pool.credit_floor" in p for p in problems))

    def test_missing_data_dir_is_reported_as_warning(self) -> None:
        missing = os.path.join(self.tmp.name, "not-yet")
        self.cfg.set("data_dir", missing)
        self.assertTrue(any("data_dir does not exist yet" in p for p in self.problems()))
        self.assertFalse(os.path.exists(missing), "validate 只报告，不创建目录")


class ListenTests(unittest.TestCase):
    def test_host_and_port(self) -> None:
        cfg = Config(None)
        cfg.set("listen", "0.0.0.0:8788")
        self.assertEqual(cfg.listen_address(), ("0.0.0.0", 8788))

    def test_bare_port_means_loopback(self) -> None:
        cfg = Config(None)
        cfg.set("listen", "9999")
        self.assertEqual(cfg.listen_address(), ("127.0.0.1", 9999))

    def test_leading_colon_means_all_interfaces(self) -> None:
        cfg = Config(None)
        cfg.set("listen", ":8080")
        self.assertEqual(cfg.listen_address(), ("0.0.0.0", 8080))


if __name__ == "__main__":
    unittest.main()
