"""store 的磁盘契约。

账号、网关密钥、运行期设置都靠这三个 JSON 文件活着：面板改完、进程重启后必须
一字不差地读回来；落盘必须原子（断电不能留下半个文件）；面板拿到的 ``masked()``
绝不能带出任何 token。这三件事错了都不会崩，只会悄悄丢号或泄密。
"""

from __future__ import annotations

import glob
import json
import os
import tempfile
import unittest

from wb2api import util
from wb2api.store import ACCOUNT_FILE, RUNTIME_FILE, Account, ApiKey, Store


class StoreTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wb2a-store-")
        self.data_dir = os.path.join(self.tmp.name, "data")
        self.store = Store(self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def reopen(self) -> Store:
        """同一目录重开一个 Store：模拟进程重启，只信磁盘。"""
        return Store(self.data_dir)

    def file(self, name: str) -> str:
        return os.path.join(self.data_dir, name)


class AccountRoundTripTests(StoreTestBase):
    def test_every_persisted_field_survives_a_restart(self) -> None:
        account = Account(
            uid="uid-1",
            id="local-1",
            realm="global",
            nickname="测试账号",
            email="a@b.test",
            avatar="https://cdn.test/a.png",
            access_token="at-1",
            refresh_token="rt-1",
            expires_at=1800000000.0,
            device_token="dt-1",
            device_id="dev-1",
            session_id="sess-1",
            proxy="http://u:p@proxy.test:8080",
            group="g1",
            disabled=True,
            note="备注",
            enterprise_id="ent-1",
            domain="corp.test",
            product="cli",
            source="import",
            static_key=True,
            weight=2.5,
            tier="pro",
            restricted_until=1800000000.0,
            credit=12.5,
            credit_at=1800000000.0,
            cooldown_until=1800000000.0,
            cooldown_reason="429",
            cooldown_level="soft",
            failures=2,
            state="cooldown",
            balance_until=1800000000.0,
            breaker_until=1800000000.0,
            degrade_until=1800000000.0,
            soft_streak=1,
            unscoped_streak=2,
            consecutive_fails=3,
            breaker_retries=4,
            model_cooldowns={"glm-4": {"until": 1800000000.0, "reason": "11102", "hits": 1}},
            last_fail_at=1800000000.0,
            last_soft_at=1800000000.0,
        )
        account.extra = {"future_field": {"nested": [1, 2]}}
        self.store.upsert_account(account)
        expected = account.to_dict()

        loaded = self.reopen().get_account("uid-1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.to_dict(), expected, "重启后字段必须一字不差")
        self.assertEqual(loaded.nickname, "测试账号", "中文不能变成转义序列或乱码")
        self.assertEqual(loaded.model_cooldowns["glm-4"]["reason"], "11102")


class AtomicWriteTests(StoreTestBase):
    def test_saved_file_is_complete_valid_json(self) -> None:
        self.store.upsert_account(Account(uid="u1", nickname="原子"))
        target = self.file(ACCOUNT_FILE)
        self.assertTrue(os.path.exists(target))
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(len(payload["accounts"]), 1)
        self.assertEqual(payload["accounts"][0]["uid"], "u1")
        self.assertEqual(payload["accounts"][0]["nickname"], "原子")
        # 原子写的临时文件不能残留（残留说明某次 rename 失败了）
        self.assertEqual(glob.glob(os.path.join(self.data_dir, ".tmp-*")), [])

    def test_save_overwrites_previous_content(self) -> None:
        self.store.upsert_account(Account(uid="u1"))
        self.store.upsert_account(Account(uid="u2"))
        self.assertTrue(self.store.remove_account("u1"))
        payload = util.read_json(self.file(ACCOUNT_FILE))
        self.assertEqual([item["uid"] for item in payload["accounts"]], ["u2"])
        self.assertFalse(self.store.remove_account("missing"), "不存在的账号不该触发写盘")


class UnknownKeyTests(StoreTestBase):
    def test_unknown_fields_are_kept_in_extra_and_written_back(self) -> None:
        # 模拟"新版本写过、旧版本读到"的字段：降级不能把它删掉。
        util.atomic_write_json(
            self.file(ACCOUNT_FILE),
            {"version": 1, "accounts": [{"uid": "u-future", "future_field": {"nested": [1, 2]}}]},
        )
        store = self.reopen()
        account = store.get_account("u-future")
        self.assertEqual(account.extra, {"future_field": {"nested": [1, 2]}})
        store.save_accounts()
        saved = util.read_json(self.file(ACCOUNT_FILE))["accounts"][0]
        self.assertEqual(saved["future_field"], {"nested": [1, 2]})


class MaskedViewTests(StoreTestBase):
    def test_masked_never_contains_token_values(self) -> None:
        account = Account(
            uid="u1",
            id="local-1",
            access_token="at-secret-123",
            refresh_token="rt-secret-123",
            device_token="dt-secret-123",
            static_key=True,
            proxy="http://user:pass@proxy.test:8080",
            enterprise_id="ent-secret-123",
        )
        masked = account.masked()
        blob = json.dumps(masked, ensure_ascii=False)
        for secret in ("at-secret-123", "rt-secret-123", "dt-secret-123", "ent-secret-123", "user:pass"):
            self.assertNotIn(secret, blob, "面板视图泄露了 %s" % secret)

        # 密钥字段本身不出现；只保留"有没有设置"的布尔位。
        self.assertNotIn("access_token", masked)
        self.assertNotIn("refresh_token", masked)
        self.assertTrue(masked["has_access_token"])
        self.assertTrue(masked["has_refresh_token"])
        # device_token 用 "set" 占位；static_key 是布尔标志，真正的密钥在 access_token 里。
        self.assertEqual(masked["device_token"], "set")
        self.assertTrue(masked["static_key"])
        self.assertIn("***", masked["proxy"])

    def test_masked_reports_missing_credentials(self) -> None:
        masked = Account(uid="u1").masked()
        self.assertFalse(masked["has_access_token"])
        self.assertFalse(masked["has_refresh_token"])
        self.assertEqual(masked["device_token"], "")
        self.assertEqual(masked["proxy"], "")
        self.assertEqual(masked["uid"], "u1")


class ApiKeyTests(StoreTestBase):
    def test_permissions_survive_a_restart(self) -> None:
        item = ApiKey(
            key="gw-key-1",
            name="测试密钥",
            realm="global",
            models=["glm-4", "gpt-*"],
            excluded_models=["gpt-4o-mini"],
            accounts=["u1"],
            groups=["g1"],
            disabled=True,
            requests=3,
            prompt_tokens=100,
            completion_tokens=50,
        )
        item.extra = {"future_field": "v"}
        self.store.add_key(item)

        loaded = self.reopen().get_key("gw-key-1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.to_dict(), item.to_dict())
        self.assertTrue(loaded.disabled)
        self.assertEqual(loaded.models, ["glm-4", "gpt-*"])
        self.assertEqual(loaded.extra, {"future_field": "v"})

    def test_allows_model_exact_wildcard_and_exclusion(self) -> None:
        exact = ApiKey(key="k1", models=["glm-4"])
        self.assertTrue(exact.allows_model("glm-4"))
        self.assertTrue(exact.allows_model("GLM-4"), "模型名大小写不敏感")
        self.assertFalse(exact.allows_model("glm-4-plus"))

        wildcard = ApiKey(key="k2", models=["gpt-*"])
        self.assertTrue(wildcard.allows_model("gpt-5"))
        self.assertFalse(wildcard.allows_model("gpt"), "通配要求前缀完整命中")
        self.assertFalse(wildcard.allows_model("claude-3"))

        guarded = ApiKey(key="k3", models=["gpt-*"], excluded_models=["gpt-4o-mini"])
        self.assertFalse(guarded.allows_model("gpt-4o-mini"), "排除优先于白名单")
        self.assertTrue(guarded.allows_model("gpt-4o"))

        unrestricted = ApiKey(key="k4", excluded_models=["danger-*"])
        self.assertTrue(unrestricted.allows_model("anything"), "空白名单 = 全部放行")
        self.assertFalse(unrestricted.allows_model("danger-model"))

        everything = ApiKey(key="k5", models=["*"])
        self.assertTrue(everything.allows_model("any-model"))

    def test_touch_key_counts_then_flush_persists(self) -> None:
        self.store.add_key(ApiKey(key="gw-key-1"))
        self.store.touch_key("gw-key-1", prompt_tokens=10, completion_tokens=5)
        self.store.touch_key("gw-key-1", prompt_tokens=-3, completion_tokens=2)
        live = self.store.get_key("gw-key-1")
        self.assertEqual(live.requests, 2)
        self.assertEqual(live.prompt_tokens, 10, "负数 token 计数要被夹到 0")
        self.assertEqual(live.completion_tokens, 7)
        self.assertGreater(live.last_used_at, 0.0)

        # touch_key 只改内存，落盘由 flush_keys 决定（否则每个请求都要写盘）。
        self.assertEqual(self.reopen().get_key("gw-key-1").requests, 0)
        self.store.flush_keys()
        self.assertEqual(self.reopen().get_key("gw-key-1").requests, 2)

    def test_touch_unknown_key_is_a_noop(self) -> None:
        self.store.touch_key("missing", 1, 1)
        self.assertEqual(self.store.list_keys(), [])


class RealmAndIdTests(StoreTestBase):
    def test_list_accounts_filters_by_realm(self) -> None:
        self.store.upsert_account(Account(uid="cn-1", realm="cn"))
        self.store.upsert_account(Account(uid="cn-2", realm="cn"))
        self.store.upsert_account(Account(uid="gl-1", realm="global"))
        self.assertEqual(len(self.store.list_accounts()), 3)
        self.assertEqual({a.uid for a in self.store.list_accounts("cn")}, {"cn-1", "cn-2"})
        self.assertEqual([a.uid for a in self.store.list_accounts("global")], ["gl-1"])
        self.assertEqual(self.store.list_accounts("mars"), [])

    def test_missing_realm_defaults_to_cn(self) -> None:
        util.atomic_write_json(self.file(ACCOUNT_FILE), {"accounts": [{"uid": "u1", "realm": ""}]})
        self.assertEqual(self.reopen().get_account("u1").realm, "cn")

    def test_ids_are_derived_from_uid_and_stable_across_stores(self) -> None:
        self.store.upsert_account(Account(uid="stable-uid"))
        first = self.reopen().get_account("stable-uid")
        second = self.reopen().get_account("stable-uid")
        self.assertEqual(first.id, "stable-uid", "没有本地 id 时用 uid 兜底")
        self.assertEqual(first.device_id, util.telegram_id("wb2a:device:stable-uid"))
        self.assertEqual(first.session_id, util.telegram_id("wb2a:session:stable-uid"))
        self.assertEqual(first.device_id, second.device_id, "同一 uid 跨实例必须得到同一设备码")
        self.assertEqual(first.session_id, second.session_id)
        self.assertEqual(len(first.device_id), 36)
        self.assertRegex(first.device_id, r"^[0-9a-f]{36}$")
        # ensure_ids 必须幂等，否则每次登录都会换设备指纹。
        self.assertEqual(first.ensure_ids().device_id, first.device_id)
        self.assertEqual(first.ensure_ids().session_id, first.session_id)


class RuntimeTests(StoreTestBase):
    def test_missing_runtime_file_is_empty_dict(self) -> None:
        self.assertEqual(self.store.runtime, {})
        self.assertFalse(os.path.exists(self.file(RUNTIME_FILE)), "读取不该创建文件")

    def test_runtime_round_trip(self) -> None:
        self.store.runtime["last_checkin"] = {"cn": 1800000000}
        self.store.runtime["schema"] = 2
        self.store.save_runtime()
        self.assertEqual(
            self.reopen().runtime, {"last_checkin": {"cn": 1800000000}, "schema": 2}
        )

    def test_non_dict_runtime_is_ignored(self) -> None:
        util.atomic_write_json(self.file(RUNTIME_FILE), [1, 2, 3])
        self.assertEqual(self.reopen().runtime, {})


if __name__ == "__main__":
    unittest.main()
