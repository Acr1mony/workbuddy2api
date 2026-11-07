"""util 里的"基础设施"函数。

这些函数几乎都不会抛异常：坏配置回落默认值、坏文件改名后返回默认、读不出的 JWT
返回 {}。静默降级最难排查，所以每条降级路径都要有显式用例，而不是只测正常路径。
"""

from __future__ import annotations

import base64
import glob
import json
import os
import tempfile
import threading
import unittest

from wb2api import util


class DurationTests(unittest.TestCase):
    def test_parse_known_units(self) -> None:
        cases = {
            "30m": 1800.0,
            "2h": 7200.0,
            "1d": 86400.0,
            "900s": 900.0,
            "900": 900.0,
            "1.5h": 5400.0,
            "250ms": 0.25,
            " 2H ": 7200.0,  # 大小写与首尾空白都不该影响
            "-5m": -300.0,
        }
        for text, expected in cases.items():
            self.assertAlmostEqual(util.parse_duration(text), expected, msg=text)

    def test_numbers_are_seconds_but_bools_are_not(self) -> None:
        self.assertEqual(util.parse_duration(900), 900.0)
        self.assertEqual(util.parse_duration(1.5), 1.5)
        # bool 是 int 的子类；配置里的 true 不能被理解成 1 秒。
        self.assertEqual(util.parse_duration(True, 7.0), 7.0)
        self.assertEqual(util.parse_duration(False, 7.0), 7.0)

    def test_garbage_falls_back_to_default(self) -> None:
        # 坏配置不能让网关起不来：一律回落 default，由 config.validate() 汇报。
        for bad in (None, "", "   ", "abc", "5x", {}, [], "10 s 20", "m30"):
            self.assertEqual(util.parse_duration(bad, 7.0), 7.0, bad)

    def test_format_duration_shapes(self) -> None:
        cases = {
            0: "0s",
            45: "45s",
            60: "1m",
            90: "1m30s",
            600: "10m",
            3600: "1h",
            5400: "1h30m",
            86400: "24h",
            -5: "0s",  # 负数按 0 展示，不能出现 "-5s"
        }
        for seconds, expected in cases.items():
            self.assertEqual(util.format_duration(seconds), expected, seconds)

    def test_round_trip_for_values_both_functions_agree_on(self) -> None:
        # format_duration 只在"整分/整时/整秒"时输出单一单位，这些值可以原样喂回。
        for seconds in (0, 5, 45, 60, 120, 600, 7200, 86400, -1):
            text = util.format_duration(seconds)
            self.assertEqual(util.parse_duration(text, -999), max(0, seconds), text)

    def test_compound_display_string_is_not_a_config_value(self) -> None:
        # format_duration(90) == "1m30s"，但 parse_duration 只接受"一个数字+一个单位"。
        # 这是当前实现的边界：展示用字符串不要回填到配置里。
        self.assertEqual(util.format_duration(90), "1m30s")
        self.assertEqual(util.parse_duration("1m30s", 0.0), 0.0)


class TelegramIdTests(unittest.TestCase):
    def test_deterministic_hex_of_length_36(self) -> None:
        first = util.telegram_id("wb2a:device:uid-1")
        self.assertEqual(first, util.telegram_id("wb2a:device:uid-1"), "同一账号重启后设备指纹必须不变")
        self.assertEqual(len(first), 36)
        self.assertRegex(first, r"^[0-9a-f]{36}$")

    def test_seed_and_length_change_the_id(self) -> None:
        base = util.telegram_id("wb2a:device:uid-1")
        self.assertNotEqual(base, util.telegram_id("wb2a:device:uid-2"))
        self.assertNotEqual(base, util.telegram_id("wb2a:session:uid-1"), "设备码与会话码不能撞")
        short = util.telegram_id("seed", 24)
        self.assertEqual(len(short), 24)
        self.assertRegex(short, r"^[0-9a-f]{24}$")


class EpochTests(unittest.TestCase):
    def test_seconds_pass_through(self) -> None:
        self.assertEqual(util.normalize_epoch(1700000000), 1700000000.0)
        self.assertEqual(util.normalize_epoch("1700000000"), 1700000000.0)
        self.assertEqual(util.normalize_epoch(1700000000.5), 1700000000.5)

    def test_milliseconds_are_divided(self) -> None:
        # 桌面端凭据文件是毫秒；> 1e11 一律按毫秒处理。
        self.assertAlmostEqual(util.normalize_epoch(1700000000123), 1700000000.123)

    def test_invalid_is_zero(self) -> None:
        for bad in (None, "", 0, -1, "junk", {}, [], 1700000000000000):  # 最后一个是微秒
            self.assertEqual(util.normalize_epoch(bad), 0.0, bad)


