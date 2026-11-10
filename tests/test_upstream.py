"""上游客户端：错误分类与响应读取。

分类错一档，处置就会错一档：把 403 风控当 auth 去刷新令牌，会白烧一次一次性
refresh token；把 429 的 6004 当普通限流，账号会一直"看起来可用"地空转。
"""

import json
import socket
import unittest

from wb2api import upstream


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


class FakeRaw:
    """http.client.HTTPResponse 的最小替身。"""

    def __init__(self, chunks, status=200, headers=None, fail=None):
        self.chunks = list(chunks)
        self.status = status
        self.reason = "OK"
        self._headers = headers or [("content-type", "application/json")]
        self.fail = fail

    def getheaders(self):
        return list(self._headers)

    def read(self, size=-1):
        if self.fail is not None:
            raise self.fail
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if size and size > 0 and len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
            return chunk[:size]
        return chunk


class FakeConn:
    def __init__(self):
        self.sock = None
        self.closed = 0

    def close(self):
        self.closed += 1


class FakePool:
    def __init__(self):
        self.released = []

    def release(self, key, conn, reusable=True):
        self.released.append((key, reusable))


def make_response(raw, pool=None, conn=None):
    return upstream.UpstreamResponse(conn or FakeConn(), raw, "key", pool or FakePool())


class TestClassify(unittest.TestCase):
    def test_401_is_auth(self):
        err = upstream.classify(401, '{"code":11100,"msg":"token expired"}')
        self.assertEqual(err.kind, "auth")
        self.assertFalse(err.retryable)

    def test_403_11140_is_restricted_not_auth(self):
        err = upstream.classify(403, '{"code":11140,"msg":"restricted"}')
        self.assertEqual(err.kind, "restricted", "11140 换号会连坐，必须与普通认证失败区分")
        self.assertEqual(err.code, 11140, "code 必须带出来")

    def test_403_html_page_is_waf_not_auth(self):
        err = upstream.classify(403, "<html><body>Request blocked by WAF</body></html>")
        self.assertEqual(err.kind, "waf", "边缘层 HTML 403 跟账号无关，不能去刷令牌/换号")

    def test_403_envelope_without_11140_is_auth(self):
        err = upstream.classify(403, '{"code":0,"msg":"invalid token"}')
        self.assertEqual(err.kind, "auth")

    def test_403_with_double_escaped_11140_is_still_restricted(self):
        body = json.dumps({"msg": '{"code":11140,"msg":"restricted"}'}, ensure_ascii=False)
        err = upstream.classify(403, body)
        self.assertEqual(err.kind, "restricted", "漏判一次就是换号连坐")

    def test_429_is_retryable_rate_limit(self):
        err = upstream.classify(429, '{"code":11104,"msg":"slow down"}', retry_after=12)
        self.assertEqual(err.kind, "rate_limit")
        self.assertTrue(err.retryable)
        self.assertEqual(err.retry_after, 12)

    def test_429_with_quota_marker_keeps_code(self):
        err = upstream.classify(429, '{"code":6004,"msg":"额度已用完，将在 2026-01-01 00:00:00 重置"}')
        self.assertEqual(err.code, 6004, "6004 是唯一的额度耗尽标记")

    def test_402_and_quota_codes_are_quota(self):
        self.assertEqual(upstream.classify(402, '{"code":0,"msg":"insufficient balance"}').kind, "quota")
        self.assertEqual(upstream.classify(200, '{"code":11109,"msg":"quota"}').kind, "quota")

    def test_bad_request_codes(self):
        for code in (11101, 11133, 11148, 11103):
            err = upstream.classify(400, json.dumps({"code": code, "msg": "bad"}))
            self.assertEqual(err.kind, "bad_request", code)

    def test_transient_statuses(self):
        for status in (500, 502, 503, 504, 520, 524):
            err = upstream.classify(status, "")
            self.assertEqual(err.kind, "transient", status)
            self.assertTrue(err.retryable)

    def test_unknown_5xx_is_still_transient(self):
        self.assertEqual(upstream.classify(599, "").kind, "transient")

    def test_other_4xx_is_bad_request(self):
        self.assertEqual(upstream.classify(404, "").kind, "bad_request")

    def test_non_json_body(self):
        err = upstream.classify(401, "<html>openresty 401 Authorization Required</html>")
        self.assertEqual(err.kind, "auth")
        self.assertIn("openresty", err.message, "边缘层 HTML 错误页也要能读出人话")

    def test_message_extraction_shapes(self):
        shapes = [
            {"code": 10001, "msg": "msg 字段"},
            {"errorCode": "10001", "message": "errorCode/message"},
            {"error": {"code": 10001, "message": "嵌套 error"}},
            {"error": "字符串 error"},
            {"statusText": "statusText 字段"},
        ]
        for payload in shapes:
            err = upstream.classify(400, json.dumps(payload, ensure_ascii=False))
            self.assertTrue(err.message, payload)

    def test_code_as_string(self):
        err = upstream.classify(400, '{"code":"11115","msg":"prompt too long"}')
        self.assertEqual(err.code, 11115, "字符串数字也要认，否则处置整条走空")

    def test_empty_body_message(self):
        err = upstream.classify(503, "")
        self.assertIn("503", err.message)


