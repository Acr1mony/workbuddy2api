"""出口池：一个号钉一个出口，什么时候换、换给谁。"""

from __future__ import annotations

import unittest

from wb2api.config import Config
from wb2api.proxies import STICKY_BREAK_FAILURES, ProxyEntry, ProxyPool, describe_binding


def pool_with(entries, **extra):
    data = {"pool": {"proxies": entries}}
    data["pool"].update(extra)
    return ProxyPool(Config(None, data))


class Fake:
    """只带出口分配关心的三个属性的账号。"""

    def __init__(self, uid, proxy="", egress=""):
        self.uid = uid
        self.proxy = proxy
        self.extra = {"egress": egress} if egress else {}


class ParseTest(unittest.TestCase):
    def test_string_and_dict_forms(self):
        pool = pool_with(["socks5h://1.1.1.1:1080", {"url": "http://2.2.2.2:3128", "weight": 2}])
        self.assertTrue(pool.enabled())
        self.assertEqual(pool.size(), 2)

    def test_legacy_realm_field_is_ignored(self):
        """老配置里写着 realm，照旧能读——但出口不再按域分组。"""
        pool = pool_with([{"url": "http://cn:1", "realm": "cn"}, {"url": "http://gl:2", "realm": "global"}])
        self.assertEqual(pool.size(), 2)
        self.assertEqual(pool.stats()["total"], 2)

    def test_duplicate_and_empty_are_dropped(self):
        pool = pool_with(["http://a:1", "http://a:1", "", {"note": "没 url"}])
        self.assertEqual(pool.size(), 1)

    def test_empty_pool_means_direct(self):
        pool = pool_with([])
        self.assertFalse(pool.enabled())
        self.assertEqual(pool.assign(Fake("u1")), "")


class BindTest(unittest.TestCase):
    def test_each_account_gets_its_own_exit(self):
        pool = pool_with(["http://a:1", "http://b:2"])
        first = pool.assign(Fake("u1"))
        second = pool.assign(Fake("u2"))
        self.assertNotEqual(first, second, "出口够用时不能让两个号共用同一个")
        self.assertEqual(pool.stats()["shared"], 0)

    def test_binding_is_sticky(self):
        pool = pool_with(["http://a:1", "http://b:2", "http://c:3"])
        account = Fake("u1")
        first = pool.assign(account)
        for _ in range(10):
            self.assertEqual(pool.assign(account), first)

    def test_more_accounts_than_exits_share_the_fewest_used(self):
        pool = pool_with(["http://a:1", "http://b:2"])
        pool.assign(Fake("u1"))
        pool.assign(Fake("u2"))
        third = pool.assign(Fake("u3"))
        self.assertIn(third, ("http://a:1", "http://b:2"))
        self.assertEqual(pool.stats()["shared"], 1)
        self.assertEqual(pool.stats()["bound"], 3)

    def test_manual_account_proxy_wins_and_is_not_recorded(self):
        pool = pool_with(["http://a:1"])
        account = Fake("u1", proxy="http://mine:9")
        for _ in range(3):
            self.assertEqual(pool.assign(account), "http://mine:9")
        self.assertEqual(pool.stats()["bound"], 0, "手配的出口不属于池子")

    def test_cooling_exit_keeps_the_binding(self):
        """冷却只是"暂时别用这个 IP 猛打"，不该顺手把号的脸换掉。"""
        pool = pool_with(["http://a:1"])
        account = Fake("u1")
        self.assertEqual(pool.assign(account), "http://a:1")
        pool.note_failure("http://a:1")
        self.assertEqual(pool.assign(account), "http://a:1")

    def test_broken_exit_is_replaced_after_enough_failures(self):
        pool = pool_with(["http://a:1", "http://b:2"])
        account = Fake("u1")
        first = pool.assign(account)
        for _ in range(STICKY_BREAK_FAILURES):
            pool.note_failure(first)
        second = pool.assign(account)
        self.assertNotEqual(second, first)
        self.assertEqual(pool.binding_of("u1"), second)

    def test_disabled_exit_is_replaced(self):
        pool = pool_with(["http://a:1", "http://b:2"])
        account = Fake("u1")
        first = pool.assign(account)
        pool.update(first, enabled=False)
        self.assertNotEqual(pool.assign(account), first)

    def test_observe_restores_and_release_frees(self):
        pool = pool_with(["http://a:1"])
        pool.observe(Fake("u1", egress="http://a:1"))
        self.assertEqual(pool.binding_of("u1"), "http://a:1")
        self.assertEqual(pool.stats()["bound"], 1)

        other = Fake("u2")
        self.assertEqual(pool.assign(other), "http://a:1")  # 只有一个出口，只能共用
        self.assertTrue(pool.release("u1"))
        self.assertFalse(pool.release("u1"))

    def test_rebind_all_clears_everything(self):
        pool = pool_with(["http://a:1", "http://b:2"])
        pool.assign(Fake("u1"))
        pool.assign(Fake("u2"))
        pool.rebind_all()
        self.assertEqual(pool.stats()["bound"], 0)

    def test_removing_an_exit_drops_its_bindings(self):
        pool = pool_with(["http://a:1", "http://b:2"])
        account = Fake("u1")
        first = pool.assign(account)
        pool.remove(first)
        self.assertEqual(pool.binding_of("u1"), "")
        self.assertEqual(pool.stats()["bound"], 0)

    def test_describe_binding_masks_and_counts(self):
        pool = pool_with(["socks5h://user:hunter2@10.0.0.1:1080"])
        pool.observe(Fake("u1", egress="socks5h://user:hunter2@10.0.0.1:1080"))
        text, shared = describe_binding(pool, "u1")
        self.assertNotIn("hunter2", text)
        self.assertEqual(shared, 1)


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

    def test_stats_mask_credentials_and_list_holders(self):
        pool = pool_with(["socks5h://user:hunter2@10.0.0.1:1080"])
        pool.assign(Fake("u1"))
        row = pool.stats()["entries"][0]
        self.assertNotIn("hunter2", row["url"])
        self.assertEqual(row["accounts"], ["u1"])
        self.assertEqual(row["account_count"], 1)

    def test_clamp_pulls_back_overlong_cooldowns(self):
        pool = pool_with([{"url": "http://a:1"}], proxy_cooldown="10s", proxy_cooldown_max="20s")
        pool.note_failure("http://a:1")
        pool._entries[0].cooldown_until = 9e9
        pool.clamp()
        self.assertLess(pool.stats()["entries"][0]["cooling_for"], 100)