class MaskSecretTests(unittest.TestCase):
    def test_empty_stays_empty(self) -> None:
        self.assertEqual(util.mask_secret(None), "")
        self.assertEqual(util.mask_secret(""), "")

    def test_short_values_are_fully_masked(self) -> None:
        for value in ("a", "abc", "abcdef", "abcdefghijkl"):
            masked = util.mask_secret(value)
            self.assertEqual(masked, "*" * len(value), value)
            self.assertNotIn(value, masked)

    def test_long_values_keep_edges_only(self) -> None:
        masked = util.mask_secret("sk-1234567890")
        self.assertEqual(masked, "sk-123***890")
        self.assertIn("***", masked)
        self.assertNotIn("sk-1234567890", masked)


class RedactTests(unittest.TestCase):
    def test_masks_sensitive_keys_recursively(self) -> None:
        payload = {
            "access_token": "token-abcdef",
            "Authorization": "Bearer sk-abcdef123456",
            "nested": {"api_key": "sk-abcdef123456", "keep": "visible"},
            "items": [{"password": "hunter2hunter2"}],
            "count": 3,
        }
        out = util.redact(payload)
        blob = json.dumps(out, ensure_ascii=False)
        for secret in ("token-abcdef", "Bearer sk-abcdef123456", "sk-abcdef123456", "hunter2hunter2"):
            self.assertNotIn(secret, blob)
        self.assertEqual(out["nested"]["keep"], "visible", "非敏感字段不能被误伤")
        self.assertEqual(out["count"], 3)

    def test_key_matching_is_case_insensitive(self) -> None:
        out = util.redact({"ACCESS_TOKEN": "token-abcdef", "Api_Key": "sk-1234567890"})
        for value in out.values():
            self.assertIn("***", value)

    def test_sensitive_keys_catalogue(self) -> None:
        # 新增敏感字段时先加进这个集合，否则 redact 和面板脱敏都会漏。
        for name in ("access_token", "refresh_token", "device_token", "api_key", "password", "authorization"):
            self.assertIn(name, util.SENSITIVE_KEYS)

    def test_non_string_values_are_left_alone(self) -> None:
        self.assertEqual(util.redact({"token": 12345}), {"token": 12345})
        self.assertIsNone(util.redact({"token": None})["token"])


class FileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wb2a-util-")
        self.dir = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def test_atomic_write_then_read_round_trip(self) -> None:
        target = self.path("state.json")
        payload = {"a": 1, "中文": "值", "nested": [1, 2]}
        util.atomic_write_json(target, payload)
        self.assertEqual(util.read_json(target), payload)
        # 原子写：目标目录里不能残留 .tmp-* 半成品，否则说明 rename 失败过。
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".tmp-")], [])
        with open(target, "r", encoding="utf-8") as handle:
            json.load(handle)

    def test_read_missing_returns_default(self) -> None:
        self.assertEqual(util.read_json(self.path("nope.json"), {"x": 1}), {"x": 1})
        self.assertIsNone(util.read_json(self.path("nope.json")))

    def test_corrupt_file_is_quarantined_and_default_returned(self) -> None:
        target = self.path("accounts.json")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(util.read_json(target, {"default": True}), {"default": True})
        self.assertFalse(os.path.exists(target), "坏文件必须让位，否则下次保存会覆盖现场")
        quarantine = glob.glob(target + ".corrupt-*")
        self.assertEqual(len(quarantine), 1, "只应该隔离一次")
        with open(quarantine[0], "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{not json", "隔离文件要保留原始内容供人工排查")

    def test_undecodable_file_is_also_quarantined(self) -> None:
        target = self.path("bad.json")
        with open(target, "wb") as handle:
            handle.write(b"\xff\xfe\x00")
        self.assertEqual(util.read_json(target, "default"), "default")
        self.assertEqual(len(glob.glob(target + ".corrupt-*")), 1)

    def test_append_and_tail_jsonl(self) -> None:
        target = self.path("usage.jsonl")
        for index in range(3):
            util.append_jsonl(target, {"i": index, "中文": "记录"})
        self.assertEqual([row["i"] for row in util.tail_jsonl(target, 2)], [1, 2], "最新一条在最后")
        self.assertEqual([row["i"] for row in util.tail_jsonl(target)], [0, 1, 2])
        self.assertEqual(util.tail_jsonl(self.path("missing.jsonl")), [])

    def test_tail_skips_broken_lines(self) -> None:
        # 台账是 append-only 的，崩在半行时不能让整份历史都读不出来。
        target = self.path("usage.jsonl")
        util.append_jsonl(target, {"ok": 1})
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("this is not json\n")
        util.append_jsonl(target, {"ok": 3})
        self.assertEqual([row["ok"] for row in util.tail_jsonl(target)], [1, 3])