class TestUpstreamError(unittest.TestCase):
    def test_shape(self):
        err = upstream.UpstreamError("boom", kind="transient", status=503, code=6021, retryable=True)
        self.assertEqual(err.as_dict(), {
            "kind": "transient", "status": 503, "code": 6021, "message": "boom", "retryable": True,
        })
        self.assertIn("[transient 503]", str(err))

    def test_flags(self):
        self.assertTrue(upstream.UpstreamError("x", kind="auth").auth_failure)
        self.assertTrue(upstream.UpstreamError("x", kind="quota").quota_failure)


class TestRetryAfter(unittest.TestCase):
    def test_numeric(self):
        self.assertEqual(upstream._retry_after({"retry-after": "7"}), 7.0)

    def test_garbage(self):
        self.assertEqual(upstream._retry_after({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}), 0.0)
        self.assertEqual(upstream._retry_after({}), 0.0)


class TestResponse(unittest.TestCase):
    def test_text_and_close(self):
        pool = FakePool()
        response = make_response(FakeRaw([b"hello ", b"world"]), pool)
        self.assertEqual(response.text(), "hello world")
        response.close()
        response.close()
        self.assertEqual(len(pool.released), 1, "close 必须幂等，否则连接会被重复入池")

    def test_json(self):
        response = make_response(FakeRaw([b'{"code":0}']))
        self.assertEqual(response.json(), {"code": 0})

    def test_json_on_html_raises_upstream_error(self):
        response = make_response(FakeRaw([b"<html>"]))
        with self.assertRaises(upstream.UpstreamError):
            response.json()

    def test_iter_lines_handles_split_and_trailing(self):
        raw = FakeRaw([b"data: 1\n\ndata: ", b"2\n", b"\r\ndata: 3"])
        lines = list(make_response(raw).iter_lines())
        self.assertEqual(lines, [b"data: 1", b"", b"data: 2", b"", b"data: 3"])

    def test_iter_text_decodes_utf8(self):
        raw = FakeRaw(["你好\n".encode("utf-8")])
        self.assertEqual(list(make_response(raw).iter_text()), ["你好"])

    def test_idle_timeout_is_its_own_kind(self):
        raw = FakeRaw([], fail=socket.timeout("timed out"))
        with self.assertRaises(upstream.UpstreamError) as ctx:
            list(make_response(raw).iter_lines(1.0))
        self.assertEqual(ctx.exception.kind, "timeout", "超时要单独分类：换号重试只会再撞一次慢上游")

    def test_broken_stream_is_transient(self):
        import http.client

        raw = FakeRaw([], fail=http.client.IncompleteRead(b"partial"))
        with self.assertRaises(upstream.UpstreamError) as ctx:
            list(make_response(raw).iter_lines(1.0))
        self.assertEqual(ctx.exception.kind, "transient")

    def test_connection_close_not_reusable(self):
        pool = FakePool()
        response = make_response(FakeRaw([], headers=[("connection", "close")]), pool)
        response.text()
        self.assertFalse(pool.released[0][1], "上游说关就关，别把死连接放回池子")

    def test_5xx_not_reusable(self):
        pool = FakePool()
        response = make_response(FakeRaw([], status=500), pool)
        response.text()
        self.assertFalse(pool.released[0][1])


