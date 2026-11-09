"""Small, dependency-free helpers shared by every module.

Design rule for this project: standard library only.  Everything that would
normally pull in a third-party package (durations, atomic writes, ids, locks,
timestamps) lives here so the rest of the code stays readable.
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import secrets
import string
import tempfile
import threading
import time
import urllib.parse
import uuid
from typing import Any, Dict, Iterable, Optional

# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------

_DURATION_UNITS = {
    "ms": 0.001,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "d": 86400.0,
}


def now() -> float:
    """Current epoch seconds (the single clock used across the gateway)."""
    return time.time()


def iso(ts: Optional[float] = None) -> str:
    """Local-time ISO-8601 with second precision, used in logs and JSON."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now() if ts is None else ts))


def parse_duration(value: Any, default: float = 0.0) -> float:
    """Parse ``"30m"`` / ``"2h"`` / ``900`` / ``"900s"`` into seconds.

    Numbers are treated as seconds.  Unknown strings fall back to *default*
    instead of raising: configuration errors must never stop the gateway from
    booting, they are reported by ``config.validate()`` instead.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return default
    text = value.strip().lower()
    if not text:
        return default
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return float(text)
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([a-z]+)", text)
    if not match:
        return default
    amount, unit = float(match.group(1)), match.group(2)
    # 未知单位必须整体回落：`amount * default` 会把 "5x" 变成 5*default，
    # 传 -1 做"合法与否"探测时会被误判成 -5。
    if unit not in _DURATION_UNITS:
        return default
    return amount * _DURATION_UNITS[unit]


def format_duration(seconds: float) -> str:
    """Human readable duration, e.g. ``90 -> "1m30s"``."""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s" if seconds % 60 else f"{seconds // 60}m"
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"


# --------------------------------------------------------------------------
# ids
# --------------------------------------------------------------------------

_HEX = string.hexdigits.lower()[:16]


def rand_hex(length: int = 32) -> str:
    """Random hex string (``length`` characters) from the OS CSPRNG.

    这份输出会被拿去当面板 HMAC 密钥和 API key，所以必须走 ``secrets``：
    ``random.choice`` 是梅森旋转，看到一段输出就能推出后续。
    """
    if length <= 0:
        return ""
    return secrets.token_hex((length + 1) // 2)[:length]


def uuid_hex() -> str:
    return uuid.uuid4().hex


def request_id() -> str:
    """Upstream-compatible request id: lowercase uuid with dashes."""
    return str(uuid.uuid4())


def telegram_id(seed: str, length: int = 36) -> str:
    """Deterministic 36-hex identifier derived from *seed*.

    Used for per-account stable device / session identifiers: identical input
    produces an identical id across restarts, which is what the upstream
    anti-abuse layer expects (it remembers the device, not the process).
    """
    import hashlib

    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    while len(digest) < length:
        digest += hashlib.sha256(digest.encode("ascii")).hexdigest()
    return digest[:length]


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------


def ensure_dir(path: str) -> str:
    if path:
        os.makedirs(path, exist_ok=True)
    return path


class CorruptFile(RuntimeError):
    """A state file exists but cannot be parsed, and the caller asked to be strict.

    对 ``accounts.json`` / ``api_keys.json`` / ``config.json`` 这类"读错了会静默
    降级成空库或默认配置"的文件，必须让调用方决定：带着一份备份退出，比带着
    空账号池大开鉴权跑一晚上好得多。
    """

    def __init__(self, path: str, backup: str = "", error: Optional[BaseException] = None) -> None:
        super().__init__(f"{path} is not valid JSON" + (f" (backup: {backup})" if backup else ""))
        self.path = path
        self.backup = backup
        self.error = error


def read_json(path: str, default: Any = None, *, strict: bool = False) -> Any:
    """Read JSON, returning *default* when the file is missing or corrupt.

    A corrupt file is renamed to ``<name>.corrupt-<ts>`` so the next save does
    not silently destroy whatever the operator hand-edited. With ``strict=True``
    a corrupt file raises :class:`CorruptFile` instead of quietly returning the
    default — the backup has already been made at that point.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        backup = ""
        try:
            backup = f"{path}.corrupt-{int(now())}"
            os.replace(path, backup)
        except OSError:
            backup = ""
        if strict:
            raise CorruptFile(path, backup, exc) from exc
        return default


def atomic_write_json(path: str, payload: Any, *, indent: int = 2) -> None:
    """Write JSON atomically (temp file in the same directory + os.replace)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    ensure_dir(directory)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".tmp-", delete=False
    )
    try:
        with handle as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=indent)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def append_jsonl(path: str, payload: Dict[str, Any]) -> None:
    ensure_dir(os.path.dirname(os.path.abspath(path)) or ".")
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def tail_jsonl(path: str, limit: int = 200) -> Iterable[Dict[str, Any]]:
    """Return up to *limit* most recent records, newest last."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# --------------------------------------------------------------------------
