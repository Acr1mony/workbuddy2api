"""上游身份头：这里错一个字段，线上就是成片的 400/403，所以逐条钉住。

所有断言都对着官方客户端的实际报文（见 docs/PROTOCOL.md），不是对着实现抄。
"""

import os
import tempfile
import unittest

from wb2api import identity


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


class FakeAccount:
    def __init__(self, uid="u-1", realm="cn", **kwargs):
        self.uid = uid
        self.realm = realm
        self.access_token = kwargs.get("access_token", "at-123")
        self.refresh_token = kwargs.get("refresh_token", "rt-123")
        self.device_id = kwargs.get("device_id", "")
        self.session_id = kwargs.get("session_id", "")
        self.device_token = kwargs.get("device_token", "")
        self.enterprise_id = kwargs.get("enterprise_id", "")
        self.domain = kwargs.get("domain", "")
        self.product = kwargs.get("product", "workbuddy")


def body_with(*messages):
    return {"model": "x", "messages": list(messages)}


class TestRealm(unittest.TestCase):
    def test_aliases(self):
        for value in ("global", "GLOBAL", "intl", "international", "www", "ai"):
            self.assertEqual(identity.normalize_realm(value), "global", value)
        for value in ("cn", "", None, "domestic", "随便"):
            self.assertEqual(identity.normalize_realm(value), "cn", value)

    def test_hosts(self):
        self.assertEqual(identity.host_for("cn"), "https://copilot.tencent.com")
        self.assertEqual(identity.host_for("global"), "https://www.workbuddy.ai")

    def test_env_override(self):
        os.environ["WB_UPSTREAM_HOST"] = "https://example.test/"
        try:
            self.assertEqual(identity.host_for("cn"), "https://example.test")
        finally:
            os.environ.pop("WB_UPSTREAM_HOST", None)


class TestUserAgent(unittest.TestCase):
    def test_cn_vs_global_brand(self):
        self.assertEqual(identity.user_agent("cn"), "WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1")
        self.assertEqual(identity.user_agent("global"), "WorkBuddy AI/5.5.4 WorkBuddy AI/5.5.4 CLI/2.137.1")

    def test_version_overrides(self):
        ua = identity.user_agent("cn", client_version="9.9.9", cli_version="1.1.1")
        self.assertEqual(ua, "WorkBuddy/9.9.9 WorkBuddy/9.9.9 CLI/1.1.1")


class TestStableIds(unittest.TestCase):
    def test_stable_per_uid(self):
        first = identity.stable_id("abc", "machine")
        second = identity.stable_id("abc", "machine")
        self.assertEqual(first, second, "同一 uid 必须恒同值，重启也不变")
        self.assertEqual(len(first), 36)
        int(first, 16)

    def test_different_purposes_and_uids_differ(self):
        self.assertNotEqual(identity.stable_id("abc", "machine"), identity.stable_id("abc", "session"))
        self.assertNotEqual(identity.stable_id("abc", "machine"), identity.stable_id("abd", "machine"))

    def test_empty_uid(self):
        self.assertEqual(identity.stable_id("", "machine"), "")

    def test_explicit_overrides(self):
        account = FakeAccount(device_id="d" * 36, session_id="s" * 36)
        self.assertEqual(identity.machine_id(account), "d" * 36)
        self.assertEqual(identity.session_id(account), "s" * 36)


class TestTurnKey(unittest.TestCase):
    def test_uses_last_user_message(self):
        body = body_with(
            {"role": "user", "content": "第一轮"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "第二轮"},
            {"role": "assistant", "content": "tool 调用前的半截"},
        )
        key = identity.turn_key(body)
        self.assertTrue(key)

    def test_same_turn_same_key_new_turn_new_key(self):
        base = [{"role": "user", "content": "第一轮"}]
        first = identity.turn_key(body_with(*base))
        # 同一轮里追加 assistant/tool 消息，键不能变（多轮 tool call 要聚合到一起）
        extended = body_with(*base + [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "结果"},
        ])
        self.assertEqual(identity.turn_key(extended), first)
        # 用户发了新消息 → 换键
        self.assertNotEqual(identity.turn_key(body_with(*base + [{"role": "user", "content": "第二轮"}])), first)

    def test_no_user_message(self):
        self.assertEqual(identity.turn_key(body_with({"role": "system", "content": "x"})), "")

    def test_text_parts(self):
        single = identity.turn_key(body_with({"role": "user", "content": "hello"}))
        parts = identity.turn_key(body_with({"role": "user", "content": [{"type": "text", "text": "hello"}]}))
        self.assertEqual(single, parts, "字符串与等价的 text parts 应当是同一条会话")


