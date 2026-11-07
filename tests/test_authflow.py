"""登录/刷新链路的判定结果。

这里盯的不是 HTTP 细节，而是"账号接下来还能不能用"：登录 state 有没有落对域、
pending 有没有被误判成失败、refresh 失败是判死还是留着下次再试。

最要命的一条是**刷新单飞**：refresh token 一次性有效，两个线程同时刷新时第二个
一定带着已消费的 token 上去，上游回 12153 把整条链废掉。所以并发用例是本文件
的主角——用假客户端上的闸门把竞态真的做出来，而不是"看起来像是线程安全的"。

另一个坑是"200 但没有 accessToken"：上游收下了刷新请求却没给令牌，说明这个
refresh token 已经被消费过，继续重试只会一直失败，和 12153 一样必须判死。
"""

import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb2api import authflow, identity
from wb2api.authflow import AuthFlow, LoginError
from wb2api.store import Account, Store
from wb2api.upstream import UpstreamError


def jwt(claims):
    """造一个不验签的 JWT：网关只解 payload 里的 sub/exp。"""
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode("ascii")
    body = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).rstrip(b"=").decode("ascii")
    return f"{header}.{body}."


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
    """按脚本吐响应；真实客户端是 post(url, headers, payload, proxy=...)。

    authflow 只走 ``post`` / ``get`` 两个包装，所以假客户端也实现同样的签名，
    并把每次调用的 method/url/headers/body 记下来给断言用。
    """

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []

    def base_url(self, realm):
        return identity.host_for(realm)

    def _record(self, method, url, headers, body, proxy):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "body": body,
                "proxy": proxy,
            }
        )
        if not self.script:
            raise AssertionError("unexpected upstream call: %s %s" % (method, url))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)

    def post(self, url, headers, payload=None, *, raw_body=None, proxy="", **_kwargs):
        body = raw_body if raw_body is not None else payload
        return self._record("POST", url, headers, body, proxy)

    def get(self, url, headers, proxy="", **_kwargs):
        return self._record("GET", url, headers, None, proxy)

    def request(self, method, url, headers=None, body=None, *, proxy="", **_kwargs):
        return self._record(method, url, headers, body, proxy)

    def close(self):
        pass


class BlockingClient(FakeClient):
    """第一次 POST 先停在闸门上，让第二个线程真的撞上刷新锁。

    如果只是"两个线程先后调用"，第一个早就刷完了，测不出双检查是否有效；
    必须把第一个刷新卡在锁内（响应回来之前），第二个才会走到锁和双重检查上。
    """

    def __init__(self, script):
        super().__init__(script)
        self.entered = threading.Event()
        self.gate = threading.Event()

    def post(self, url, headers, payload=None, *, raw_body=None, proxy="", **_kwargs):
        response = super().post(url, headers, payload, raw_body=raw_body, proxy=proxy)
        self.entered.set()
        self.gate.wait(5.0)
        return response


def refresh_ok(access, refresh="rt-2", expires_in=3600):
    return {"code": 0, "msg": "ok", "data": {"accessToken": access, "refreshToken": refresh,
                                             "expiresIn": expires_in}}


class AuthFlowBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wb2a-auth-")
        self.addCleanup(self._tmp.cleanup)
        # host_for 可以被环境变量整体改写；测试要的是内置的分域域名。
        saved = os.environ.pop("WB_UPSTREAM_HOST", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "WB_UPSTREAM_HOST", saved)
        self.cfg = FakeCfg({"cooldown": {"auth_dead": "24h"}})
        self.store = Store(self._tmp.name)

    def flow(self, script=()):
        client = FakeClient(script)
        return AuthFlow(client, self.store, self.cfg), client

    def account(self, uid="u1", realm="cn", expires_in=3600.0, refresh="rt-1", access="at-old"):
        return Account(uid=uid, id=uid, realm=realm, access_token=access,
                       refresh_token=refresh, expires_at=time.time() + expires_in)