class KeyedLockTests(unittest.TestCase):
    def test_same_key_shares_one_lock(self) -> None:
        locks = util.KeyedLock()
        self.assertIs(locks.get("acct-a"), locks.get("acct-a"))
        self.assertIsNot(locks.get("acct-a"), locks.get("acct-b"))

    def test_lock_blocks_other_threads_until_released(self) -> None:
        locks = util.KeyedLock()
        lock = locks.get("acct-a")
        taken = []

        def worker() -> None:
            taken.append(lock.acquire(blocking=False))

        with lock:
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(taken, [False], "持锁期间其他线程不该拿到同一把键锁")
            # 别的键不受影响：热路径不能被无关账号串行化。
            other = locks.get("acct-b")
            self.assertTrue(other.acquire(blocking=False))
            other.release()

        self.assertTrue(lock.acquire(blocking=False), "释放后应能再次获取")
        lock.release()

    def test_different_threads_can_take_different_keys(self) -> None:
        locks = util.KeyedLock()
        results = []

        def worker(key: str) -> None:
            with locks.get(key):
                results.append(key)

        threads = [threading.Thread(target=worker, args=(key,)) for key in ("a", "b", "c")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(sorted(results), ["a", "b", "c"])


class UrlTests(unittest.TestCase):
    def test_split_url_known_shapes(self) -> None:
        cases = {
            "https://host:8443/a?b": ("https", "host", 8443, "/a?b"),
            "http://user:pass@host/x": ("http", "host", 80, "/x"),
            "socks5://proxy:1080": ("socks5", "proxy", 1080, "/"),
            "https://host:443": ("https", "host", 443, "/"),
            "[::1]:8080/v1": ("https", "[::1]", 8080, "/v1"),
        }
        for url, expected in cases.items():
            self.assertEqual(util.split_url(url), expected, url)

    def test_bare_host_defaults_to_https_443(self) -> None:
        self.assertEqual(util.split_url("example.com"), ("https", "example.com", 443, "/"))

    def test_empty_url_does_not_raise(self) -> None:
        self.assertEqual(util.split_url(""), ("https", "", 443, "/"))


class QuoteTests(unittest.TestCase):
    def test_round_trip_with_reserved_characters(self) -> None:
        text = "a b/c?d=中文&e"
        encoded = util.quote(text)
        self.assertIn("%20", encoded)
        self.assertIn("%2F", encoded, "safe='' 意味着斜杠也要转义")
        self.assertEqual(util.unquote(encoded), text)

    def test_empty_values(self) -> None:
        self.assertEqual(util.quote(""), "")
        self.assertEqual(util.unquote(""), "")
        self.assertEqual(util.quote(None), "")


def _unsigned_token(claims) -> str:
    """手搓一个 alg=none 的 JWT：网关只读载荷，不验签。"""

    def encode(value) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(claims)}.sig"


class JwtTests(unittest.TestCase):
    def test_reads_claims_without_verifying(self) -> None:
        claims = {"sub": "uid-1", "exp": 1700000000, "realm": "cn"}
        self.assertEqual(util.jwt_claims(_unsigned_token(claims)), claims)

    def test_padding_is_restored_for_odd_lengths(self) -> None:
        # payload 的 base64 长度不是 4 的倍数时，缺的 "=" 要自己补回来。
        claims = {"sub": "u"}
        token = _unsigned_token(claims)
        self.assertNotEqual(len(token.split(".")[1]) % 4, 0)
        self.assertEqual(util.jwt_claims(token), claims)

    def test_garbage_returns_empty_dict(self) -> None:
        for token in ("", "abc", "a.b.c", "a.!!!.c", "!!.!!.!!", None):
            self.assertEqual(util.jwt_claims(token), {}, token)

    def test_non_object_payload_is_ignored(self) -> None:
        # 合法 base64 但载荷是数组：调用方期望 dict，不能把 list 当 claims 用。
        self.assertEqual(util.jwt_claims("a.W10.c"), {})


if __name__ == "__main__":
    unittest.main()