class TestConversationId(unittest.TestCase):
    def test_metadata_first(self):
        body = {"metadata": {"conversationId": "conv-1"}, "conversation_id": "conv-2"}
        self.assertEqual(identity.conversation_id_from_body(body), "conv-1")

    def test_top_level(self):
        self.assertEqual(identity.conversation_id_from_body({"conversation_id": "conv-3"}), "conv-3")

    def test_never_falls_back_to_user_id(self):
        body = {"user": "user-42", "messages": [{"role": "user", "content": "x"}]}
        self.assertEqual(identity.conversation_id_from_body(body), "",
                         "拿 user 当会话会把所有人的对话并成一条")

    def test_chat_meta_generates_ids(self):
        meta = identity.ChatMeta.for_request(body_with({"role": "user", "content": "x"}))
        for value in (meta.conversation_id, meta.conversation_request_id, meta.message_id):
            self.assertEqual(len(value), 32, value)

    def test_same_turn_same_request_id(self):
        body = body_with({"role": "user", "content": "x"})
        first = identity.ChatMeta.for_request(body)
        second = identity.ChatMeta.for_request(body)
        self.assertEqual(first.conversation_request_id, second.conversation_request_id)
        self.assertNotEqual(first.message_id, second.message_id, "每次请求都是新消息")

    def test_conversation_id_is_stable_for_a_session(self):
        """客户端不带 conversation_id 时，同一段对话必须拿到同一个会话 id。

        每轮随机等于告诉上游"这个客户端几分钟里开了几十个会话"。
        """
        body = body_with({"role": "user", "content": "x"})
        first = identity.ChatMeta.for_request(body, session_hint="conv-key-1")
        second = identity.ChatMeta.for_request(body, session_hint="conv-key-1")
        self.assertEqual(first.conversation_id, second.conversation_id)
        self.assertEqual(len(first.conversation_id), 32)
        other = identity.ChatMeta.for_request(body, session_hint="conv-key-2")
        self.assertNotEqual(first.conversation_id, other.conversation_id)

    def test_body_conversation_id_wins_over_session_hint(self):
        body = body_with({"role": "user", "content": "x"})
        body["conversation_id"] = "client-conv"
        meta = identity.ChatMeta.for_request(body, session_hint="ignored")
        self.assertEqual(meta.conversation_id, "client-conv")


class TestChatHeaders(unittest.TestCase):
    def test_required_headers_cn(self):
        headers = identity.chat_headers(FakeAccount(), FakeCfg())
        self.assertEqual(headers["X-CodeBuddy-Request"], "1")
        self.assertEqual(headers["Origin"], "https://www.codebuddy.cn")
        self.assertEqual(headers["Referer"], "https://www.codebuddy.cn/")
        self.assertEqual(headers["Accept-Language"], "zh-CN")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("text/event-stream", headers["Accept"])
        self.assertEqual(headers["Authorization"], "Bearer at-123")
        self.assertEqual(headers["X-User-Id"], "u-1")
        self.assertNotIn("X-Refresh-Token", headers, "refresh token 绝不能出现在 chat 头上")
        for name in ("X-Conversation-ID", "X-Conversation-Request-ID", "X-Conversation-Message-ID",
                     "X-Request-ID", "X-Root-Request-ID", "X-Trace-ID", "X-B3-TraceId",
                     "X-B3-SpanId", "X-B3-Sampled"):
            self.assertTrue(headers.get(name), name)

    def test_b3_shapes(self):
        headers = identity.chat_headers(FakeAccount(), FakeCfg())
        self.assertEqual(len(headers["X-B3-SpanId"]), 16)
        self.assertEqual(headers["X-B3-Sampled"], "1")
        self.assertTrue(headers["traceparent"].startswith("00-"))
        self.assertEqual(headers["traceparent"].count("-"), 3)

    def test_missing_token_says_so(self):
        headers = identity.chat_headers(FakeAccount(access_token=""), FakeCfg())
        self.assertEqual(headers["X-No-Authorization"], "1")
        self.assertNotIn("Authorization", headers)

    def test_static_key_also_sends_api_key(self):
        headers = identity.chat_headers(FakeAccount(access_token="ck_abc"), FakeCfg())
        self.assertEqual(headers["X-API-Key"], "ck_abc")
        headers = identity.chat_headers(FakeAccount(access_token="at-1"), FakeCfg())
        self.assertNotIn("X-API-Key", headers)

    def test_global_declares_no_enterprise(self):
        headers = identity.chat_headers(FakeAccount(realm="global"), FakeCfg())
        self.assertEqual(headers["X-No-Enterprise-Id"], "1")
        self.assertEqual(headers["X-Domain"], "www.workbuddy.ai")
        self.assertNotIn("X-Enterprise-Id", headers)
        self.assertEqual(headers["Accept-Language"], "en-US")
        self.assertIn("WorkBuddy AI/", headers["User-Agent"])

    def test_cn_enterprise(self):
        headers = identity.chat_headers(FakeAccount(enterprise_id="ent-9"), FakeCfg())
        self.assertEqual(headers["X-Enterprise-Id"], "ent-9")
        self.assertEqual(headers["X-Tenant-Id"], "ent-9")
        self.assertNotIn("X-No-Enterprise-Id", headers)

    def test_cli_product_drops_browser_headers(self):
        headers = identity.chat_headers(FakeAccount(product="cli"), FakeCfg())
        self.assertNotIn("Origin", headers)
        self.assertNotIn("Referer", headers)
        self.assertNotIn("X-Requested-With", headers)
        self.assertTrue(headers["User-Agent"].startswith("CLI/"))

    def test_passthrough_ip_is_opt_in(self):
        headers = identity.chat_headers(FakeAccount(), FakeCfg(), client_ip="1.2.3.4")
        self.assertNotIn("X-Forwarded-For", headers)
        headers = identity.chat_headers(
            FakeAccount(), FakeCfg({"upstream": {"passthrough_ip": True}}), client_ip="1.2.3.4")
        self.assertEqual(headers["X-Forwarded-For"], "1.2.3.4")
        self.assertEqual(headers["X-Real-IP"], "1.2.3.4")

    def test_saas_client_name(self):
        headers = identity.chat_headers(FakeAccount(), FakeCfg({"upstream": {"client_name": "SaaS"}}))
        self.assertEqual(headers["X-Product"], "SaaS")
        self.assertNotIn("X-IDE-Name", headers)