class TestConnPool(unittest.TestCase):
    def test_round_trip(self):
        pool = upstream._ConnPool()
        conn = FakeConn()
        self.assertIsNone(pool.acquire("k"))
        pool.release("k", conn)
        self.assertIs(pool.acquire("k"), conn)
        self.assertIsNone(pool.acquire("k"), "取走后就不该还在池里")

    def test_not_reusable_closes(self):
        pool = upstream._ConnPool()
        conn = FakeConn()
        pool.release("k", conn, reusable=False)
        self.assertEqual(conn.closed, 1)
        self.assertIsNone(pool.acquire("k"))

    def test_max_idle(self):
        pool = upstream._ConnPool(max_idle=2)
        conns = [FakeConn() for _ in range(4)]
        for conn in conns:
            pool.release("k", conn)
        self.assertEqual(sum(1 for c in conns if c.closed), 2, "超出上限的连接要关掉，不然句柄泄漏")
        self.assertIsNotNone(pool.acquire("k"))
        self.assertIsNotNone(pool.acquire("k"))
        self.assertIsNone(pool.acquire("k"))

    def test_close_empties(self):
        pool = upstream._ConnPool()
        conn = FakeConn()
        pool.release("k", conn)
        pool.close()
        self.assertEqual(conn.closed, 1)
        self.assertIsNone(pool.acquire("k"))


class TestBaseUrl(unittest.TestCase):
    def test_defaults(self):
        client = upstream.UpstreamClient(FakeCfg())
        self.assertEqual(client.base_url("cn"), "https://copilot.tencent.com")
        self.assertEqual(client.base_url("global"), "https://www.workbuddy.ai")

    def test_configured_realm_keys(self):
        client = upstream.UpstreamClient(FakeCfg({"upstream": {"hosts": {"cn": "https://a.test/", "global": "https://b.test"}}}))
        self.assertEqual(client.base_url("cn"), "https://a.test")
        self.assertEqual(client.base_url("global"), "https://b.test")

    def test_legacy_keys_still_work(self):
        client = upstream.UpstreamClient(FakeCfg({"upstream": {"hosts": {"domestic": "https://old-cn.test", "international": "https://old-g.test"}}}))
        self.assertEqual(client.base_url("cn"), "https://old-cn.test")
        self.assertEqual(client.base_url("global"), "https://old-g.test")

    def test_timeouts(self):
        client = upstream.UpstreamClient(FakeCfg({"upstream": {"timeout_seconds": 30, "idle_timeout_seconds": 90}}))
        # (连接/握手, 流式空闲, 等响应头)；没配 header 时跟连接超时一致
        self.assertEqual(client._timeouts(), (30.0, 90.0, 30.0))

    def test_header_timeout_is_separate(self):
        client = upstream.UpstreamClient(
            FakeCfg({"upstream": {"timeout_seconds": 30, "idle_timeout_seconds": 90, "header_timeout_seconds": 5}})
        )
        self.assertEqual(client._timeouts(), (30.0, 90.0, 5.0))

    def test_header_timeout_garbage_falls_back(self):
        client = upstream.UpstreamClient(
            FakeCfg({"upstream": {"timeout_seconds": 30, "idle_timeout_seconds": 90, "header_timeout_seconds": "soon"}})
        )
        self.assertEqual(client._timeouts(), (30.0, 90.0, 30.0))


class CaptureConn(FakeConn):
    """记下真正发出去的请求体：余额/签到接口用 raw_body 自己序列化，不能被二次编码。"""

    def __init__(self, raw):
        super().__init__()
        self.raw = raw
        self.sent = []

    def request(self, method, path, body=None, headers=None):
        self.sent.append((method, path, body, headers))

    def getresponse(self):
        return self.raw


class TestRequestBody(unittest.TestCase):
    def _client(self, conn):
        client = upstream.UpstreamClient(FakeCfg())
        client._connect = lambda url, proxy, timeout, credential="": (conn, "k")  # type: ignore[assignment]
        return client

    def test_raw_body_passthrough(self):
        conn = CaptureConn(FakeRaw([json.dumps({"code": 0}).encode()]))
        client = self._client(conn)
        resp = client.request("POST", "https://a.test/x", {"k": "v"}, raw_body=b'{"a":1}')
        self.assertEqual(resp.json()["code"], 0)
        self.assertEqual(conn.sent[0][2], b'{"a":1}')

    def test_post_json_still_works(self):
        conn = CaptureConn(FakeRaw([b"{}"]))
        client = self._client(conn)
        client.post("https://a.test/x", {}, {"a": 1})
        self.assertEqual(json.loads(conn.sent[0][2].decode()), {"a": 1})


if __name__ == "__main__":
    unittest.main()