# locks
# --------------------------------------------------------------------------


class KeyedLock:
    """A lock per string key, created on demand.

    Account state (cooldown counters, in-flight counters) is mutated from many
    request threads; a plain ``threading.Lock`` is not enough because the hot
    path must not serialise unrelated accounts.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: Dict[str, threading.RLock] = {}

    def get(self, key: str) -> threading.RLock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock

    def __enter__(self) -> "KeyedLock":
        self._guard.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._guard.release()


# --------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------


def mask_secret(value: Optional[str], keep: int = 6) -> str:
    """``sk-abcdef123456`` -> ``sk-abc***3456``; empty stays empty."""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}***{value[-keep // 2:]}"


SENSITIVE_KEYS = {
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "api_key",
    "password",
    "secret",
    "authorization",
    "device_token",
    "cookie",
}


def redact(value: Any) -> Any:
    """Recursively mask sensitive values before they reach a log or the panel."""
    if isinstance(value, dict):
        return {
            key: (mask_secret(v) if isinstance(v, str) and key.lower() in SENSITIVE_KEYS else redact(v))
            for key, v in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def first_line(text: Optional[str], limit: int = 300) -> str:
    if not text:
        return ""
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:limit]


def utc_stamp(ts: Optional[float] = None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(now() if ts is None else ts))


# --------------------------------------------------------------------------
# urls
# --------------------------------------------------------------------------

_PORT_DEFAULTS = {"http": 80, "https": 443}


def split_url(url: str) -> tuple:
    """``https://host:8443/a?b`` -> ``("https", "host", 8443, "/a?b")``.

    ``urllib.parse`` 当然能做这件事，但我们要的是一个不会拒绝奇怪协议的
    四元组（账号代理可能是 ``socks5://``，我们只做提示不做连接）。
    """
    text = (url or "").strip()
    if "://" not in text:
        text = "https://" + text
    scheme, rest = text.split("://", 1)
    scheme = scheme.lower()
    slash = rest.find("/")
    if slash >= 0:
        authority, path = rest[:slash], rest[slash:]
    else:
        authority, path = rest, "/"
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    if authority.startswith("["):
        host, _, tail = authority.partition("]")
        host = host + "]"
        port = int(tail[1:]) if tail.startswith(":") and tail[1:].isdigit() else _PORT_DEFAULTS.get(scheme, 443)
    elif ":" in authority:
        host, port_text = authority.rsplit(":", 1)
        port = int(port_text) if port_text.isdigit() else _PORT_DEFAULTS.get(scheme, 443)
    else:
        host, port = authority, _PORT_DEFAULTS.get(scheme, 443)
    return scheme, host, port, path


def proxy_parts(proxy: str) -> tuple:
    """``http://user:pass@host:8080`` -> ``("host", 8080, "user", "pass")``。

    不支持的目标（socks5 等）返回 ``("", 0, "", "")``，由调用方决定是否忽略。
    """
    text = (proxy or "").strip()
    if not text:
        return "", 0, "", ""
    if "://" not in text:
        text = "http://" + text
    scheme, rest = text.split("://", 1)
    if scheme.lower() not in ("http", "https"):
        return "", 0, "", ""
    authority = rest.split("/", 1)[0]
    user = password = ""
    if "@" in authority:
        cred, authority = authority.rsplit("@", 1)
        if ":" in cred:
            user, password = cred.split(":", 1)
        else:
            user = cred
    host, _, port_text = authority.rpartition(":") if ":" in authority else (authority, "", "")
    if not host:
        host, port_text = authority, ""
    port = int(port_text) if port_text.isdigit() else 80
    return host, port, unquote(user), unquote(password)


def unquote(text: str) -> str:
    return urllib.parse.unquote(text) if text else ""


def quote(text: str) -> str:
    return urllib.parse.quote(str(text or ""), safe="")


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------


def jwt_claims(token: str) -> dict:
    """读 JWT 载荷（**不验签**）。

    网关只需要拿 ``sub``（uid）和 ``exp``（过期时间）做本地判断，签名本身由上游
    在每次请求时校验；因此这里刻意不做验签，也不引入依赖。
    """
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(body.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def normalize_epoch(value: Any) -> float:
    """把上游的时间戳统一成秒级 epoch；认不出就返回 0。

    上游在不同接口里混用秒和毫秒（桌面端凭据文件是毫秒），``> 1e11`` 一律按毫秒处理。
    """
    if value is None or value == "":
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number <= 0:
        return 0.0
    if number > 1e11:
        number /= 1000.0
    if number > 1e12:  # 微秒级，明显不对
        return 0.0
    return number
