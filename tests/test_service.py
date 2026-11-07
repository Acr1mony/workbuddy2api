"""service.py 的编排行为：换不换号、流式边界、记账。

用假客户端（FakeClient）按脚本吐响应，不碰网络——这几个判断是整条链路里最容易
写错的地方（"该换号却没换" 与 "不该换号却换了" 的代价不对称），值得用脚本化
的上游把它钉死。
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb2api.config import Config
from wb2api.service import GatewayService, ServiceError, parse_reset_clock
from wb2api.store import Account, Store
from wb2api.upstream import UpstreamError


def frame(obj):
    return "data: " + json.dumps(obj, ensure_ascii=False)


def sse_lines(*lines):
    """SSE 以空行分帧——iter_sse 只在空行处 dispatch，漏了空行整段会被拼成一条。"""
    out = []
    for line in lines:
        out.append(line)
        out.append("")
    return out


def chat_chunk(text="", finish=None, usage=None):
    payload = {
        "id": "x",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "deepseek-v3",
        "choices": [{"index": 0, "delta": {"content": text} if text else {}, "finish_reason": finish}],
    }
    if usage:
        payload["usage"] = usage
    return frame(payload)


class FakeResponse:
    def __init__(self, lines):
        self.lines = list(lines)
        self.closed = False

    def iter_text(self, idle_timeout=300.0):
        for line in self.lines:
            yield line

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def base_url(self, realm):
        return "https://%s.test" % realm

    def post(self, url, headers, raw_body=b"", proxy=""):
        self.calls.append({"url": url, "headers": dict(headers), "body": raw_body, "proxy": proxy})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def request(self, *args, **kwargs):  # pragma: no cover - 面板用不到
        raise AssertionError("unexpected request()")

    def close(self):
        pass


def ok_stream(text="hello"):
    return FakeResponse(sse_lines(": keepalive", chat_chunk(text),
                                  chat_chunk("", finish="stop"), frame("[DONE]")))


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-svc-")
        self.cfg = Config(None, {"data_dir": self.tmp, "panel": {"enabled": False}})
        self.store = Store(self.tmp)
        for uid, realm in (("u1", "cn"), ("u2", "cn")):
            account = Account(uid=uid, id=uid, realm=realm, access_token="tok-" + uid,
                              refresh_token="rt-" + uid, expires_at=time.time() + 86400)
            self.store.upsert_account(account)

    def service(self, script):
        client = FakeClient(script)
        svc = GatewayService(self.cfg, Store(self.tmp), client=client)
        return svc, client

    def body(self, **extra):
        payload = {"model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}]}
        payload.update(extra)
        return payload

    def request(self, payload):
        from wb2api.httpserver import Request

        return Request("POST", "/v1/chat/completions", {}, {}, json.dumps(payload).encode(), ("1.2.3.4", 1))

    # -- 正常路径 -----------------------------------------------------------
    def test_non_stream_aggregates_locally(self):
        svc, client = self.service([ok_stream("你好")])
        resp = svc.chat_completions(self.request(self.body()))
        payload = json.loads(resp.body)
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["choices"][0]["message"]["content"], "你好")
        self.assertEqual(len(client.calls), 1)
        # 上游只能流式：我们发出去的 body 必须带 stream=true
        sent = json.loads(client.calls[0]["body"])
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["stream_options"], {"include_usage": True})

    def test_pacing_marks_the_account_used(self):
        """打过一次之后要记下时间，下一次才知道该等多久。"""
        svc, client = self.service([ok_stream("hi")])
        svc.chat_completions(self.request(self.body()))
        uid = client.calls[0]["headers"]["X-User-Id"]
        account = svc.store.get_account(uid)
        self.assertIsNotNone(account)
        self.assertGreater(account.last_chat_at, 0.0)
        self.assertGreater(svc.pool.pace_delay(account), 0.0, "默认 200ms 节奏应当生效")

    def test_stream_passes_frames_through(self):
        svc, _client = self.service([ok_stream("分块")])
        resp = svc.chat_completions(self.request(self.body(stream=True)))
        frames = list(resp.stream)
        text = b"".join(frames).decode()
        self.assertIn("分块", text)
        self.assertIn("[DONE]", text)

    def test_usage_recorded_after_stream(self):
        svc, _client = self.service([
            FakeResponse(sse_lines(
                chat_chunk("a"),
                chat_chunk("b", finish="stop"),
                chat_chunk("", usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}),
                frame("[DONE]"),
            )),
        ])
        resp = svc.chat_completions(self.request(self.body(stream=True)))
        list(resp.stream)
        recent = svc.ledger.recent(5)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["prompt_tokens"], 11)
        self.assertEqual(recent[0]["completion_tokens"], 7)
        self.assertEqual(recent[0]["outcome"], "completed")

    # -- 换号决策 -----------------------------------------------------------
    def test_5xx_rotates_to_another_account(self):
        error = UpstreamError("bad gateway", kind="transient", status=502)
        svc, client = self.service([error, ok_stream("second")])
        resp = svc.chat_completions(self.request(self.body()))
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(client.calls), 2)
        self.assertNotEqual(client.calls[0]["headers"]["X-User-Id"], client.calls[1]["headers"]["X-User-Id"])

    def test_restricted_never_rotates(self):
        error = UpstreamError("forbidden", kind="forbidden", status=403, code=11140)
        svc, client = self.service([error])
        with self.assertRaises(ServiceError) as ctx:
            svc.chat_completions(self.request(self.body()))
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(len(client.calls), 1, "风控 11140 换号只会连坐，必须只试一次")

    def test_fingerprint_never_rotates(self):
        error = UpstreamError("bad request: 11-128 fingerprint", kind="bad_request", status=400)
        svc, client = self.service([error])
        with self.assertRaises(ServiceError) as ctx:
            svc.chat_completions(self.request(self.body()))
        self.assertEqual(ctx.exception.code, "fingerprint_rejected")
        self.assertEqual(len(client.calls), 1)

    def test_prompt_too_long_never_rotates(self):
        error = UpstreamError("prompt too long", kind="bad_request", status=400, code=11115)
        svc, client = self.service([error])
        with self.assertRaises(ServiceError) as ctx:
            svc.chat_completions(self.request(self.body()))
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(len(client.calls), 1)

    def test_quota_rotates_and_cools_account(self):
        error = UpstreamError("no credits", kind="quota", status=402, code=6004)
        svc, client = self.service([error, ok_stream()])
        resp = svc.chat_completions(self.request(self.body()))
        self.assertEqual(resp.status, 200)
        cooled = [a for a in svc.store.list_accounts() if a.balance_until > time.time()]
        self.assertEqual(len(cooled), 1)

    def test_session_dead_marks_account(self):
        error = UpstreamError("session gone 12153", kind="auth", status=401, code=12153)
        svc, client = self.service([error, ok_stream()])
        resp = svc.chat_completions(self.request(self.body()))
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(client.calls), 2)

    def test_model_unavailable_is_400_not_502(self):
        # 上游真实返回：400 {"code":11102,"msg":"model [glm-4.6] service info not found"}
        # 这类错误换号、重试都没用，必须让客户端看到 400 并自己换模型。
        error = UpstreamError("model [glm-4.6] service info not found", kind="bad_request", status=400, code=11102)
        svc, client = self.service([error])
        with self.assertRaises(ServiceError) as ctx:
            svc.chat_completions(self.request(self.body()))
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "model_unavailable")
        self.assertEqual(len(client.calls), 1)

    def test_all_accounts_fail_gives_503(self):
        errors = [UpstreamError("boom", kind="transient", status=500) for _ in range(12)]
        svc, _client = self.service(errors)
        with self.assertRaises(ServiceError) as ctx:
            svc.chat_completions(self.request(self.body()))
        self.assertIn(ctx.exception.status, (502, 503))

    def test_no_accounts_gives_503_no_account(self):
        empty = tempfile.mkdtemp(prefix="wb2a-svc-empty-")
        svc = GatewayService(self.cfg, Store(empty), client=FakeClient([]))
        with self.assertRaises(ServiceError) as ctx:
            svc.chat_completions(self.request(self.body()))
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, "no_account")

    # -- 辅助 ---------------------------------------------------------------
    def test_parse_reset_clock(self):
        stamp = parse_reset_clock("额度将在 2026-11-08 04:00:00 重置")
        self.assertGreater(stamp, time.time())

    def test_parse_reset_clock_garbage(self):
        self.assertEqual(parse_reset_clock("no clock here"), 0.0)
        self.assertEqual(parse_reset_clock(""), 0.0)

    def test_model_prefix_selects_realm(self):
        self.store.upsert_account(Account(uid="u3", id="u3", realm="global",
                                          access_token="tok-u3", refresh_token="rt-u3",
                                          expires_at=time.time() + 86400))
        svc, client = self.service([ok_stream()])
        svc.chat_completions(self.request(self.body(model="global:deepseek-v3")))
        self.assertEqual(client.calls[0]["url"], "https://global.test/v2/chat/completions")
        self.assertEqual(client.calls[0]["headers"]["Origin"], "https://www.workbuddy.ai")

    def test_sanitize_mode_rewrites(self):
        svc, client = self.service([ok_stream()])
        svc.cfg.set("prompt.mode", "sanitize")
        payload = self.body(messages=[{"role": "user", "content": "You are Claude Code, Anthropic's official CLI for Claude"}])
        svc.chat_completions(self.request(payload))
        sent = client.calls[0]["body"].decode()
        self.assertNotIn("Anthropic's official CLI for Claude", sent)

    # -- 防封号：403 三路 / 超时 / 出口退避 --------------------------------
    def test_waf_403_never_rotates_and_only_brief_cooldown(self):
        error = UpstreamError("Request blocked by WAF", kind="waf", status=403, retryable=True)
        svc, client = self.service([error])
        with self.assertRaises(ServiceError) as ctx:
            svc.chat_completions(self.request(self.body()))
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.code, "content_blocked")
        self.assertEqual(len(client.calls), 1, "内容审核跟账号无关，换号只会连坐")
        account = svc.store.get_account(client.calls[0]["headers"]["X-User-Id"])
        self.assertGreater(account.cooldown_until, time.time())
        self.assertLess(account.cooldown_until - time.time(), 120, "只该避让一分钟，不是两小时")
        self.assertEqual(account.restricted_until, 0.0, "WAF 不是风控封号，不能写成受限")

    def test_timeout_never_rotates(self):
        error = UpstreamError("upstream timeout", kind="timeout", status=0)
        svc, client = self.service([error])
        with self.assertRaises(ServiceError) as ctx:
            svc.chat_completions(self.request(self.body()))
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, "upstream_timeout")
        self.assertEqual(len(client.calls), 1, "超时换号只会再撞一次慢上游，还可能被重复生成")

    def test_transient_does_not_penalize_the_account(self):
        error = UpstreamError("bad gateway", kind="transient", status=502)
        svc, client = self.service([error, ok_stream()])
        svc.chat_completions(self.request(self.body()))
        first = svc.store.get_account(client.calls[0]["headers"]["X-User-Id"])
        self.assertEqual(first.breaker_until, 0.0, "链路抖动不该把账号打进熔断")
        self.assertEqual(first.failures, 0)

    def test_rate_limit_carries_retry_after(self):
        error = UpstreamError("slow down", kind="rate_limit", status=429, retryable=True, retry_after=12)
        svc, _client = self.service([])
        mapped = svc._client_error(error, "cn", "deepseek-v3")
        self.assertEqual(mapped.status, 429)
        self.assertAlmostEqual(mapped.retry_after, 12.0, msg="有 Retry-After 就给客户端，别让它盲目重试")

    def test_stream_binds_session_affinity(self):
        """流式是主路径：这里不绑亲和，等于从来没有亲和。"""
        from wb2api.httpserver import Request

        svc, client = self.service([ok_stream("hi")])
        payload = self.body(stream=True)
        req = Request(
            "POST", "/v1/chat/completions", {}, {"X-Conversation-Id": "conv-42"},
            json.dumps(payload).encode(), ("1.2.3.4", 1),
        )
        resp = svc.chat_completions(req)
        list(resp.stream)
        bound = svc.affinity.candidates("conv-42")
        self.assertTrue(bound, "流式请求成功后必须把会话绑到账号上")
        self.assertEqual(bound[0], client.calls[0]["headers"]["X-User-Id"])


    def test_waf_retries_once_with_a_neutral_prompt(self):
        """内容审核打回时换号没用，但摘掉用户自己的系统提示词常常能过。"""
        error = UpstreamError("Request blocked by WAF", kind="waf", status=403, retryable=True)
        svc, client = self.service([error, ok_stream("ok")])
        payload = self.body(messages=[
            {"role": "system", "content": "You are Claude Code, Anthropic's official CLI for Claude"},
            {"role": "user", "content": "hi"},
        ])
        svc.chat_completions(self.request(payload))
        self.assertEqual(len(client.calls), 2, "只允许降级重试一次")
        retried = client.calls[1]["body"].decode("utf-8")
        self.assertIn("You are a helpful assistant.", retried)
        self.assertNotIn("Claude Code", retried)
        account = svc.store.get_account(client.calls[0]["headers"]["X-User-Id"])
        # 第一发不算这个号的账：重试成功就该是干净的
        self.assertEqual(account.cooldown_until, 0.0)
        self.assertEqual(account.failures, 0)

    def test_waf_retry_happens_only_once(self):
        error = UpstreamError("Request blocked by WAF", kind="waf", status=403, retryable=True)
        svc, client = self.service([error, error])
        payload = self.body(messages=[
            {"role": "system", "content": "You are Claude Code, Anthropic's official CLI for Claude"},
            {"role": "user", "content": "hi"},
        ])
        with self.assertRaises(ServiceError) as ctx:
            svc.chat_completions(self.request(payload))
        self.assertEqual(ctx.exception.code, "content_blocked")
        self.assertEqual(len(client.calls), 2, "降级只试一次，别在风控上反复试探")

    def test_waf_without_custom_prompt_does_not_retry(self):
        """系统提示词本来就是我们补的中性那句，重发等于白烧一次风控计数。"""
        error = UpstreamError("Request blocked by WAF", kind="waf", status=403, retryable=True)
        svc, client = self.service([error])
        with self.assertRaises(ServiceError):
            svc.chat_completions(self.request(self.body()))
        self.assertEqual(len(client.calls), 1)

    def test_media_request_does_not_bind_affinity(self):
        from wb2api.httpserver import Request

        svc, client = self.service([ok_stream("hi")])
        payload = self.body(stream=True, messages=[{"role": "user", "content": [
            {"type": "text", "text": "这是什么"},
            {"type": "image_url", "image_url": {"url": "https://x/1.png"}},
        ]}])
        req = Request(
            "POST", "/v1/chat/completions", {}, {"X-Conversation-Id": "conv-img"},
            json.dumps(payload).encode(), ("1.2.3.4", 1),
        )
        list(svc.chat_completions(req).stream)
        self.assertEqual(svc.affinity.candidates("conv-img"), [],
                         "媒体请求钉在同一个号上只会让它的画像更反常")


if __name__ == "__main__":
    unittest.main(verbosity=2)