class MutationTest(unittest.TestCase):
    def test_add_remove_update_and_config_roundtrip(self):
        pool = pool_with([])
        self.assertTrue(pool.add("http://a:1", weight=2, note="家宽"))
        self.assertFalse(pool.add("http://a:1"))  # 重复
        self.assertTrue(pool.update("http://a:1", enabled=False))
        self.assertEqual(pool.assign(Fake("u1")), "")  # 停用后不参与
        self.assertTrue(pool.update("http://a:1", enabled=True, note="改过"))
        config = pool.to_config()
        self.assertEqual(config[0]["url"], "http://a:1")
        self.assertEqual(config[0]["weight"], 2.0)
        self.assertNotIn("realm", config[0], "域已经不属于出口的概念了")

        again = pool_with(config)
        self.assertEqual(again.size(), 1)
        self.assertTrue(again.remove("http://a:1"))
        self.assertFalse(again.remove("http://a:1"))

    def test_reload_keeps_runtime_health_and_bindings(self):
        pool = pool_with([{"url": "http://a:1"}])
        pool.assign(Fake("u1"))
        pool.note_failure("http://a:1")
        before = pool.stats()["entries"][0]["failures"]
        pool.reload()  # 相当于面板点了一次"保存"
        self.assertEqual(pool.stats()["entries"][0]["failures"], before)
        self.assertEqual(pool.binding_of("u1"), "http://a:1")

    def test_reset_health(self):
        pool = pool_with([{"url": "http://a:1"}])
        pool.note_failure("http://a:1")
        pool.reset_health()
        self.assertEqual(pool.stats()["cooling"], 0)


class EntryTest(unittest.TestCase):
    def test_weight_floor(self):
        entry = ProxyEntry("http://a:1", weight=0)
        self.assertEqual(entry.weight, 0.1)


if __name__ == "__main__":
    unittest.main()
