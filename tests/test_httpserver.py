"""httpserver 的骨架行为：路由匹配、请求解析、面板会话令牌、错误边界。

这一层如果错了，上面所有业务代码的报错都会变成 500 或"路由不存在"，
所以单独钉一遍。
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb2api.httpserver import (Application, BadRequest, Request, Response, Router,
                               SessionAuth, error_response, json_response)
from wb2api.store import Store


def req(method="GET", path="/", query=None, headers=None, body=b"", client=("1.2.3.4", 5000)):
    return Request(method, path, query or {}, headers or {}, body, client)


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.router = Router()

    def test_static_route(self):
        self.router.add("GET", "/healthz", lambda r: {"ok": True})
        handler, params, status = self.router.match("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(params, {})

    def test_path_params(self):
        self.router.add("POST", r"/api/accounts/(?P<uid>[^/]+)/delete", lambda r: None)
        handler, params, status = self.router.match("POST", "/api/accounts/u_1/delete")
        self.assertEqual(status, 200)
        self.assertEqual(params["uid"], "u_1")

    def test_regex_is_anchored(self):
        self.router.add("GET", "/v1/models", lambda r: None)
        _h, _p, status = self.router.match("GET", "/v1/models/extra")
        self.assertEqual(status, 404)

    def test_method_not_allowed(self):
        self.router.add("POST", "/panel/login", lambda r: None)
        _h, _p, status = self.router.match("GET", "/panel/login")
        self.assertEqual(status, 405)

    def test_unknown(self):
        _h, _p, status = self.router.match("GET", "/nope")
        self.assertEqual(status, 404)

    def test_route_decorator(self):
        @self.router.route("GET", "/ping")
        def _ping(r):
            return "pong"

        self.assertEqual(len(self.router.routes()), 1)


class RequestTest(unittest.TestCase):
    def test_arg(self):
        r = req(query={"limit": ["5"], "flag": ["1"]})
        self.assertEqual(r.arg("limit"), "5")
        self.assertEqual(r.arg("missing", "x"), "x")
        self.assertTrue(r.arg_bool("flag"))

    def test_bearer_prefers_authorization(self):
        r = req(headers={"authorization": "Bearer sk-1", "x-api-key": "sk-2"})
        self.assertEqual(r.bearer(), "sk-1")

    def test_bearer_falls_back_to_x_api_key(self):
        r = req(headers={"x-api-key": "sk-2"})
        self.assertEqual(r.bearer(), "sk-2")

    def test_bearer_empty(self):
        self.assertEqual(req().bearer(), "")

    def test_headers_lowercased(self):
        r = req(headers={"X-Panel-Token": "abc"})
        self.assertEqual(r.header("x-panel-token"), "abc")

    def test_client_ip_is_property(self):
        self.assertEqual(req(client=("9.9.9.9", 1)).client_ip, "9.9.9.9")

    def test_json_body(self):
        r = req(body=json.dumps({"a": 1}).encode())
        self.assertEqual(r.json_body(), {"a": 1})

    def test_json_body_empty_is_empty_dict(self):
        self.assertEqual(req().json_body(), {})

    def test_json_body_invalid_raises(self):
        with self.assertRaises(BadRequest):
            req(body=b"{oops").json_body()

    def test_json_body_not_object_raises(self):
        with self.assertRaises(BadRequest):
            req(body=b"[1,2]").json_body()

    def test_json_body_too_large(self):
        with self.assertRaises(BadRequest):
            req(body=b'{"a":"' + b"x" * 2048 + b'"}').json_body(max_bytes=1024)


class ResponseTest(unittest.TestCase):
    def test_json_response(self):
        resp = json_response({"ok": True})
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body), {"ok": True})
        self.assertIn("application/json", resp.content_type)

    def test_error_response_envelope(self):
        resp = error_response(503, "没有可用账号", "no_account")
        payload = json.loads(resp.body)
        self.assertEqual(resp.status, 503)
        self.assertEqual(payload["error"]["code"], "no_account")
        self.assertEqual(payload["error"]["message"], "没有可用账号")

    def test_stream_response_is_stream(self):
        from wb2api.httpserver import stream_response

        resp = stream_response([b"data: a\n\n"])
        self.assertIsNotNone(resp.stream)
        self.assertEqual(list(resp.stream), [b"data: a\n\n"])


class ApplicationTest(unittest.TestCase):
    def test_dispatch_returns_dict(self):
        router = Router()
        router.add("GET", "/ok", lambda r: {"a": 1})
        app = Application(router)
        resp = app.dispatch(req(path="/ok"))
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body), {"a": 1})

    def test_dispatch_wraps_bad_request(self):
        router = Router()

        @router.route("GET", "/bad")
        def _bad(r):
            raise BadRequest("字段缺失", 400, "missing_field")

        app = Application(router)
        resp = app.dispatch(req(path="/bad"))
        self.assertEqual(resp.status, 400)
        self.assertEqual(json.loads(resp.body)["error"]["code"], "missing_field")

    def test_dispatch_500_on_unexpected(self):
        router = Router()

        @router.route("GET", "/boom")
        def _boom(r):
            raise RuntimeError("boom")

        app = Application(router)
        resp = app.dispatch(req(path="/boom"))
        self.assertEqual(resp.status, 500)

    def test_dispatch_404(self):
        app = Application(Router())
        self.assertEqual(app.dispatch(req(path="/missing")).status, 404)

    def test_dispatch_405(self):
        router = Router()
        router.add("POST", "/only", lambda r: {})
        self.assertEqual(Application(router).dispatch(req(path="/only")).status, 405)


class SessionAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-auth-")
        self.store = Store(self.tmp)

    def test_issue_and_verify(self):
        auth = SessionAuth(self.store, ttl_seconds=3600)
        token, expires = auth.issue()
        self.assertGreater(expires, time.time())
        self.assertTrue(auth.verify(token))

    def test_verify_rejects_garbage(self):
        auth = SessionAuth(self.store)
        self.assertFalse(auth.verify(""))
        self.assertFalse(auth.verify("a.b.c"))

    def test_verify_rejects_expired(self):
        auth = SessionAuth(self.store, ttl_seconds=-1)
        token, _expires = auth.issue()
        self.assertFalse(auth.verify(token))

    def test_secret_is_stable_across_instances(self):
        token, _e = SessionAuth(self.store).issue()
        self.assertTrue(SessionAuth(Store(self.tmp)).verify(token))


if __name__ == "__main__":
    unittest.main(verbosity=2)
