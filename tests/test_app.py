"""真起一个 HTTP 服务再打进去：这是唯一能证明 chunked/SSE/路由表真能跑通的测试。

前面的用例都直接调 ``Application.dispatch``，绕过了 socket、HTTP/1.1 分块、
CORS 和静态文件；线上最容易坏的反而是那一层。
"""

import http.client
import json
import tempfile
import threading
import unittest

from wb2api import app as app_module
from wb2api import config as config_module
from wb2api.httpserver import serve


class ServerCase(unittest.TestCase):
    """每个用例起一个服务，用完就关（端口给 0，让系统分配）。"""

    panel = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = config_module.Config(None, values=json.loads(json.dumps(config_module.DEFAULTS)))
        cfg.set("data_dir", self.tmp.name)
        cfg.set("panel.enabled", self.panel)
        cfg.set("panel.password", "s3cret-pass")
        cfg.set("api_key", "")
        self.cfg = cfg
        self.app, self.service = app_module.build(cfg)
        self.server = serve(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = None
        head = dict(headers or {})
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            head.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=payload, headers=head)
            response = conn.getresponse()
            data = response.read()
            return response.status, dict((k.lower(), v) for k, v in response.getheaders()), data
        finally:
            conn.close()

    def json_of(self, data):
        return json.loads(data.decode("utf-8"))

    def panel_token(self):
        _status, _headers, data = self.request("POST", "/panel/login", {"password": "s3cret-pass"})
        return self.json_of(data)["token"]