class LoginTest(AuthFlowBase):
    def test_start_login_posts_to_state_endpoint(self):
        auth, client = self.flow([{"code": 0, "data": {"state": "st-1", "authUrl": "https://login.test/x"}}])
        session = auth.start_login("cn")
        self.assertEqual(session.state, "st-1")
        self.assertEqual(session.url, "https://login.test/x")
        self.assertEqual(session.status, "pending")
        call = client.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://copilot.tencent.com/v2/plugin/auth/state?platform=CLI",
                         "state 端点必须带 platform=CLI，否则上游会给网页版的流程")
        self.assertEqual(call["body"], {})

    def test_start_login_global_uses_global_host(self):
        auth, client = self.flow([{"code": 0, "data": {"state": "st-2", "authUrl": "https://login.test/y"}}])
        session = auth.start_login("global")
        self.assertEqual(session.realm, "global")
        self.assertTrue(client.calls[0]["url"].startswith("https://www.workbuddy.ai"),
                        "global 域走 www.workbuddy.ai，不能拿国内域名去登录")
        self.assertEqual(client.calls[0]["headers"]["Accept-Language"], "en-US")

    def test_start_login_alias_normalized(self):
        auth, client = self.flow([{"code": 0, "data": {"state": "s", "authUrl": "u"}}])
        self.assertEqual(auth.start_login("intl").realm, "global")
        self.assertTrue(client.calls[0]["url"].startswith("https://www.workbuddy.ai"))

    def test_start_login_rejects_error_and_empty_payload(self):
        auth, _client = self.flow([{"code": 403, "msg": "region not allowed"}])
        with self.assertRaises(LoginError) as ctx:
            auth.start_login("cn")
        self.assertIn("region not allowed", str(ctx.exception))

        auth, _client = self.flow([{"code": 0, "data": {"state": "only-state"}}])
        with self.assertRaises(LoginError):
            auth.start_login("cn")
        self.assertEqual(auth._sessions, {}, "失败的 state 不能留在会话表里")

    def test_start_login_network_error_is_login_error(self):
        auth, _client = self.flow([UpstreamError("boom", kind="transient", status=502)])
        with self.assertRaises(LoginError):
            auth.start_login("cn")

    def test_poll_pending_is_not_an_error(self):
        auth, client = self.flow([
            {"code": 0, "data": {"state": "st-1", "authUrl": "https://login.test/x"}},
            {"code": authflow.LOGIN_PENDING, "msg": "等待浏览器完成登录"},
        ])
        auth.start_login("cn")
        view = auth.poll_login("st-1")
        self.assertEqual(view["status"], "pending", "11217 是'还没扫码'，不能当成失败把会话判死")
        self.assertIn("浏览器", view["message"])
        self.assertIn("/v2/plugin/auth/token?state=st-1", client.calls[1]["url"])

    def test_pending_then_success_in_same_session(self):
        access = jwt({"sub": "u-42", "exp": time.time() + 3600})
        auth, client = self.flow([
            {"code": 0, "data": {"state": "st-1", "authUrl": "https://login.test/x", "stateToken": "s"}},
            {"code": authflow.LOGIN_PENDING, "msg": "等待中"},
            {"code": 0, "data": {"accessToken": access, "refreshToken": "rt-42", "state": "s"}},
            {"code": 0, "data": {"nickname": "小明", "email": "a@b.c", "enterpriseId": "ent-1"}},
        ])
        auth.start_login("cn")
        self.assertEqual(auth.poll_login("st-1")["status"], "pending")
        view = auth.poll_login("st-1")
        self.assertEqual(view["status"], "ok")
        self.assertEqual(view["uid"], "u-42")
        self.assertEqual(view["message"], "登录成功")

        stored = self.store.get_account("u-42")
        self.assertIsNotNone(stored, "登录成功必须落盘，否则重启就白登了")
        self.assertEqual(stored.access_token, access)
        self.assertEqual(stored.refresh_token, "rt-42")
        self.assertAlmostEqual(stored.expires_at, time.time() + 3600, delta=8)
        self.assertEqual(stored.nickname, "小明")
        self.assertEqual(stored.email, "a@b.c")
        self.assertEqual(stored.enterprise_id, "ent-1")
        self.assertEqual(stored.source, "oauth")
        self.assertEqual(stored.state, "ready")
        self.assertIn("/v2/plugin/login/account?state=s", client.calls[3]["url"])

    def test_poll_ok_view_is_idempotent(self):
        access = jwt({"sub": "u-7", "exp": time.time() + 3600})
        auth, client = self.flow([
            {"code": 0, "data": {"state": "st", "authUrl": "u"}},
            {"code": 0, "data": {"accessToken": access, "refreshToken": "rt"}},
            {"code": 0, "data": {}},
        ])
        auth.start_login("cn")
        auth.poll_login("st")
        calls = len(client.calls)
        view = auth.poll_login("st")
        self.assertEqual(view["status"], "ok")
        self.assertEqual(len(client.calls), calls, "已经成功的会话再轮询不该再打上游")

    def test_poll_unknown_state(self):
        auth, _client = self.flow()
        view = auth.poll_login("gone")
        self.assertEqual(view["status"], "error")
        self.assertIn("重新发起", view["message"])

    def test_poll_expired_session(self):
        auth, client = self.flow([
            {"code": 0, "data": {"state": "st", "authUrl": "u"}},
        ])
        session = auth.start_login("cn")
        session.created_at -= authflow.STATE_TTL + 1
        view = auth.poll_login("st")
        self.assertEqual(view["status"], "error")
        self.assertIn("超时", view["message"])
        self.assertEqual(len(client.calls), 1, "过期会话不该再打上游")

    def test_poll_network_error_stays_pending(self):
        auth, _client = self.flow([
            {"code": 0, "data": {"state": "st", "authUrl": "u"}},
            UpstreamError("timeout", kind="transient", status=0),
        ])
        auth.start_login("cn")
        view = auth.poll_login("st")
        self.assertEqual(view["status"], "pending", "轮询时网络抖动是常态，不能让用户重来")

    def test_static_key_flagged(self):
        auth, _client = self.flow([
            {"code": 0, "data": {"state": "st", "authUrl": "u"}},
            {"code": 0, "data": {"accessToken": "ck_abc123", "uid": "u-9"}},
            {"code": 0, "data": {}},
        ])
        auth.start_login("cn")
        auth.poll_login("st")
        stored = self.store.get_account("u-9")
        self.assertTrue(stored.static_key, "ck_/pt_ 是静态密钥：不可刷新、永不过期")
        self.assertEqual(stored.nickname, "u-9"[:8], "拿不到昵称就用 uid 前缀，不能空着")

    def test_expires_in_5184000_honoured(self):
        auth, _client = self.flow([
            {"code": 0, "data": {"state": "st", "authUrl": "u"}},
            {"code": 0, "data": {"accessToken": "at-opaque", "refreshToken": "rt", "uid": "u-1",
                                 "expiresIn": 5184000}},
            {"code": 0, "data": {}},
        ])
        auth.start_login("cn")
        auth.poll_login("st")
        self.assertAlmostEqual(self.store.get_account("u-1").expires_at, time.time() + 5184000, delta=10)

    def test_absurd_expires_in_not_trusted(self):
        auth, _client = self.flow([
            {"code": 0, "data": {"state": "st", "authUrl": "u"}},
            {"code": 0, "data": {"accessToken": "at-opaque", "refreshToken": "rt", "uid": "u-1",
                                 "expiresIn": 10 * 365 * 86400}},
            {"code": 0, "data": {}},
        ])
        auth.start_login("cn")
        auth.poll_login("st")
        # 上游偶发回"十年"，采信它等于永不过期、也永远不会保活。
        self.assertEqual(self.store.get_account("u-1").expires_at, 0.0,
                         "超过 400 天的 expiresIn 只能当成'未知'")

    def test_expiry_prefers_token_claims(self):
        exp = time.time() + 7200
        auth, _client = self.flow([
            {"code": 0, "data": {"state": "st", "authUrl": "u"}},
            {"code": 0, "data": {"accessToken": jwt({"sub": "u-2", "exp": exp}), "refreshToken": "rt",
                                 "expiresIn": 60}},
            {"code": 0, "data": {}},
        ])
        auth.start_login("cn")
        auth.poll_login("st")
        self.assertAlmostEqual(self.store.get_account("u-2").expires_at, exp, delta=2,
                               msg="JWT 里的 exp 比 expiresIn 可信（后者是相对值，容易被算错）")

    def test_token_without_uid_is_rejected(self):
        auth, _client = self.flow([
            {"code": 0, "data": {"state": "st", "authUrl": "u"}},
            {"code": 0, "data": {"accessToken": "at-opaque", "refreshToken": "rt"}},
        ])
        auth.start_login("cn")
        view = auth.poll_login("st")
        self.assertEqual(view["status"], "error")
        self.assertIn("uid", view["message"])
        self.assertEqual(self.store.list_accounts(), [], "没有 uid 的账号不能入库")

    def test_login_uid_from_tokens_when_jwt_has_no_sub(self):
        # 上游换过令牌格式：claims 里没有 sub/uid/userId 时，退回响应体里的 uid。
        access = jwt({"exp": time.time() + 3600})
        auth, _client = self.flow([
            {"code": 0, "data": {"state": "st", "authUrl": "u"}},
            {"code": 0, "data": {"accessToken": access, "refreshToken": "rt",
                                 "uid": "u-9", "state": "s"}},
            {"code": 0, "data": {}},
        ])
        auth.start_login("cn")
        view = auth.poll_login("st")
        self.assertEqual(view["status"], "ok")
        self.assertEqual(view["uid"], "u-9")
        self.assertIsNotNone(self.store.get_account("u-9"))

    def test_cancel_login(self):
        auth, _client = self.flow([{"code": 0, "data": {"state": "st", "authUrl": "u"}}])
        auth.start_login("cn")
        self.assertTrue(auth.cancel_login("st"))
        self.assertFalse(auth.cancel_login("st"))


