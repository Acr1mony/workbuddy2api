"""pool/sticky 的行为测试。

重点不是覆盖率，而是把这几个"写错了很难查"的不变量钉住：
窗口互不覆盖、连续计数会衰减、11140 连坐、并发槽、会话粘滞。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from wb2api import pool, sticky, util
from wb2api.config import Config
from wb2api.store import Account, Store


class PoolTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="wb2a-pool-")
        self.cfg = Config(os.path.join(self.tmp, "config.json"))
        self.cfg.set("pool.credit_floor", 0)
        self.store = Store(os.path.join(self.tmp, "data"))
        self.affinity = sticky.SessionAffinity()
        self.pool = pool.Pool(self.store, self.cfg, self.affinity)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, uid: str, realm: str = "cn", **kwargs) -> Account:
        account = Account(uid=uid, id=uid, realm=realm, access_token="tok-" + uid, **kwargs)
        self.store.upsert_account(account, save=False)
        return account


class WindowTests(PoolTestBase):
    def test_soft_rate_ladder_doubles_and_is_capped(self) -> None:
        a = self.add("a")
        self.pool.note_soft_rate(a, reason="429")
        first = a.cooldown_until - util.now()
        self.assertAlmostEqual(first, 600, delta=2)

        a.last_soft_at = util.now()
        a.cooldown_until = 0.0  # 模拟冷却结束后又撞上
        self.pool.note_soft_rate(a, reason="429")
        second = a.cooldown_until - util.now()
        self.assertAlmostEqual(second, 1200, delta=2)

        for _ in range(10):
            a.cooldown_until = 0.0
            a.last_soft_at = util.now()
            self.pool.note_soft_rate(a, reason="429")
        self.assertLessEqual(a.cooldown_until - util.now(), pool.SOFT_RATE_MAX + 2)

    def test_soft_rate_with_reset_clock_is_exact(self) -> None:
        a = self.add("a")
        reset = util.now() + 90
        self.pool.note_soft_rate(a, reset_at=reset)
        self.assertAlmostEqual(a.cooldown_until, reset, delta=1)

    def test_soft_streak_decays_after_fail_window(self) -> None:
        a = self.add("a")
        self.pool.note_soft_rate(a, reason="429")
        self.assertEqual(a.soft_streak, 1)
        # 十分钟没动静，就不该继续当"连续"递增
        a.last_soft_at = util.now() - (pool.FAIL_WINDOW + 10)
        a.cooldown_until = 0.0
        self.pool.note_soft_rate(a, reason="429")
        self.assertEqual(a.soft_streak, 1)

    def test_unscoped_ladder_is_ten_times_shorter(self) -> None:
        a = self.add("a")
        self.pool.note_unscoped_rate(a)
        self.assertAlmostEqual(a.cooldown_until - util.now(), 60, delta=2)
        for _ in range(6):
            a.cooldown_until = 0.0
            a.last_soft_at = util.now()
            self.pool.note_unscoped_rate(a)
        self.assertLessEqual(a.cooldown_until - util.now(), pool.UNSCOPED_MAX + 2)

    def test_breaker_needs_threshold_then_escalates(self) -> None:
        a = self.add("a")
        self.pool.note_failure(a, "502")
        self.pool.note_failure(a, "502")
        self.assertEqual(a.breaker_until, 0.0)
        self.pool.note_failure(a, "502")
        first = a.breaker_until - util.now()
        self.assertAlmostEqual(first, 3600, delta=2)  # 1800 * 2^1

        a.breaker_until = 0.0
        for _ in range(3):
            a.last_fail_at = util.now()
            self.pool.note_failure(a, "502")
        self.assertAlmostEqual(a.breaker_until - util.now(), 7200, delta=2)  # 1800 * 2^2

    def test_failure_counter_decays(self) -> None:
        a = self.add("a")
        a.last_fail_at = util.now() - (pool.FAIL_WINDOW + 5)
        a.failures = 2
        self.pool.note_failure(a, "502")
        self.assertEqual(a.failures, 1)

    def test_degrade_at_threshold_and_does_not_extend(self) -> None:
        a = self.add("a")
        for _ in range(4):
            self.pool.note_unknown_failure(a, "weird")
        self.assertEqual(a.degrade_until, 0.0)
        self.pool.note_unknown_failure(a, "weird")
        window = a.degrade_until - util.now()
        self.assertAlmostEqual(window, 600, delta=2)
        # 已有窗口不被延长
        before = a.degrade_until
        for _ in range(5):
            a.breaker_until = 0.0
            a.last_fail_at = util.now()
            self.pool.note_unknown_failure(a, "weird")
        self.assertAlmostEqual(a.degrade_until, before, delta=1)

    def test_model_cooldown_does_not_touch_account_window(self) -> None:
        a = self.add("a")
        self.pool.note_soft_rate(a, model="glm-4")
        account_window = a.cooldown_until
        b = self.add("b")
        self.pool.block_model(b, "gpt-5", reason="11102")
        self.assertEqual(b.cooldown_until, 0.0)
        self.assertGreater(pool.model_cooldown_until(b, "gpt-5"), util.now())
        self.assertGreater(account_window, 0)

    def test_throttle_until_reports_longest_window(self) -> None:
        a = self.add("a")
        a.cooldown_until = util.now() + 60
        a.breaker_until = util.now() + 600
        until, name = pool.throttle_until(a)
        self.assertEqual(name, "breaker_until")
        self.assertAlmostEqual(until - util.now(), 600, delta=2)

    def test_success_clears_everything(self) -> None:
        a = self.add("a")
        self.pool.note_restricted(a)
        self.pool.note_hard_credit(a)
        self.pool.block_model(a, "x")
        self.pool.note_success(a, model="x")
        self.assertEqual(pool.throttle_until(a)[0], 0.0)
        self.assertEqual(a.model_cooldowns, {})
        self.assertEqual(a.state, "ready")

    def test_next_4am_is_in_future(self) -> None:
        now = util.now()
        target = pool.next_local_4am(now)
        self.assertGreater(target, now)
        self.assertLessEqual(target - now, 86400)
        self.assertEqual(time.localtime(target).tm_hour, 4)


class RestrictionTests(PoolTestBase):
    def test_restricted_is_two_hours(self) -> None:
        a = self.add("a")
        self.pool.note_restricted(a, "11140")
        self.assertAlmostEqual(a.restricted_until - util.now(), 7200, delta=2)

    def test_two_accounts_in_window_pause_the_realm(self) -> None:
        a = self.add("a")
        b = self.add("b")
        self.pool.note_restricted(a)
        self.assertEqual(self.pool.realm_hold_remaining("cn"), 0.0)
        self.pool.note_restricted(b)
        remaining = self.pool.realm_hold_remaining("cn")
        self.assertAlmostEqual(remaining, 1800, delta=2)
        # 整域暂停期间不选号
        self.add("c")
        self.assertIsNone(self.pool.pick("cn", "glm-4"))
        # global 域不受影响
        self.assertIsNotNone(self.pool.pick("global", "glm-4") if self.add("g", realm="global") else None)

    def test_hold_can_be_clamped(self) -> None:
        a = self.add("a")
        b = self.add("b")
        self.pool.note_restricted(a)
        self.pool.note_restricted(b)
        self.cfg.set("pool.restriction_hold_seconds", 60)
        self.pool.clamp_restriction_holds()
        self.assertLessEqual(self.pool.realm_hold_remaining("cn"), 61)


class ModelBlockTests(PoolTestBase):
    """"模型不可用"必须能和"账号在冷却"区分开：前者客户端换模型，后者等一会儿。"""

    def test_all_accounts_blocked_reports_model(self) -> None:
        a = self.add("a")
        b = self.add("b")
        self.pool.block_model(a, "glm-4.6", "service info not found")
        self.pool.block_model(b, "glm-4.6", "service info not found")
        info = self.pool.model_blocked("cn", "glm-4.6")
        self.assertTrue(info)
        self.assertIn("service info not found", info["reason"])
        self.assertGreater(info["until"], time.time())

    def test_one_free_account_means_not_blocked(self) -> None:
        a = self.add("a")
        self.add("b")
        self.pool.block_model(a, "glm-4.6", "boom")
        self.assertEqual(self.pool.model_blocked("cn", "glm-4.6"), {})

    def test_other_model_and_realm_unaffected(self) -> None:
        a = self.add("a")
        self.add("b")
        for account in (a, self.store.list_accounts("cn")[1]):
            self.pool.block_model(account, "glm-4.6", "boom")
        self.assertEqual(self.pool.model_blocked("cn", "hy3"), {})
        self.assertEqual(self.pool.model_blocked("global", "glm-4.6"), {})

    def test_no_accounts_is_not_a_model_problem(self) -> None:
        self.assertEqual(self.pool.model_blocked("cn", "glm-4.6"), {})


class SelectionTests(PoolTestBase):
    def test_credit_floor_blocks_only_paid_models(self) -> None:
        self.cfg.set("pool.credit_floor", 100)
        self.cfg.set("pool.model_rates", {"paid-model": 0.5, "free-model": 0.0})
        cheap = self.add("cheap", credit=10.0)
        self.assertFalse(self.pool.is_ready(cheap, "paid-model"))
        self.assertTrue(self.pool.is_ready(cheap, "free-model"))
        # 未知成本默认放行（目录没预热时全拦会直接让网关不可用）
        self.assertTrue(self.pool.is_ready(cheap, "unknown-model"))
        self.cfg.set("pool.credit_floor_unknown", "block")
        self.assertFalse(self.pool.is_ready(cheap, "unknown-model"))

    def test_session_affinity_keeps_one_account(self) -> None:
        for i in range(5):
            self.add(f"acct{i}")
        key = "conv-42"
        first = self.pool.select("cn", "glm-4", session_key=key)
        self.assertIsNotNone(first)
        self.pool.note_success(first, session_key=key)
        self.pool.release(first)
        seen = set()
        for _ in range(10):
            account = self.pool.select("cn", "glm-4", session_key=key)
            seen.add(account.uid)
            self.pool.release(account)
        self.assertEqual(seen, {first.uid})

    def test_affinity_demote_only_drops_failed_account(self) -> None:
        self.affinity.bind("k", "a")
        self.affinity.bind("k", "b")
        self.affinity.demote("k", "b")
        self.assertEqual(self.affinity.candidates("k"), ["a"])

    def test_concurrency_cap(self) -> None:
        self.cfg.set("pool.max_in_flight", 1)
        a = self.add("only")
        self.assertTrue(self.pool.acquire(a))
        self.assertFalse(self.pool.acquire(a))
        self.assertIsNone(self.pool.pick("cn", "glm-4"))
        self.pool.release(a)
        self.assertIsNotNone(self.pool.pick("cn", "glm-4"))

    def test_pick_skips_excluded_and_disabled(self) -> None:
        self.add("x", disabled=True)
        self.add("y")
        chosen = self.pool.pick("cn", "glm-4", exclude=["y"])
        self.assertIsNone(chosen)

    def test_realm_isolation(self) -> None:
        self.add("cn-only", realm="cn")
        self.assertIsNone(self.pool.pick("global", "glm-4"))

    def test_weighted_spreads_load(self) -> None:
        for i in range(4):
            self.add(f"w{i}")
        counts = {}
        for _ in range(200):
            account = self.pool.pick("cn", "glm-4")
            counts[account.uid] = counts.get(account.uid, 0) + 1
            account.last_used_at = util.now()
            account.used_seq += 1
        self.assertEqual(len(counts), 4)
        self.assertLess(max(counts.values()), 120)


class StickyKeyTests(unittest.TestCase):
    def test_header_wins(self) -> None:
        key = sticky.session_key({"X-Conversation-Id": "abc"}, {"conversation_id": "zzz"})
        self.assertEqual(key, "abc")

    def test_body_conversation_id(self) -> None:
        key = sticky.session_key({}, {"conversation_id": "body-1"})
        self.assertEqual(key, "body-1")

    def test_metadata_fallback(self) -> None:
        key = sticky.session_key({}, {"metadata": {"conversation_id": "meta-1"}})
        self.assertEqual(key, "meta-1")

    def test_prefix_hash_is_stable_for_same_opening(self) -> None:
        body = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]}
        a = sticky.session_key({}, body)
        b = sticky.session_key({}, {"messages": body["messages"] + [{"role": "assistant", "content": "x"}]})
        self.assertTrue(a.startswith("pfx-"))
        self.assertEqual(a, b)

    def test_different_opening_differs(self) -> None:
        one = sticky.session_key({}, {"messages": [{"role": "user", "content": "hello"}]})
        two = sticky.session_key({}, {"messages": [{"role": "user", "content": "goodbye"}]})
        self.assertNotEqual(one, two)

    def test_expiry_drops_binding(self) -> None:
        aff = sticky.SessionAffinity(ttl=0.01)
        aff.bind("k", "a")
        time.sleep(0.02)
        self.assertEqual(aff.candidates("k"), [])

    def test_history_is_bounded(self) -> None:
        aff = sticky.SessionAffinity(history=3)
        for uid in "abcdef":
            aff.bind("k", uid)
        self.assertEqual(aff.candidates("k"), ["f", "e", "d"])


if __name__ == "__main__":
    unittest.main()