class TestRefreshHeaders(unittest.TestCase):
    def test_token_goes_in_header(self):
        headers = identity.refresh_headers(FakeAccount(), FakeCfg())
        self.assertEqual(headers["X-Refresh-Token"], "rt-123")
        self.assertNotIn("Authorization", headers)

    def test_source_is_per_realm(self):
        self.assertEqual(identity.refresh_headers(FakeAccount(), FakeCfg())["X-Auth-Refresh-Source"], "workbuddy")
        self.assertEqual(
            identity.refresh_headers(FakeAccount(realm="global"), FakeCfg())["X-Auth-Refresh-Source"], "plugin")


class TestBillingHeaders(unittest.TestCase):
    def test_global_never_sends_tencent_device_token(self):
        cfg = FakeCfg({"upstream": {"device_token": "dev-xyz"}})
        headers = identity.billing_headers(FakeAccount(realm="global"), cfg)
        self.assertNotIn("X-Device-Token", headers, "国际版没有这个头，带上它等于跨账号关联键")
        self.assertNotIn("X-Enterprise-Id", headers)
        self.assertEqual(headers["X-No-Enterprise-Id"], "1")

    def test_cn_sends_device_token(self):
        cfg = FakeCfg({"upstream": {"device_token": "dev-xyz"}})
        headers = identity.billing_headers(FakeAccount(), cfg)
        self.assertEqual(headers["X-Device-Token"], "dev-xyz")
        self.assertIn("Authorization", headers)


class TestDeviceToken(unittest.TestCase):
    def tearDown(self):
        identity.clear_device_cache()

    def test_realm_and_product_gating(self):
        cfg = FakeCfg({"upstream": {"device_token": "dev-1"}})
        self.assertEqual(identity.device_token_for(FakeAccount(), cfg), "dev-1")
        self.assertEqual(identity.device_token_for(FakeAccount(realm="global"), cfg), "")
        self.assertEqual(identity.device_token_for(FakeAccount(product="cli"), cfg), "")
        self.assertEqual(identity.device_token_for(FakeAccount(), FakeCfg({"upstream": {"device_token_mode": "never"}})), "")

    def test_account_value_wins(self):
        cfg = FakeCfg({"upstream": {"device_token": "dev-1"}})
        self.assertEqual(identity.device_token_for(FakeAccount(device_token="own"), cfg), "own")

    def test_file_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "device.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("from-file\n")
            cfg = FakeCfg({"upstream": {"device_token_file": path}})
            self.assertEqual(identity.device_token_for(FakeAccount(), cfg), "from-file")
            # 缓存命中后即使文件改了也读缓存（避免每次请求都打盘）
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("changed\n")
            self.assertEqual(identity.device_token_for(FakeAccount(), cfg), "from-file")
            identity.clear_device_cache()
            self.assertEqual(identity.device_token_for(FakeAccount(), cfg), "changed")

    def test_missing_file_is_empty(self):
        cfg = FakeCfg({"upstream": {"device_token_file": os.path.join(tempfile.gettempdir(), "nope-xyz.txt")}})
        self.assertEqual(identity.device_token_for(FakeAccount(), cfg), "")


if __name__ == "__main__":
    unittest.main()