class EnsureTokenTest(AuthFlowBase):
    def test_fresh_token_does_not_refresh(self):
        auth, client = self.flow([])
        account = self.account(expires_in=86400)
        self.assertTrue(auth.ensure_token(account))
        self.assertEqual(client.calls, [], "离过期还早就不该刷，刷新令牌是消耗品")

    def test_near_expiry_refreshes(self):
        new = jwt({"exp": time.time() + 3600})
        auth, client = self.flow([refresh_ok(new)])
        account = self.account(expires_in=60)  # 在 180s 的安全窗口内
        self.assertTrue(auth.ensure_token(account))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(account.access_token, new)

    def test_expired_refreshes_and_rotates(self):
        new = jwt({"exp": time.time() + 3600})
        auth, client = self.flow([refresh_ok(new, refresh="rt-2", expires_in=7200)])
        account = self.account(expires_in=-10)
        account.failures = 3
        account.last_error = "old error"
        account.cooldown_until = time.time() + 100
        account.cooldown_level = "soft"
        account.state = "auth_dead"
        self.assertTrue(auth.refresh(account))
        self.assertEqual(account.access_token, new)
        self.assertEqual(account.refresh_token, "rt-2", "响应里带了新的 refresh token 就必须换掉")
        self.assertAlmostEqual(account.expires_at, time.time() + 3600, delta=8)
        self.assertEqual(account.failures, 0)
        self.assertEqual(account.last_error, "")
        self.assertEqual(account.cooldown_until, 0)
        self.assertEqual(account.state, "ready", "刷新成功要把 auth_dead 拉回来")
        self.assertEqual(self.store.get_account("u1").access_token, new)

        call = client.calls[0]
        self.assertEqual(call["url"], "https://copilot.tencent.com/v2/plugin/auth/token/refresh")
        self.assertEqual(call["headers"]["X-Refresh-Token"], "rt-1",
                         "refresh token 只能放在头里，不能进 body/query")
        self.assertEqual(call["body"], {})
        self.assertEqual(call["headers"]["X-Auth-Refresh-Source"], "workbuddy")

    def test_global_refresh_uses_plugin_source(self):
        new = jwt({"exp": time.time() + 3600})
        auth, client = self.flow([refresh_ok(new)])
        account = self.account(realm="global", expires_in=-10)
        auth.refresh(account)
        call = client.calls[0]
        self.assertTrue(call["url"].startswith("https://www.workbuddy.ai"))
        self.assertEqual(call["headers"]["X-Auth-Refresh-Source"], "plugin",
                         "国际版客户端的刷新来源是 plugin，国内是 workbuddy")

    def test_static_key_never_refreshes(self):
        auth, client = self.flow([])
        account = self.account(access="ck_static", expires_in=-99999)
        account.static_key = True
        self.assertTrue(auth.ensure_token(account))
        self.assertEqual(client.calls, [])

    def test_no_refresh_token_marks_dead(self):
        auth, client = self.flow([])
        account = self.account(expires_in=-10, refresh="")
        self.assertFalse(auth.refresh(account))
        self.assertEqual(client.calls, [])
        self.assertEqual(account.state, "auth_dead")
        self.assertEqual(account.cooldown_level, "auth")

    def test_far_future_token_with_force_still_refreshes(self):
        # 手动"立即刷新"必须真的打上游（force 语义），不能被提前返回吞掉。
        new = jwt({"exp": time.time() + 3600})
        auth, client = self.flow([refresh_ok(new)])
        account = self.account(expires_in=86400)
        self.assertTrue(auth.refresh(account, force=True))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(account.access_token, new)

    def test_double_envelope_refresh(self):
        # 上游偶发把令牌再套一层：{"data": {"data": {...}}}，不认就会判成"没给令牌"。
        new = jwt({"exp": time.time() + 3600})
        payload = {"code": 0, "data": {"data": {"accessToken": new, "refreshToken": "rt-2"}}}
        auth, _client = self.flow([payload])
        account = self.account(expires_in=-10)
        self.assertTrue(auth.refresh(account))
        self.assertEqual(account.access_token, new)
        self.assertEqual(account.refresh_token, "rt-2")


