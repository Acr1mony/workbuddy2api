"""失败报文回放：预览（不打上游）+ 回放（真发一次）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb2api import replay
from wb2api.config import Config
from wb2api.errors import ServiceError
from wb2api.service import GatewayService
from wb2api.store import Account, Store
from tests.test_service import FakeClient, FakeResponse, chat_chunk, frame, sse_lines

DSH_SYSTEM = (
    "You are an AI agent powered by DeepSeek Harness.\n"
    "You are interacting with the user through the DeepSeek Harness Web GUI whose URL the runtime context names.\n"
    "Tokens prefixed with @ are paths the user explicitly referenced."
)


def body_with(system: str = DSH_SYSTEM) -> dict:
    return {
        "model": "deepseek-v3",
        "stream": True,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "hi"},
        ],
    }


class ReplayBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb2a-replay-")
        self.cfg = Config(None, {"data_dir": self.tmp, "panel": {"enabled": False}})
        store = Store(self.tmp)
        store.upsert_account(Account(uid="u1", id="u1", realm="cn", access_token="tok-u1",
                                     refresh_token="rt-u1", expires_at=time.time() + 86400))
        self.svc = GatewayService(self.cfg, Store(self.tmp), client=FakeClient([
            FakeResponse(sse_lines(chat_chunk("pong"), chat_chunk("", finish="stop"),
                                   frame("[DONE]"))),
        ]))

    def dump(self, body: dict = None, *, long_body: bool = False, request_id: str = "req_d1") -> str:
        if long_body:
            # 超过 max_body_bytes（默认 8192）：dump 自己会截断并置 request_truncated，
            # 必须走真实落盘路径——改返回的副本没用。
            body = body_with(DSH_SYSTEM + "x" * 9000)
        payload = json.dumps(body if body is not None else body_with(), ensure_ascii=False)
        self.svc.diag.dump(
            request_id=request_id, model="deepseek-v3", realm="cn", status=400,
            code="11-128", message="Illegal API invocation from an unapproved channel",
            body='{"code":"11-128"}',
            request_headers={"Authorization": "Bearer tok"},
            request_body=payload,
        )
        return request_id


class PreviewTest(ReplayBase):
    def test_preview_shows_sanitized_system_without_calling_upstream(self):
        rid = self.dump()
        result = replay.preview(self.svc, rid)
        self.assertTrue(result["changed"])
        self.assertTrue(any("DeepSeek" in text for text in result["system_before"]))
        joined_after = "\n".join(result["system_after"])
        # 三处指纹都按"最小改写"处理：语序、补词、整句换中性句
        self.assertIn("whose URL is named in the runtime context.", joined_after)
        self.assertIn("paths that the user explicitly referenced.", joined_after)
        self.assertIn("helpful AI assistant", joined_after)
        # 品牌名本身**故意保留**（上游只拦品牌与上下文连排，瞎删会伤正文）
        self.assertIn("DeepSeek Harness", joined_after)
        self.assertEqual(self.svc.client.calls, [])

    def test_preview_reports_each_changed_message(self):
        rid = self.dump()
        result = replay.preview(self.svc, rid)
        self.assertEqual(result["changes"][0]["role"], "system")
        self.assertNotEqual(result["changes"][0]["before"], result["changes"][0]["after"])

    def test_unknown_request_id(self):
        with self.assertRaises(ServiceError) as ctx:
            replay.preview(self.svc, "req_nope")
        self.assertEqual(ctx.exception.code, "unknown_dump")

    def test_missing_body(self):
        self.svc.diag.dump(request_id="req_nb", model="m", realm="cn", status=502,
                           code="upstream", message="boom", body="", request_headers={})
        with self.assertRaises(ServiceError) as ctx:
            replay.preview(self.svc, "req_nb")
        self.assertEqual(ctx.exception.code, "no_request_body")

    def test_truncated_body_is_refused(self):
        rid = self.dump(long_body=True)
        with self.assertRaises(ServiceError) as ctx:
            replay.preview(self.svc, rid)
        self.assertEqual(ctx.exception.code, "truncated_body")


class ReplayTest(ReplayBase):
    def test_replay_sends_sanitized_body_and_reports(self):
        rid = self.dump()
        result = replay.replay(self.svc, rid, sanitize=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["sample"], "pong")
        sent = json.loads(self.svc.client.calls[0]["body"].decode("utf-8"))
        system = [m for m in sent["messages"] if m["role"] == "system"][0]["content"]
        # 指纹三处都被最小改写（品牌保留是设计，见 test_preview_*）
        self.assertIn("helpful AI assistant", system)
        self.assertIn("whose URL is named in the runtime context.", system)
        self.assertFalse(sent["stream"])
        # 槽位必须还回去，否则几次回放就把池子锁死
        self.assertEqual(sum(a.in_flight for a in self.svc.store.list_accounts("cn")), 0)

    def test_replay_without_sanitize_reproduces_the_fingerprint(self):
        rid = self.dump()
        replay.replay(self.svc, rid, sanitize=False)
        sent = json.loads(self.svc.client.calls[0]["body"].decode("utf-8"))
        system = [m for m in sent["messages"] if m["role"] == "system"][0]["content"]
        self.assertIn("DeepSeek Harness", system)

    def test_replay_is_recorded_in_ledger(self):
        rid = self.dump()
        replay.replay(self.svc, rid, sanitize=True)
        recent = self.svc.reqlog.recent(5)
        self.assertTrue(any(item.get("kind") == "replay" for item in recent), recent)

    def test_replay_without_accounts_reports_failure(self):
        store = Store(self.tmp)
        for account in store.list_accounts("cn"):
            account.disabled = True
        store.save_accounts()
        empty = GatewayService(self.cfg, store, client=FakeClient([]))
        rid = self.dump()
        result = replay.replay(empty, rid, sanitize=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "no_account")


if __name__ == "__main__":
    unittest.main()
