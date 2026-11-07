"""`wb2a login`：没有浏览器的那台机器也要能加账号。

两条路径都要测：
- 面板在跑 → 登录必须**交给面板**（同一份 accounts.json 只能有一个写者，
  CLI 自己落盘会被面板下一次保存覆盖掉）；
- 面板没跑 → 自己起 auth 流程，链接照样打得出来。
"""

import argparse
import contextlib
import io
import json
import unittest

from wb2api import cli

from tests.test_app import ServerCase


def login_args(**extra):
    base = dict(realm="cn", proxy="", timeout=5.0, no_wait=True, json=False)
    base.update(extra)
    return argparse.Namespace(**base)


class PanelDownTest(unittest.TestCase):
    def test_panel_call_returns_none_when_nothing_listens(self):
        # 端口 1 一定没人监听：调用方据此回落到本地流程，不该抛异常。
        self.assertIsNone(cli._panel_call("http://127.0.0.1:1", "GET", "/panel/status", timeout=1.0))

    def test_local_flow_still_prints_a_link(self):
        # 面板不可达时走本地流程。这里把 _service 换成一个只会发链接的假服务，
        # 免得测试变成"能不能连上上游"的探测——那不是这个用例要证明的事。
        import tempfile
        import types
        from unittest import mock

        from wb2api.config import Config

        fake = types.SimpleNamespace(
            auth=types.SimpleNamespace(
                start_login=lambda realm="cn", proxy="": types.SimpleNamespace(
                    state="st-local", url="https://example.test/login?state=st-local"
                )
            ),
            close=lambda: None,
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config(None, {"data_dir": tmp.name, "listen": "127.0.0.1:1", "panel": {"enabled": False}})
        out = io.StringIO()
        with mock.patch.object(cli, "_service", lambda cfg_: fake):
            with contextlib.redirect_stdout(out):
                code = cli._login(cfg, login_args())
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("链接：https://example.test/login", text)
        self.assertIn("st-local", text)


class PanelOwnsTheLoginTest(ServerCase):
    """面板在跑时，state 必须出现在面板的会话表里——那才叫"交给它"。"""

    def setUp(self):
        super().setUp()
        self.cfg.set("listen", "127.0.0.1:%d" % self.port)
        # 别去碰真上游：这里考的是"登录交给面板"，不是 auth/state 能不能通。
        from wb2api.authflow import LoginSession

        def fake_start(realm="cn", proxy=""):
            session = LoginSession(state="st-test", realm=realm,
                                   url="https://example.test/login?platform=CLI&state=st-test")
            # 真实 start_login 会把会话登记进表，面板的轮询才认得这个 state。
            self.app.panel.auth._sessions[session.state] = session
            return session

        self.app.panel.auth.start_login = fake_start

    def test_link_is_issued_by_the_panel(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli._login(self.cfg, login_args(realm="global"))
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("https://example.test/login", out.getvalue())

        token = self.panel_token()
        started = cli._panel_call(
            cli._panel_base(self.cfg), "GET", "/api/accounts/login/poll?state=st-test", token=token
        )
        self.assertEqual(started.get("status"), "pending",
                         "面板应当认得自己发出的 state，而不是回 404/error")
        self.assertEqual(started.get("realm"), "global")

    def test_json_mode_is_machine_readable(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli._login(self.cfg, login_args(json=True))
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(payload["realm"], "cn")
        self.assertEqual(payload["state"], "st-test")
        self.assertTrue(payload["url"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
