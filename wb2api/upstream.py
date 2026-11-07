"""上游 HTTP 客户端：连接复用、流式读取、错误归一化。

只依赖标准库的 ``http.client``，因为我们要三件 ``urllib`` 做不好的事：

1. **增量读取**：SSE 必须一帧一帧往外吐，不能等整个响应体；
2. **空闲超时**：模型思考 30 秒不出字是正常的，但如果 5 分钟没有任何字节就该断；
3. **可预测的重连**：连接复用失败要能安全重试，而不是把半个响应交给客户端。

错误统一收敛成 :class:`UpstreamError`，按 ``kind`` 区分处置方式：
``transient`` / ``rate_limit`` 可换号重试，``auth`` 要刷新 token，
``quota`` 要冷却账号，``bad_request`` 必须改请求体（换号没用）。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import select
import socket
import ssl
import threading
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from . import identity, sse, util
from . import config as conf
from .log import get_logger

log = get_logger("upstream")

# 上游非 2xx 时 body 里的业务码
CODE_AUTH_EXPIRED = 11100
CODE_PARAM_INVALID = 11101
CODE_MODEL_PARAM_INVALID = 11133
CODE_TOOL_PAIRING = 11148
CODE_RATE_LIMITED = 11104
CODE_QUOTA = 11109
CODE_NO_MODEL = 11103
CODE_RESTRICTED = 11140
CODE_SESSION_DEAD = 12153

TRANSIENT_STATUS = {500, 502, 503, 504, 520, 521, 522, 523, 524}

# 403 有三种完全不同的含义，处置方式相反，绝不能一律当"凭证失效"：
# ``restricted`` 是风控封号（换号会连坐，只能停）；
# ``waf`` 是链路层/内容层拦截（跟账号无关，软冷却后重试即可）；
# 其余才是真的要刷 token。
_RESTRICTED_RE = re.compile(r'"code"\s*:\s*"?11140"?')
_RESTRICTED_MARKERS = ("11140", "unusual activity", "账号存在异常行为", "已被限制", "异常行为")
_WAF_MARKERS = (
    "unusual activity",
    "access denied",
    "request blocked",
    "captcha",
    "web application firewall",
    "安全策略",
    "访问被拒绝",
)


def _unescape(text: str) -> str:
    """上游偶尔把整个信封再套一层转义（``"{\\"code\\":11140}"``），解一层再看。

    ``json.loads`` 出来是 str 时说明多套了一层，也一并拆掉。只解两三层就够，
    无限解会把正常内容里的引号也当转义。
    """
    current = text or ""
    for _ in range(3):
        stripped = current.strip()
        if not stripped:
            return current
        if stripped.startswith('"') and stripped.endswith('"') and len(stripped) > 2:
            try:
                inner = json.loads(stripped)
            except (ValueError, TypeError):
                return current
            if isinstance(inner, str) and inner != current:
                current = inner
                continue
        if '\\"' in current:
            # 只有纯 ASCII 才能安全走 unicode_escape：中文内容会被解成乱码
            if not current.isascii():
                return current
            try:
                current = current.encode("utf-8").decode("unicode_escape")
                continue
            except (UnicodeDecodeError, ValueError):
                return current
        return current
    return current


def is_restricted(body_text: str, code: int = 0, message: str = "") -> bool:
    """这股请求是不是被风控当成脚本了。

    只看 ``code == 11140`` 会漏：消息可能被 HTML 包着、也可能被转义两次。
    漏判的代价是"换号"——一次连坐能烧掉整池，所以这里宁可误判成受限。
    """
    if code == CODE_RESTRICTED:
        return True
    text = _unescape(body_text)
    if _RESTRICTED_RE.search(text):
        return True
    probe = f"{message}\n{text}".lower()
    return any(marker in probe for marker in _RESTRICTED_MARKERS)


def is_waf(body_text: str, message: str = "") -> bool:
    """403 但不是风控封号：网关侧/内容审核拦的，跟账号健康无关。"""
    text = _unescape(body_text or "")
    stripped = text.strip()
    looks_json = stripped.startswith("{") or stripped.startswith("[")
    if not looks_json:
        # 边缘层（openresty/APISIX）的 HTML 错误页都长这样
        return True
    probe = f"{message}\n{stripped}".lower()
    return any(marker in probe for marker in _WAF_MARKERS)


class UpstreamError(Exception):
    """一个出站请求的失败，带上足够信息让上层决定"下一步"。"""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "error",
        status: int = 0,
        code: int = 0,
        retryable: bool = False,
        retry_after: float = 0.0,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status = status
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.body = body

    @property
    def auth_failure(self) -> bool:
        return self.kind == "auth"

    @property
    def quota_failure(self) -> bool:
        return self.kind == "quota"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }

    def __str__(self) -> str:
        return f"[{self.kind}{'' if not self.status else ' ' + str(self.status)}] {self.message}"


def _error_text(payload: Any) -> Tuple[int, str]:
    """从上游错误体里抠出 (code, message)。上游有三种形状，都见过。"""
    if not isinstance(payload, dict):
        return 0, ""
    code = 0
    for key in ("code", "errorCode", "errCode", "error_code"):
        value = payload.get(key)
        if isinstance(value, int):
            code = value
            break
        if isinstance(value, str) and value.isdigit():
            code = int(value)
            break
    message = ""
    for key in ("msg", "message", "errorMsg", "error_description"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            message = value
            break
    error = payload.get("error")
    if isinstance(error, dict):
        if not message:
            for key in ("message", "msg", "description"):
                value = error.get(key)
                if isinstance(value, str) and value:
                    message = value
                    break
        if not code:
            for key in ("code", "type"):
                value = error.get(key)
                if isinstance(value, int):
                    code = value
                elif isinstance(value, str) and value.isdigit():
                    code = int(value)
    elif isinstance(error, str) and not message:
        message = error
    if not message:
        for key in ("statusText", "status_text"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                message = value
                break
    return code, message


_BAD_REQUEST_CODES = {CODE_PARAM_INVALID, CODE_MODEL_PARAM_INVALID, CODE_TOOL_PAIRING, CODE_NO_MODEL}
_QUOTA_CODES = {CODE_QUOTA}


def classify(status: int, body_text: str = "", retry_after: float = 0.0) -> UpstreamError:
    """把 HTTP 状态 + 业务码翻译成统一的 kind。"""
    payload: Any = None
    if body_text:
        try:
            payload = json.loads(body_text)
        except (ValueError, TypeError):
            payload = None
    code, message = _error_text(payload)
    if not message:
        message = (body_text or "").strip()[:400] or f"upstream HTTP {status}"
    lowered = message.lower()

    if status == 403:
        if is_restricted(body_text, code, message):
            return UpstreamError(message, kind="restricted", status=status, code=code or CODE_RESTRICTED, body=body_text)
        if is_waf(body_text, message):
            # 内容审核 / WAF 拦的，账号本身没问题：只有它走软冷却，且绝不刷新凭证
            return UpstreamError(message, kind="waf", status=status, code=code, retryable=True, body=body_text)
        return UpstreamError(message, kind="auth", status=status, code=code, body=body_text)
    if status == 401 or code == CODE_AUTH_EXPIRED or (status == 401 and "token" in lowered):
        return UpstreamError(message, kind="auth", status=status, code=code, body=body_text)
    if status == 429 or code == CODE_RATE_LIMITED or "rate limit" in lowered or "too many" in lowered:
        return UpstreamError(
            message, kind="rate_limit", status=status, code=code, retryable=True, retry_after=retry_after, body=body_text
        )
    if status == 402 or code in _QUOTA_CODES or "quota" in lowered or "balance" in lowered or "credit" in lowered:
        return UpstreamError(message, kind="quota", status=status, code=code, body=body_text)
    if code in _BAD_REQUEST_CODES:
        return UpstreamError(message, kind="bad_request", status=status, code=code, body=body_text)
    if status in TRANSIENT_STATUS:
        return UpstreamError(
            message, kind="transient", status=status, code=code, retryable=True, retry_after=retry_after, body=body_text
        )
    if status >= 500:
        return UpstreamError(
            message, kind="transient", status=status, code=code, retryable=True, retry_after=retry_after, body=body_text
        )
    if status >= 400:
        return UpstreamError(message, kind="bad_request", status=status, code=code, body=body_text)
    return UpstreamError(message, kind="error", status=status, code=code, body=body_text)


# --------------------------------------------------------------------------
# 响应
# --------------------------------------------------------------------------


class UpstreamResponse:
    """一个已经拿到响应头的上游响应，body 按需读。"""

    def __init__(self, conn: http.client.HTTPConnection, raw: http.client.HTTPResponse, pool_key: str, pool: "_ConnPool") -> None:
        self._conn = conn
        self._raw = raw
        self._pool_key = pool_key
        self._pool = pool
        self.status = raw.status
        self.reason = raw.reason
        self.headers = {key.lower(): value for key, value in raw.getheaders()}
        self._closed = False
        # 只有"整段 body 都读完了"的连接才允许回池。半读的连接里还留着上一条流的残帧，
        # 放回去会让下一个请求读到别人的数据（协议错位，比不复用严重得多）。
        self._consumed = False

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def _read(self, limit: int) -> bytes:
        chunks: List[bytes] = []
        total = 0
        while total < limit:
            data = self._raw.read(min(65536, limit - total))
            if not data:
                break
            chunks.append(data)
            total += len(data)
        return b"".join(chunks)

    def text(self, limit: int = 1_000_000) -> str:
        try:
            data = self._read(limit)
            if len(data) < limit:
                self._consumed = True
            return data.decode("utf-8", "replace")
        finally:
            self.close()

    def json(self, limit: int = 1_000_000) -> Any:
        text = self.text(limit)
        try:
            return json.loads(text)
        except ValueError:
            raise UpstreamError(f"upstream returned non-JSON: {text[:200]!r}", kind="error", status=self.status)

    def iter_lines(self, idle_timeout: float = 0.0) -> Iterator[bytes]:
        """逐行读（SSE 的天然单位），带空闲超时。"""
        buffer = b""
        try:
            while True:
                if idle_timeout:
                    try:
                        self._conn.sock.settimeout(idle_timeout)  # type: ignore[union-attr]
                    except (AttributeError, OSError):
                        pass
                chunk = self._raw.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    yield line.rstrip(b"\r")
            if buffer:
                yield buffer.rstrip(b"\r")
            self._consumed = True
        except socket.timeout:
            raise UpstreamError(
                f"upstream idle for more than {idle_timeout:.0f}s", kind="timeout", status=0
            )
        except (http.client.IncompleteRead, ConnectionError) as exc:
            raise UpstreamError(f"upstream stream broke: {exc}", kind="transient", status=0, retryable=True)
        finally:
            self.close()

    def iter_text(self, idle_timeout: float = 0.0) -> Iterator[str]:
        for line in self.iter_lines(idle_timeout):
            yield line.decode("utf-8", "replace")

    def iter_sse(self, idle_timeout: float = 0.0) -> Iterator[sse.SSEEvent]:
        return sse.iter_sse(self.iter_text(idle_timeout))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pool.release(self._pool_key, self._conn, reusable=self._reusable())

    def _reusable(self) -> bool:
        if not self._consumed:
            return False
        return self.header("connection", "").lower() != "close" and self.status < 500


# --------------------------------------------------------------------------
# 连接池
# --------------------------------------------------------------------------


class _ConnPool:
    """按 (host, proxy, 凭据) 复用连接；每个连接同一时刻只给一个线程。

    闲置超过 ``IDLE_TTL`` 的连接直接丢掉：WAF 风暴之后上游会常态性掐闲置连接，
    复用一个已经被对端关闭的 socket 只会让请求卡在等首字节上。
    """

    IDLE_TTL = 90.0

    def __init__(self, max_idle: int = 2) -> None:
        self._idle: Dict[str, List[Tuple[http.client.HTTPConnection, float]]] = {}
        self._lock = threading.Lock()
        self._max_idle = max_idle

    def acquire(self, key: str) -> Optional[http.client.HTTPConnection]:
        with self._lock:
            bucket = self._idle.get(key)
            while bucket:
                conn, stamp = bucket.pop()
                if time.time() - stamp > self.IDLE_TTL or not _alive(conn):
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
                return conn
        return None

    def release(self, key: str, conn: http.client.HTTPConnection, reusable: bool = True) -> None:
        if not reusable:
            try:
                conn.close()
            except Exception:
                pass
            return
        with self._lock:
            bucket = self._idle.setdefault(key, [])
            if len(bucket) < self._max_idle:
                bucket.append((conn, time.time()))
                return
        try:
            conn.close()
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            for bucket in self._idle.values():
                for conn, _stamp in bucket:
                    try:
                        conn.close()
                    except Exception:
                        pass
            self._idle.clear()


def _alive(conn: http.client.HTTPConnection) -> bool:
    """连接是否还活着：对端已关闭时 socket 立刻可读（读到 EOF）。"""
    sock = getattr(conn, "sock", None)
    if sock is None:
        # 还没连过（或已被 http.client 关闭）的连接留着没坏处：真要用时它会自己重连
        return True
    try:
        readable, _w, _x = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return False
    return not readable


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class UpstreamClient:
    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self._pool = _ConnPool()
        self._ssl = self._build_ssl()

    # -- 配置 ---------------------------------------------------------------
    def _build_ssl(self) -> ssl.SSLContext:
        verify = True if self.cfg is None else bool(self.cfg.get("upstream.verify_tls", True))
        ca_bundle = "" if self.cfg is None else (self.cfg.get("upstream.ca_bundle", "") or "")
        if verify:
            context = ssl.create_default_context(cafile=ca_bundle or None)
        else:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def reload_ssl(self) -> None:
        self._ssl = self._build_ssl()

    def base_url(self, realm: str) -> str:
        if self.cfg is not None:
            configured = conf.realm_value(
                self.cfg, "upstream.hosts", identity.normalize_realm(realm), "") or ""
            if configured:
                return str(configured).rstrip("/")
        return identity.host_for(realm)

    def _timeouts(self) -> Tuple[float, float]:
        if self.cfg is None:
            return 120.0, 300.0
        return (
            float(self.cfg.get("upstream.timeout_seconds", 120)),
            float(self.cfg.get("upstream.idle_timeout_seconds", 300)),
        )

    # -- 连接 ---------------------------------------------------------------
    def _connect(self, url: str, proxy: str, timeout: float, credential: str = "") -> Tuple[http.client.HTTPConnection, str]:
        parts = util.split_url(url)
        scheme, host, port, _path = parts
        # 池键必须带凭据摘要：同一个 TCP 连接上先后出现两个 Authorization，
        # 在上游看就是"多开设备共用一条链路"，这是最直接的跨号关联线索。
        tag = hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16] if credential else "anon"
        key = f"{scheme}://{host}:{port}|{proxy}|{tag}"
        conn = self._pool.acquire(key)
        if conn is not None:
            return conn, key
        if proxy and scheme == "https":
            proxy_parts = util.split_url(proxy if "://" in proxy else "http://" + proxy)
            tunnel = http.client.HTTPSConnection(proxy_parts[1], proxy_parts[2], timeout=timeout, context=self._ssl)
            tunnel.set_tunnel(host, port)
            return tunnel, key
        if proxy and scheme == "http":
            proxy_parts = util.split_url(proxy if "://" in proxy else "http://" + proxy)
            return http.client.HTTPConnection(proxy_parts[1], proxy_parts[2], timeout=timeout), key
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=self._ssl), key
        return http.client.HTTPConnection(host, port, timeout=timeout), key

    # -- 请求 ---------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        *,
        raw_body: Optional[bytes] = None,
        proxy: str = "",
        timeout: Optional[float] = None,
        retries: int = 1,
    ) -> UpstreamResponse:
        # raw_body 是 post() 的同义参数：billing 先自己序列化好再发，必须与请求体字节一致，
        # 不能被二次 json.dumps（那会多一层字符串转义，上游直接当畸形请求）。
        if raw_body is not None:
            body = raw_body
        connect_timeout, _idle = self._timeouts()
        timeout = timeout or connect_timeout
        parts = util.split_url(url)
        path = parts[3] or "/"
        attempt = 0
        last: Optional[Exception] = None
        credential = ""
        if headers:
            credential = headers.get("Authorization") or headers.get("authorization") or headers.get("X-User-Id") or ""
        while attempt <= retries:
            attempt += 1
            conn = None
            key = ""
            reused = False
            try:
                conn, key = self._connect(url, proxy, timeout, credential)
                reused = getattr(conn, "sock", None) is not None
                conn.request(method, path, body=body, headers=headers or {})
                raw = conn.getresponse()
                response = UpstreamResponse(conn, raw, key, self._pool)
                if response.status >= 400:
                    text = response.text(65536)
                    raise classify(response.status, text, _retry_after(response.headers))
                return response
            except UpstreamError:
                raise
            except (http.client.HTTPException, ConnectionError, socket.timeout, OSError) as exc:
                last = exc
                if conn is not None:
                    self._pool.release(key, conn, reusable=False)
                # 超时不许重试：请求可能已经被上游处理完（真花了钱），重发就是重复生成。
                # 只对"复用的旧连接已经死了"这种情况重来一次，那时上游什么都没收到。
                retryable = reused and not isinstance(exc, socket.timeout)
                if attempt > retries or not retryable:
                    break
                log.debug("transport retry %s %s (%s)", method, url, exc)
                time.sleep(0.2 * attempt)
        if isinstance(last, socket.timeout):
            raise UpstreamError(f"upstream timeout: {last}", kind="timeout", status=0)
        raise UpstreamError(f"transport failure: {last}", kind="transient", status=0, retryable=True)

    def get(self, url: str, headers: Dict[str, str], **kwargs: Any) -> UpstreamResponse:
        return self.request("GET", url, headers, None, **kwargs)

    def post(
        self,
        url: str,
        headers: Dict[str, str],
        payload: Any = None,
        *,
        raw_body: Optional[bytes] = None,
        **kwargs: Any,
    ) -> UpstreamResponse:
        body = raw_body if raw_body is not None else (json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b"")
        return self.request("POST", url, headers, body, **kwargs)

    def close(self) -> None:
        self._pool.close()


def _retry_after(headers: Dict[str, str]) -> float:
    raw = headers.get("retry-after", "")
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0