class TestClientSurface(ServerCase):
    def test_healthz_is_open(self):
        status, _headers, data = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(self.json_of(data)["ok"])

    def test_chat_without_accounts_is_503_not_429(self):
        status, _headers, data = self.request(
            "POST", "/v1/chat/completions",
            {"model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 503)
        self.assertEqual(self.json_of(data)["error"]["code"], "no_account",
                         "池空是 503；429 只留给真的限流，否则客户端会疯狂重试")

    def test_missing_model_is_400(self):
        status, _headers, data = self.request("POST", "/v1/chat/completions", {"messages": []})
        self.assertEqual(status, 400)
        self.assertEqual(self.json_of(data)["error"]["type"], "invalid_request_error")

    def test_bad_bearer(self):
        self.cfg.set("api_key", "sk-test")
        status, _headers, data = self.request(
            "POST", "/v1/chat/completions",
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            {"Authorization": "Bearer wrong"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.json_of(data)["error"]["code"], "invalid_api_key")

    def test_cors_headers(self):
        status, headers, _data = self.request("GET", "/healthz", headers={"Origin": "http://localhost:3000"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("access-control-allow-origin"), "*")

    def test_options_preflight(self):
        status, headers, _data = self.request("OPTIONS", "/v1/chat/completions")
        self.assertEqual(status, 204)

    def test_unknown_route_404(self):
        status, _headers, _data = self.request("GET", "/nope")
        self.assertEqual(status, 404)

    def test_wrong_method_405(self):
        status, _headers, _data = self.request("GET", "/v1/chat/completions")
        self.assertEqual(status, 405)

    def test_status_requires_auth_when_key_configured(self):
        self.cfg.set("api_key", "sk-test")
        status, _headers, _data = self.request("GET", "/status")
        self.assertEqual(status, 401, "/status 里有账号数和余额，不能匿名可读")

    def test_status_open_in_local_mode(self):
        # 没配任何密钥就是"本地开放模式"：一个密钥都没有时把 /status 也拦住只会让人没法自检。
        status, _headers, _data = self.request("GET", "/status")
        self.assertEqual(status, 200)


class TestPanelSurface(ServerCase):
    def test_panel_status_is_open_and_masks_password(self):
        status, _headers, data = self.request("GET", "/panel/status")
        self.assertEqual(status, 200)
        payload = self.json_of(data)
        self.assertTrue(payload["enabled"])
        self.assertFalse(payload["is_default_password"])
        self.assertNotIn("s3cret-pass", data.decode("utf-8"))

    def test_login_flow_and_token_header(self):
        status, _headers, data = self.request("POST", "/panel/login", {"password": "wrong"})
        self.assertEqual(status, 401)
        status, headers, data = self.request("POST", "/panel/login", {"password": "s3cret-pass"})
        self.assertEqual(status, 200)
        token = self.json_of(data)["token"]
        self.assertTrue(token)
        self.assertIn("set-cookie", headers, "浏览器面板还依赖 cookie")
        cookie = headers["set-cookie"].split(";")[0]
        self.assertIn("HttpOnly", headers["set-cookie"])
        status, _headers, data = self.request("GET", "/api/accounts", headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(data)["accounts"], [])
        status, _headers, _data = self.request("GET", "/api/accounts", headers={"Cookie": cookie})
        self.assertEqual(status, 200, "cookie 通道必须和头通道等价")
        status, _headers, _data = self.request("GET", "/api/accounts", headers={"Cookie": cookie + "x"})
        self.assertEqual(status, 401, "签名不对的 cookie 不能放行")

    def test_api_requires_token(self):
        status, _headers, _data = self.request("GET", "/api/accounts")
        self.assertEqual(status, 401)

    def test_static_index_served(self):
        status, headers, data = self.request("GET", "/panel/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        self.assertIn(b"<html", data.lower())
        self.assertEqual(headers.get("cache-control"), "no-cache")

    def test_panel_redirect(self):
        status, headers, _data = self.request("GET", "/panel")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("location"), "/panel/")

    def test_no_path_traversal(self):
        for path in ("/panel/..%2f..%2fwb2api%2fconfig.py", "/panel/../../etc/passwd", "/panel/.."):
            status, _headers, _data = self.request("GET", path)
            self.assertIn(status, (400, 404), path)

    def test_settings_never_leak_secrets(self):
        status, _headers, data = self.request("GET", "/panel/status")
        self.assertNotIn(b"s3cret-pass", data)


class TestApiKeySettings(ServerCase):
    """密钥增删：面板只显示打码形态，明文必须在这一条响应里给出一次。"""

    def _post_settings(self, body, token=None):
        headers = {"X-Panel-Token": token or self.panel_token()}
        status, _headers, data = self.request("POST", "/api/settings", body, headers)
        return status, self.json_of(data)

    def test_add_returns_plaintext_once(self):
        status, payload = self._post_settings({"keys_add": [{"name": "cli", "realm": "cn"}]})
        self.assertEqual(status, 200)
        added = payload["added"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["name"], "cli")
        self.assertTrue(added[0]["key"], "新建密钥必须把明文回给客户端一次")

        listed = self.json_of(self.request("GET", "/api/settings", headers={"X-Panel-Token": self.panel_token()})[2])
        self.assertEqual(len(listed["api_keys"]), 1)
        self.assertNotEqual(listed["api_keys"][0]["key"], added[0]["key"], "列表里只能是打码的")

    def test_add_accepts_object_and_bare_name(self):
        for body in ({"keys_add": {"name": "obj"}}, {"keys_add": "bare"}):
            status, payload = self._post_settings(body)
            self.assertEqual(status, 200, body)
            self.assertEqual(len(payload["added"]), 1)
        self.assertEqual(len(self.app.store.list_keys()), 2)

    def test_delete_by_name_removes_the_key(self):
        _status, payload = self._post_settings({"keys_add": [{"name": "cli"}]})
        key = payload["added"][0]["key"]
        status, deleted = self._post_settings({"keys_delete": ["cli"]})
        self.assertEqual(status, 200)
        self.assertEqual(deleted["deleted"], [key])
        self.assertEqual(self.app.store.list_keys(), [])

    def test_delete_by_key_value(self):
        _status, payload = self._post_settings({"keys_add": [{"name": "cli"}]})
        key = payload["added"][0]["key"]
        _status, deleted = self._post_settings({"keys_delete": [key]})
        self.assertEqual(deleted["deleted"], [key])

    def test_delete_unknown_name_is_silent(self):
        status, deleted = self._post_settings({"keys_delete": ["nope"]})
        self.assertEqual(status, 200)
        self.assertEqual(deleted["deleted"], [])


class TestLoginPoll(ServerCase):
    """轮询接口曾经按对象访问属性，而 authflow 返回的是 dict → 面板点"添加账号"直接 500。"""

    def test_pending_payload(self):
        token = self.panel_token()
        self.app.panel.auth.poll_login = lambda state: {"state": state, "status": "pending", "message": "等待中"}
        status, _headers, data = self.request(
            "GET", "/api/accounts/login/poll?state=abc", headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        payload = self.json_of(data)
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["message"], "等待中")

    def test_error_payload_is_passed_through(self):
        token = self.panel_token()
        self.app.panel.auth.poll_login = lambda state: {"status": "error", "message": "登录会话不存在"}
        status, _headers, data = self.request(
            "GET", "/api/accounts/login/poll?state=abc", headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(data)["status"], "error")

    def test_success_attaches_masked_account(self):
        from wb2api.store import Account

        self.service.store.upsert_account(Account(uid="u-poll", realm="global", access_token="secret-at"))
        token = self.panel_token()
        self.app.panel.auth.poll_login = lambda state: {"state": state, "status": "ok", "uid": "u-poll"}
        status, _headers, data = self.request(
            "GET", "/api/accounts/login/poll?state=abc", headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        account = self.json_of(data)["account"]
        self.assertEqual(account["uid"], "u-poll")
        self.assertNotIn("secret-at", data.decode("utf-8"), "轮询结果里绝不能出现明文令牌")

    def test_missing_state_is_400(self):
        token = self.panel_token()
        status, _headers, _data = self.request(
            "GET", "/api/accounts/login/poll", headers={"X-Panel-Token": token})
        self.assertEqual(status, 400)


class TestPanelDisabled(ServerCase):
    panel = False

    def test_panel_paths_do_not_exist(self):
        for path in ("/panel/status", "/panel/", "/api/accounts"):
            status, _headers, _data = self.request("GET", path)
            self.assertEqual(status, 404, path)


class TestBanner(unittest.TestCase):
    def test_banner_warns_on_default_password(self):
        cfg = config_module.Config(None, values=json.loads(json.dumps(config_module.DEFAULTS)))
        cfg.set("panel.password", "admin")
        text = app_module.banner(cfg)
        self.assertIn("默认密码", text)
        self.assertIn("/v1", text)

    def test_banner_quiet_with_custom_password(self):
        cfg = config_module.Config(None, values=json.loads(json.dumps(config_module.DEFAULTS)))
        cfg.set("panel.password", "nope-nope")
        self.assertNotIn("默认密码", app_module.banner(cfg))


if __name__ == "__main__":
    unittest.main()
