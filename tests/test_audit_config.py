"""配置键审计工具的测试。

这个工具本身很容易"静默失效"：正则写错了，它会对所有键都说"没人读"，
或者反过来永远通过。所以两头都要钉住——既要有死键能被抓到，也要保证
当前仓库是干净的（CI 里跑的就是这条）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_config  # noqa: E402


class LeavesTest(unittest.TestCase):
    def test_flattens_nested_dicts(self):
        got = sorted(audit_config.leaves({"a": {"b": 1, "c": {"d": 2}}, "e": 3}))
        self.assertEqual(got, ["a.b", "a.c.d", "e"])

    def test_leaf_of_empty_dict_still_shows_up(self):
        # `features: {}` 曾经是个空段：它本身没有键，但不该让整棵树消失
        self.assertEqual(list(audit_config.leaves({"features": {}})), ["features"])


class RepoIsCleanTest(unittest.TestCase):
    def test_no_dead_keys_in_this_repo(self):
        dead = audit_config.dead_keys(ROOT)
        self.assertEqual(dead, [], f"这些配置键写了但没人读：{dead}")

    def test_main_returns_zero_when_clean(self):
        self.assertEqual(audit_config.main([]), 0)


class DetectsDeadKeyTest(unittest.TestCase):
    """造一个假仓库：一个键在代码里被读了，另一个没有。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        pkg = os.path.join(self.tmp.name, "wb2api")
        os.makedirs(pkg)
        with open(os.path.join(pkg, "service.py"), "w", encoding="utf-8") as handle:
            handle.write(
                "def f(cfg):\n"
                '    return cfg.get("upstream.timeout_seconds")\n'
                "\n"
                "def g(cfg):\n"
                '    return cfg.get("upstream", {}).get("stream_heartbeat_seconds")\n'
                "\n"
                "def h(pool):\n"
                '    return pool._pool_cfg("pool", "max_in_flight")\n'
                "\n"
                "def k(cfg, realm):\n"
                '    return conf.realm_value(cfg, "pool.strategy", realm)\n'
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_sees_dotted_segmented_and_section_reads(self):
        used = audit_config.used_keys(self.tmp.name)
        self.assertIn("upstream.timeout_seconds", used)
        self.assertIn("pool.max_in_flight", used, "分段读取要抓全两个参数，不能只剩段名")
        self.assertIn("stream_heartbeat_seconds", used, "整段读取只能看到叶子名")

    def test_segment_name_alone_does_not_hide_its_children(self):
        """`_pool_cfg("pool", "x")` 只说明 "pool.x" 被读了，不能让它包住整个 pool 段。"""
        self.assertNotIn("pool", audit_config.used_keys(self.tmp.name))

    def test_section_level_get_hides_its_children(self):
        """已知取舍：`cfg.get("upstream", {})` 只出现段名，于是整段都算"用过"。

        工具是抓"拼错了/忘了接"，不是证明每个键都被真的读到，所以宁可漏报也
        不误报——真有这种写法时，靠 review 和这行注释提醒。
        """
        original = audit_config.DEFAULTS
        audit_config.DEFAULTS = {"upstream": {"nobody_reads_this": 7}}
        try:
            dead = audit_config.dead_keys(self.tmp.name)
        finally:
            audit_config.DEFAULTS = original
        self.assertEqual(dead, [])

    def test_reports_the_key_nobody_reads(self):
        original = audit_config.DEFAULTS
        audit_config.DEFAULTS = {
            "upstream": {"timeout_seconds": 120},
            "pool": {"max_in_flight": 3},
            "telemetry": {"no_one_calls_this": 1},
        }
        try:
            dead = audit_config.dead_keys(self.tmp.name)
        finally:
            audit_config.DEFAULTS = original
        self.assertEqual(dead, ["telemetry.no_one_calls_this"])

    def test_prefix_relation_counts_as_used(self):
        # realm_value(cfg, "pool.strategy", realm) 读的是 pool.strategy.cn
        original = audit_config.DEFAULTS
        audit_config.DEFAULTS = {"pool": {"strategy": {"cn": "weighted_random"}}}
        try:
            dead = audit_config.dead_keys(self.tmp.name)
        finally:
            audit_config.DEFAULTS = original
        self.assertEqual(dead, [], "段名被读到时，它的子键不该报死")


if __name__ == "__main__":
    unittest.main()
