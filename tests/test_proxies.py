"""出口池：选谁、什么时候踢出去、踢多久回来。"""

from __future__ import annotations

import unittest

from wb2api.config import Config
from wb2api.proxies import ProxyEntry, ProxyPool


def pool_with(entries, **extra):
    data = {"pool": {"proxies": entries}}
    data["pool"].update(extra)
    return ProxyPool(Config(None, data))


class ParseTest(unittest.TestCase):
    def test_string_and_dict_forms(self):
        pool = pool_with(["socks5h://1.1.1.1:1080", {"url": "http://2.2.2.2:3128", "realm": "global", "weight": 2}])
        self.assertTrue(pool.enabled())
        self.assertEqual(pool.size(), 2)
        self.assertEqual(pool.size("cn"), 1)  # global-only 的出口不服务 cn
        self.assertEqual(pool.size("global"), 2)  # global 域两个都能用

    def test_duplicate_and_empty_are_dropped(self):
        pool = pool_with(["http://a:1", "http://a:1", "", {"note": "没 url"}])
        self.assertEqual(pool.size(), 1)

    def test_empty_pool_means_direct(self):
        pool = pool_with([])
        self.assertFalse(pool.enabled())
        self.assertEqual(pool.pick("cn"), "")


class PickTest(unittest.TestCase):
    def test_only_realm_matching_entries_are_used(self):
        pool = pool_with([
            {"url": "http://cn:1", "realm": "cn"},
            {"url": "http://gl:2", "realm": "global"},
        ])
        for _ in range(20):
            self.assertEqual(pool.pick("cn"), "http://cn:1")
            self.assertEqual(pool.pick("global"), "http://gl:2")

    def test_cooling_entry_is_skipped(self):
        pool = pool_with([{"url": "http://bad:1"}, {"url": "http://good:2"}])
        pool.note_failure("http://bad:1")
        for _ in range(20):
            self.assertEqual(pool.pick("cn"), "http://good:2")

    def test_when_all_cooling_it_still_returns_a_proxy(self):
        """全在冷却也不能退回直连：直连等于把真实 IP 交出去。"""
        pool = pool_with([{"url": "http://a:1"}])
        pool.note_failure("http://a:1")
        self.assertEqual(pool.pick("cn"), "http://a:1")


class HealthTest(unittest.TestCase):
    def test_failure_backoff_doubles_and_caps(self):
        pool = pool_with([{"url": "http://a:1"}], proxy_cooldown="10s", proxy_cooldown_max="40s")
        self.assertEqual(pool.note_failure("http://a:1"), 10.0)
        self.assertEqual(pool.note_failure("http://a:1"), 20.0)
        self.assertEqual(pool.note_failure("http://a:1"), 40.0)
        self.assertEqual(pool.note_failure("http://a:1"), 40.0)

    def test_success_clears_cooldown(self):
        pool = pool_with([{"url": "http://a:1"}])
        pool.note_failure("http://a:1")
        self.assertGreater(pool.stats()["cooling"], 0)
        pool.note_success("http://a:1")
        self.assertEqual(pool.stats()["cooling"], 0)

    def test_stats_mask_credentials(self):
        pool = pool_with(["socks5h://user:hunter2@10.0.0.1:1080"])
        row = pool.stats()["entries"][0]
        self.assertNotIn("hunter2", row["url"])
        self.assertEqual(row["realm"], "both")


class MutationTest(unittest.TestCase):
    def test_add_remove_update_and_config_roundtrip(self):
        pool = pool_with([])
        self.assertTrue(pool.add("http://a:1", realm="cn", weight=2, note="家宽"))
        self.assertFalse(pool.add("http://a:1"))  # 重复
        self.assertTrue(pool.update("http://a:1", enabled=False))
        self.assertFalse(pool.pick("cn"))  # 停用后不参与
        self.assertTrue(pool.update("http://a:1", enabled=True, note="改过"))
        config = pool.to_config()
        self.assertEqual(config[0]["url"], "http://a:1")
        self.assertEqual(config[0]["weight"], 2.0)

        again = pool_with(config)
        self.assertEqual(again.size(), 1)
        self.assertTrue(again.remove("http://a:1"))
        self.assertFalse(again.remove("http://a:1"))

    def test_reload_keeps_runtime_health(self):
        pool = pool_with([{"url": "http://a:1"}])
        pool.note_failure("http://a:1")
        before = pool.stats()["entries"][0]["failures"]
        pool.reload()  # 相当于面板点了一次"保存"
        self.assertEqual(pool.stats()["entries"][0]["failures"], before)

    def test_reset_health(self):
        pool = pool_with([{"url": "http://a:1"}])
        pool.note_failure("http://a:1")
        pool.reset_health()
        self.assertEqual(pool.stats()["cooling"], 0)


class EntryTest(unittest.TestCase):
    def test_weight_floor(self):
        entry = ProxyEntry("http://a:1", weight=0)
        self.assertEqual(entry.weight, 0.1)

    def test_realm_aliases(self):
        self.assertEqual(ProxyEntry("http://a:1", realm="international").realm, "global")
        self.assertEqual(ProxyEntry("http://a:1", realm="cn").realm, "cn")
        self.assertEqual(ProxyEntry("http://a:1", realm="").realm, "")


if __name__ == "__main__":
    unittest.main()