class SingleFlightTest(AuthFlowBase):
    """并发刷新只能有一个真的打到上游——刷新令牌一次性有效。"""

    def _run_pair(self, kind):
        """起两个线程，用假客户端的闸门把"第一个已在刷、第二个刚到"钉住。"""
        client = BlockingClient([refresh_ok(jwt({"exp": time.time() + 3600}))])
        auth = AuthFlow(client, self.store, self.cfg)
        if kind == "ensure":
            account = self.account(expires_in=-30)  # 已过期：ensure_token 才会去刷
        else:
            account = self.account(expires_in=86400)  # 手动强制刷新：令牌本来还有效
        self.store.upsert_account(account)
        results = []
        errors = []

        def call():
            try:
                if kind == "ensure":
                    results.append(auth.ensure_token(account))
                else:
                    results.append(auth.refresh(account, force=True))
            except Exception as exc:  # pragma: no cover - 失败时给断言更清楚的上下文
                errors.append(exc)

        first = threading.Thread(target=call, name="refresh-a")
        first.start()
        self.assertTrue(client.entered.wait(5.0), "第一个刷新没有进入上游调用")
        second = threading.Thread(target=call, name="refresh-b")
        second.start()
        # 第二个线程会停在刷新锁上（打不到假客户端），给它足够时间走到那里。
        time.sleep(0.3)
        client.gate.set()
        first.join(5.0)
        second.join(5.0)
        self.assertEqual(errors, [])
        self.assertFalse(first.is_alive() or second.is_alive(), "刷新线程超时未结束")
        return client, results

    def test_concurrent_refresh_forced_is_single_flight(self):
        client, results = self._run_pair("force")
        self.assertEqual(len(client.calls), 1,
                         "两个并发 refresh 只能打一次上游：第二次会带着已消费的 refresh token → 12153")
        self.assertEqual(results, [True, True])

    def test_concurrent_ensure_token_is_single_flight(self):
        client, results = self._run_pair("ensure")
        self.assertEqual(len(client.calls), 1,
                         "并发 ensure_token 同样只能刷一次；否则第二次必被判死")
        self.assertEqual(results, [True, True])


