"""billing.py 的宽容解析：余额读不出来必须是 None，不能是 0。

余额这一个数字在系统里有真实后果：scheduler 每轮把它写进 account.credit，
pool 的 credit_floor 低于阈值就把账号踢出池子，面板上还直接展示。上游少给一个
字段就显示"余额 0 / 已欠费"，比不显示更糟。所以这里把几种信封形状、以及"读不懂
时返回 None"钉死。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb2api.billing import Billing, CHECKIN_PATH, RESOURCE_PATH
from wb2api.store import Account
from wb2api.upstream import UpstreamError


class FakeCfg:
    """跟 config.Config 一样用点号路径取配置。"""

    def __init__(self, data=None):
        self.data = data or {}

    def get(self, dotted, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.closed = False

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def close(self):
        self.closed = True


class FakeClient:
    """按脚本吐响应，同时把每次调用的 method/url/headers/body 记下来。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.responses = []

    def base_url(self, realm):
        return "https://%s.host.test" % realm

    def request(self, method, url, headers=None, raw_body=b"", proxy=""):
        self.calls.append({
            "method": method,
            "url": url,
            "headers": dict(headers or {}),
            "body": raw_body,
            "proxy": proxy,
        })
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        response = FakeResponse(item)
        self.responses.append(response)
        return response


class BillingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def account(self, realm="cn", **kw):
        kw.setdefault("uid", "u1")
        kw["realm"] = realm
        kw.setdefault("access_token", "at-1")
        return Account(**kw)

    def flow(self, script):
        client = FakeClient(script)
        return Billing(client, FakeCfg()), client


class CreditsTest(BillingBase):
    def credit(self, payload):
        billing, client = self.flow([payload])
        value = billing.credits(self.account(), "cn")
        self.assertEqual(len(client.calls), 1, "读余额必然打一次上游")
        call = client.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertTrue(call["url"].endswith(RESOURCE_PATH),
                        "余额接口路径变了？%s" % call["url"])
        self.assertTrue(client.responses[0].closed, "响应必须 close，否则连接池会慢慢漏")
        return value

    def test_data_remaining(self):
        self.assertEqual(self.credit({"code": 0, "data": {"remaining": 12.5}}), 12.5)

    def test_nested_resource_credits(self):
        # 新版把额度塞进 data.resource.credits 里，且 credits 本身还是个对象。
        payload = {"code": 0, "data": {"resource": {"credits": {"value": 33}}}}
        self.assertEqual(self.credit(payload), 33.0)

    def test_string_number(self):
        # 上游数字字段时而字符串时而数字，别因为类型就不认。
        self.assertEqual(self.credit({"data": {"balance": "8.25"}}), 8.25)

    def test_most_specific_key_wins(self):
        # remaining 比 total 具体：total 可能是"月度总量"，不是余额。
        self.assertEqual(self.credit({"data": {"total": 999, "remaining": 5}}), 5.0)

    def test_bare_number_inside_data_envelope(self):
        self.assertEqual(self.credit({"code": 0, "data": 42}), 42.0)

    def test_unreadable_payload_is_none_not_zero(self):
        # 这是本文件最重要的一组：读不懂要返回 None。
        # 旧实现会在整个 payload 里"找第一个数字"，于是 code=0 变成"余额 0"、
        # code=11100 变成"余额 11100"，面板假数据、账号被 credit_floor 踢掉。
        cases = [
            ({"code": 0, "msg": "ok"}, "只有状态码、没有 data"),
            ({"code": 0, "data": {"foo": "bar"}}, "data 里没有额度字段"),
            ({"code": 11100, "msg": "token expired"}, "错误信封，code 不是余额"),
            ({"code": 0, "data": {"remaining": True}}, "布尔值不能当额度"),
            ({}, "空对象"),
        ]
        for payload, why in cases:
            with self.subTest(why=why):
                self.assertIsNone(self.credit(payload), "%s：读不出额度必须返回 None" % why)

    def test_code_zero_no_data_is_none(self):
        self.assertIsNone(self.credit({"code": 0}))

    def test_upstream_error_is_none(self):
        billing, _client = self.flow([UpstreamError("boom", kind="transient", status=502)])
        self.assertIsNone(billing.credits(self.account(), "cn"),
                          "网络错误时不能返回 0——那等于告诉调度器账号没钱了")

    def test_broken_json_is_none(self):
        # 上游 200 但回了 HTML 错误页：json() 抛 ValueError。这同样算"读不到"，
        # 必须返回 None——refresh_balances 外面没有 try，异常会打断整轮余额刷新。
        billing, _client = self.flow([ValueError("not json")])
        self.assertIsNone(billing.credits(self.account(), "cn"))


