"""入站 `X-Request-Id` 的复用与消毒。

为什么要复用：客户端（SDK、别的网关、你自己写的脚本）报障时给的是**它**的 id。
我们如果另发一个，日志/台账/响应头就和它对不上号，排障第一步就断了。

为什么不能原样收下：这个串会进日志、台账、响应头。换行、超长、奇怪的字符都能
把一行日志撕成两行，所以按白名单收窄，不合规就当没给。
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_service import FakeClient, ok_stream
from wb2api.config import Config
from wb2api.httpserver import Request
from wb2api.service import GatewayService, _request_id
from wb2api.store import Account, Store


class RequestIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-reqid-")
        self.cfg = Config(None, {"data_dir": self.tmp, "panel": {"enabled": False}})
        store = Store(self.tmp)
        store.upsert_account(Account(
            uid="u1", id="u1", realm="cn", access_token="tok-u1",
            refresh_token="rt-u1", expires_at=time.time() + 86400,
        ))

    def service(self, headers=None):
        svc = GatewayService(self.cfg, Store(self.tmp), client=FakeClient([ok_stream("hi")]))
        request = Request("POST", "/v1/chat/completions", {}, dict(headers or {}),
                          json.dumps({"model": "deepseek-v3",
                                      "messages": [{"role": "user", "content": "hi"}]}).encode(),
                          ("1.2.3.4", 1))
        return svc, request

    def test_client_request_id_is_reused(self):
        svc, request = self.service({"X-Request-Id": "sdk-abc.123:xyz"})
        resp = svc.chat_completions(request)
        self.assertEqual(resp.headers.get("X-Request-Id"), "sdk-abc.123:xyz")
        self.assertEqual(svc.ledger.recent(1)[0].get("request_id"), "sdk-abc.123:xyz")

    def test_correlation_id_is_honoured_when_request_id_is_absent(self):
        svc, request = self.service({"X-Correlation-Id": "corr-9"})
        resp = svc.chat_completions(request)
        self.assertEqual(resp.headers.get("X-Request-Id"), "corr-9")

    def test_hostile_request_id_is_replaced(self):
        """换行会伪造一行日志，所以宁可丢掉客户端的 id 也不能原样收。"""
        svc, request = self.service({"X-Request-Id": "bad id\nINJECTED line"})
        resp = svc.chat_completions(request)
        rid = resp.headers.get("X-Request-Id")
        self.assertTrue(rid.startswith("req_"), rid)
        self.assertNotIn("INJECTED", rid)

    def test_overlong_request_id_is_replaced(self):
        svc, request = self.service({"X-Request-Id": "a" * 65})
        resp = svc.chat_completions(request)
        self.assertTrue(resp.headers.get("X-Request-Id").startswith("req_"))

    def test_missing_header_generates_one(self):
        svc, request = self.service()
        resp = svc.chat_completions(request)
        rid = resp.headers.get("X-Request-Id")
        self.assertTrue(rid.startswith("req_"))
        self.assertEqual(len(rid), len("req_") + 8)

    def test_helper_keeps_prefixes_and_case(self):
        request = Request("GET", "/", {}, {"Request-Id": "Core-42.v2"}, b"", ("1.1.1.1", 1))
        self.assertEqual(_request_id(request), "Core-42.v2")


class ModuleLayoutTest(unittest.TestCase):
    """拆分之后的结构守卫：主文件不许再长回去，方法也不许被重复定义。"""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _lines(self, name):
        with open(os.path.join(self.ROOT, "wb2api", name), encoding="utf-8") as fh:
            return fh.read().splitlines()

    def test_service_module_stays_small(self):
        count = len(self._lines("service.py"))
        self.assertLess(count, 700, "service.py 又长到 %d 行了，按职责拆出去" % count)

    def test_gateway_service_is_built_from_mixins(self):
        from wb2api.failover import FailoverMixin
        from wb2api.relay import RelayMixin

        self.assertTrue(issubclass(GatewayService, RelayMixin))
        self.assertTrue(issubclass(GatewayService, FailoverMixin))
        for name in ("_relay_stream", "_relay_messages", "_record", "_absorb", "_client_error"):
            self.assertTrue(callable(getattr(GatewayService, name, None)), name)

    def test_no_method_is_defined_twice(self):
        """同名方法在 service 和 mixin 里各写一遍，MRO 会静默选一个——最难查的那种 bug。"""
        import ast

        def methods(path):
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef):
                            names.add(item.name)
            return names

        service = methods(os.path.join(self.ROOT, "wb2api", "service.py"))
        for part in ("relay.py", "failover.py"):
            overlap = service & methods(os.path.join(self.ROOT, "wb2api", part))
            self.assertEqual(overlap, set(), "%s 与 service.py 重复定义了这些方法" % part)


if __name__ == "__main__":
    unittest.main()