class DeadMarkerTest(AuthFlowBase):
    def _refresh(self, payload):
        auth, client = self.flow([payload])
        account = self.account(expires_in=-10)
        ok = auth.refresh(account)
        return ok, account, self.store.get_account("u1"), client

    def test_code_12153_is_dead(self):
        ok, account, stored, _client = self._refresh({"code": 12153, "msg": "refresh token consumed"})
        self.assertFalse(ok)
        self.assertEqual(stored.state, "auth_dead", "12153 = 刷新令牌已被消费，重试没有意义")
        self.assertEqual(stored.cooldown_level, "auth")
        self.assertGreater(stored.cooldown_until, time.time() + 3600)
        self.assertIn("12153", stored.last_error)

    def test_offline_session_message_is_dead(self):
        ok, _account, stored, _client = self._refresh(
            {"code": 400, "msg": "Offline user session not found"})
        self.assertFalse(ok)
        self.assertEqual(stored.state, "auth_dead")

    def test_empty_access_token_on_200_is_dead(self):
        # 上游 200 + code 0 却没给令牌 = 刷新令牌已经被消费过。
        # 当成 transient 会一直重试同一个一次性 token，每次都白打一次上游。
        ok, _account, stored, _client = self._refresh({"code": 0, "msg": "", "data": {}})
        self.assertFalse(ok)
        self.assertEqual(stored.state, "auth_dead")

    def test_transient_5xx_is_not_dead(self):
        auth, _client = self.flow([UpstreamError("bad gateway", kind="transient", status=502)])
        account = self.account(expires_in=-10)
        self.assertFalse(auth.refresh(account))
        stored = self.store.get_account("u1")
        self.assertEqual(stored.state, "ready", "5xx 是上游抖动，账号要留着下次再刷")
        self.assertEqual(stored.failures, 1)
        self.assertEqual(stored.cooldown_until, 0)
        self.assertIn("刷新失败", stored.last_error)

    def test_transient_payload_is_not_dead(self):
        auth, _client = self.flow([{"code": 500, "msg": "internal error"}])
        account = self.account(expires_in=-10)
        self.assertFalse(auth.refresh(account))
        stored = self.store.get_account("u1")
        self.assertEqual(stored.state, "ready")
        self.assertEqual(stored.failures, 1)

    def test_classify_refresh_failure(self):
        self.assertEqual(authflow.classify_refresh_failure({"code": 12153}), "dead")
        self.assertEqual(authflow.classify_refresh_failure(
            {"code": 400, "msg": "Offline user session not found"}), "dead")
        self.assertEqual(authflow.classify_refresh_failure({"code": 502, "msg": "bad gateway"}), "transient")
        self.assertEqual(authflow.classify_refresh_failure({}), "transient")


if __name__ == "__main__":
    unittest.main(verbosity=2)