class CheckinTest(BillingBase):
    def checkin(self, payload):
        billing, client = self.flow([payload])
        ok, message = billing.checkin(self.account(), "cn")
        call = client.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(call["url"].endswith(CHECKIN_PATH))
        self.assertEqual(json.loads(call["body"].decode("utf-8")), {},
                         "签到接口要一个空 JSON 体，不是空字节")
        return ok, message

    def test_code_zero_without_message(self):
        ok, message = self.checkin({"code": 0})
        self.assertTrue(ok)
        self.assertEqual(message, "已签到")

    def test_code_zero_with_message(self):
        ok, message = self.checkin({"code": 0, "msg": "今日已签到"})
        self.assertTrue(ok)
        self.assertEqual(message, "今日已签到")

    def test_missing_code_is_ok(self):
        # 有些版本成功时连 code 都不给。
        ok, _message = self.checkin({"data": {"checked": True}})
        self.assertTrue(ok)

    def test_string_zero_code_is_ok(self):
        # 上游确实混用字符串码（authflow 里两处都接受 "0"），签到这里以前会误判失败。
        ok, _message = self.checkin({"code": "0", "msg": "ok"})
        self.assertTrue(ok, 'code="0" 也是成功，不能判成失败')

    def test_business_error_code(self):
        ok, message = self.checkin({"code": 10001, "msg": "企业账号无需签到"})
        self.assertFalse(ok)
        self.assertEqual(message, "10001: 企业账号无需签到")

    def test_network_error(self):
        billing, _client = self.flow([UpstreamError("timeout", kind="transient")])
        ok, message = billing.checkin(self.account(), "cn")
        self.assertFalse(ok)
        self.assertIn("timeout", message)


class ReportTest(BillingBase):
    def test_report_posts_to_report_endpoint(self):
        billing, client = self.flow([{"code": 0}])
        payload = {"event": "active", "ts": 1700000000}
        self.assertTrue(billing.report(self.account(), "cn", payload))
        call = client.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(call["url"].endswith("/v2/report"), "上报路径必须是 /v2/report")
        self.assertEqual(json.loads(call["body"].decode("utf-8")), payload)
        self.assertTrue(client.responses[0].closed)

    def test_report_failure_is_swallowed(self):
        # 上报失败绝不能影响聊天请求本身。
        billing, _client = self.flow([UpstreamError("down", kind="transient")])
        self.assertFalse(billing.report(self.account(), "cn", {"a": 1}))


class HeadersTest(BillingBase):
    def headers_for(self, account, cfg=None):
        billing, client = self.flow([{"code": 0, "data": {"remaining": 1}}])
        billing.cfg = cfg or FakeCfg()
        billing.credits(account, account.realm)
        return client.calls[0]["headers"]

    def test_cn_headers_include_device_token(self):
        account = self.account(realm="cn", device_token="dev-1", enterprise_id="ent-1")
        headers = self.headers_for(account)
        self.assertEqual(headers.get("X-Device-Token"), "dev-1",
                         "国内域要带设备令牌，否则上游容易判成非官方客户端")
        self.assertEqual(headers.get("X-Enterprise-Id"), "ent-1")
        self.assertEqual(headers.get("X-Tenant-Id"), "ent-1")
        self.assertEqual(headers.get("X-Domain"), "codebuddy.cn")

    def test_global_headers_have_no_device_token(self):
        # 国际版客户端根本没有这个头，带上它等于把腾讯设备 ID 跨域带出去。
        account = self.account(realm="global", device_token="dev-1", enterprise_id="ent-1")
        headers = self.headers_for(account)
        self.assertNotIn("X-Device-Token", headers)
        self.assertNotIn("X-Enterprise-Id", headers, "global 不能自报企业 ID")
        self.assertEqual(headers.get("X-No-Enterprise-Id"), "1")
        self.assertEqual(headers.get("X-Domain"), "www.workbuddy.ai")

    def test_device_token_mode_never(self):
        account = self.account(realm="cn", device_token="dev-1")
        headers = self.headers_for(account, FakeCfg({"upstream": {"device_token_mode": "never"}}))
        self.assertNotIn("X-Device-Token", headers)

    def test_cli_product_suppresses_device_token(self):
        # auto 模式下 CLI 形态不注入设备令牌：CLI 本来就没有 QIMEI。
        account = self.account(realm="cn", device_token="dev-1", product="cli")
        headers = self.headers_for(account)
        self.assertNotIn("X-Device-Token", headers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
