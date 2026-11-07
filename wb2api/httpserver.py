"""A small HTTP framework on top of ``http.server``.

Why not Flask/FastAPI: the whole point of this gateway is that it starts with a
stock Python install, on a NAS or a router, with no wheel downloads.  The subset
implemented here is deliberately tiny:

* ``Router``      - ``(method, /regex/)`` -> handler, path params as kwargs
* ``Request``     - parsed path/query/headers/body, JSON with a size cap
* ``Response``    - JSON / bytes / SSE stream (chunked or close-delimited)
* ``SessionAuth`` - HMAC-signed admin cookie, no server-side session store

Anything the gateway needs beyond this (routing, streaming, cookies, range
requests) is implemented here once instead of being re-invented per module.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import socket
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, List, Optional, Pattern, Tuple

from . import util
from .log import get_logger

log = get_logger("http")

JSON_TYPE = "application/json; charset=utf-8"
TEXT_TYPE = "text/plain; charset=utf-8"


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Response:
    """A single HTTP response; ``stream`` turns it into a chunked body."""

    __slots__ = ("status", "body", "headers", "content_type", "stream", "close")

    def __init__(
        self,
        status: int = 200,
        body: Any = b"",
        headers: Optional[Dict[str, str]] = None,
        content_type: str = JSON_TYPE,
        stream: Optional[Iterable[bytes]] = None,
        close: bool = False,
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self.content_type = content_type
        self.stream = stream
        self.close = close
        if stream is not None:
            self.body = b""
        elif isinstance(body, (dict, list)):
            self.body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            self.body = body.encode("utf-8")
        else:
            self.body = body or b""


def json_response(payload: Any, status: int = 200, headers: Optional[Dict[str, str]] = None) -> Response:
    return Response(status=status, body=payload, headers=headers)


def error_response(
    status: int,
    message: str,
    code: str = "",
    *,
    kind: str = "error",
    headers: Optional[Dict[str, str]] = None,
) -> Response:
    """OpenAI-compatible error envelope (clients parse ``error.message``)."""
    body = {
        "error": {
            "message": message,
            "type": kind,
            "code": code or None,
        }
    }
    return Response(status=status, body=body, headers=dict(headers or {}))


def stream_response(frames: Iterable[bytes], status: int = 200, headers: Optional[Dict[str, str]] = None) -> Response:
    merged = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    merged.update(headers or {})
    return Response(status=status, stream=frames, content_type="text/event-stream; charset=utf-8", headers=merged)


# --------------------------------------------------------------------------
# request
# --------------------------------------------------------------------------


class Request:
    def __init__(
        self,
        method: str,
        path: str,
        query: Dict[str, List[str]],
        headers: "Any",
        body: bytes,
        client: Tuple[str, int],
        params: Optional[Dict[str, str]] = None,
    ) -> None:
        self.method = method.upper()
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body or b""
        self.client = client
        self.params = params or {}
        self.state: Dict[str, Any] = {}

    # -- query / headers ----------------------------------------------------
    def arg(self, name: str, default: str = "") -> str:
        values = self.query.get(name)
        return values[0] if values else default

    def arg_bool(self, name: str, default: bool = False) -> bool:
        raw = self.arg(name).strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on"}

    def header(self, name: str, default: str = "") -> str:
        return self._lower_headers().get(name.lower(), default) or default

    def _lower_headers(self) -> Dict[str, str]:
        """大小写不敏感的视图。header 名到处大小写混用，漏一次就是"鉴权静默失效"。"""
        cached = getattr(self, "_headers_lc", None)
        if cached is None:
            cached = {}
            try:
                items = self.headers.items()
            except AttributeError:  # 已经是普通 dict 且键大小写不一
                items = self.headers.items() if hasattr(self.headers, "items") else []
            for key, value in items:
                cached[str(key).lower()] = value
            self._headers_lc = cached
        return cached

    def bearer(self) -> str:
        """Extract a bearer token from ``Authorization`` or ``x-api-key``."""
        raw = self.header("authorization")
        if raw:
            parts = raw.split(None, 1)
            if len(parts) == 2 and parts[0].lower() in {"bearer", "token"}:
                return parts[1].strip()
            return raw.strip()
        return self.header("x-api-key").strip()

    @property
    def client_ip(self) -> str:
        forwarded = self.header("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client[0] if self.client else ""

    # -- body ---------------------------------------------------------------
    def json_body(self, max_bytes: int = 16 * 1024 * 1024) -> Dict[str, Any]:
        if len(self.body) > max_bytes:
            raise BadRequest(f"request body exceeds {max_bytes} bytes", 413, "payload_too_large")
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BadRequest(f"invalid JSON body: {exc}", 400, "invalid_json") from exc
        if not isinstance(data, dict):
            raise BadRequest("request body must be a JSON object", 400, "invalid_json")
        return data


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------


class Router:
    def __init__(self) -> None:
        self._routes: List[Tuple[str, Pattern[str], Callable[..., Any], str]] = []

    def add(self, method: str, pattern: str, handler: Callable[..., Any]) -> None:
        self._routes.append((method.upper(), re.compile(f"^{pattern}$"), handler, pattern))

    def route(self, method: str, pattern: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.add(method, pattern, func)
            return func

        return decorator

    def match(self, method: str, path: str) -> Tuple[Optional[Callable[..., Any]], Dict[str, str], int]:
        allowed = 0
        for route_method, regex, handler, _pattern in self._routes:
            found = regex.match(path)
            if not found:
                continue
            allowed += 1
            if route_method == method or (route_method == "GET" and method == "HEAD"):
                return handler, found.groupdict(), 200
        if allowed:
            return None, {}, 405
        return None, {}, 404

    def routes(self) -> List[Tuple[str, str]]:
        return [(method, pattern) for method, _regex, _handler, pattern in self._routes]


# --------------------------------------------------------------------------
# admin session cookie
# --------------------------------------------------------------------------


class SessionAuth:
    """Signed, stateless cookie: ``<exp>.<hmac>``.

    The signing key lives in ``runtime.json`` (generated on first boot) so a
    restart does not log the operator out, while a data-dir copy on another
    machine cannot forge cookies.
    """

    COOKIE = "wb2a_panel"

    def __init__(self, store: Any, ttl_seconds: float = 12 * 3600) -> None:
        self.store = store
        self.ttl = float(ttl_seconds)
        secret = store.runtime.get("panel_secret")
        if not secret:
            secret = util.rand_hex(64)
            store.runtime["panel_secret"] = secret
            store.save_runtime()
        self._secret = secret.encode("utf-8")

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def issue(self) -> Tuple[str, int]:
        expires = int(time.time() + self.ttl)
        payload = f"{expires}"
        token = base64.urlsafe_b64encode(f"{payload}.{self._sign(payload)}".encode("utf-8")).decode("ascii").rstrip("=")
        return token, expires

    def verify(self, token: Optional[str]) -> bool:
        if not token:
            return False
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
            payload, signature = raw.rsplit(".", 1)
            if not hmac.compare_digest(signature, self._sign(payload)):
                return False
            return int(payload) > time.time()
        except Exception:
            return False


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], app: "Application") -> None:
        self.app = app
        super().__init__(address, GatewayHandler)
        self.request_queue_size = 128

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc_type, exc, _tb = sys.exc_info()
        if exc_type in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            log.debug("client %s disconnected: %s", client_address, exc_type.__name__)
            return
        log.exception("unhandled error for %s: %s", client_address, exc)


class Application:
    """Holds the router, shared services and per-request context."""

    def __init__(self, router: Router, *, service: Any = None) -> None:
        self.router = router
        self.service = service

    def dispatch(self, req: Request) -> Response:
        handler, params, state = self.router.match(req.method, req.path)
        if handler is None:
            if state == 405:
                return error_response(405, f"method {req.method} not allowed for {req.path}", "method_not_allowed")
            return error_response(404, f"no route for {req.path}", "not_found")
        req.params = params
        try:
            result = handler(req, **params)
        except BadRequest as exc:
            return error_response(exc.status, exc.message, exc.code)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as exc:  # 入口层兜底：handler 的 bug 不该炸掉连接
            log.exception("handler %s failed: %s", handler, exc)
            return error_response(500, "internal error", "internal_error")
        if isinstance(result, Response):
            return result
        if isinstance(result, (dict, list, str, bytes)):
            return Response(body=result)
        raise RuntimeError(f"handler {handler} returned {type(result)!r}")


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "workbuddy2api"
    sys_version = ""
    # Suppress the default stderr access log; the app logs requests itself.
    _quiet = True

    # -- plumbing -----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _read_body(self, max_bytes: int) -> bytes:
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            size = int(length)
        except ValueError:
            raise BadRequest("invalid Content-Length", 400, "bad_length")
        if size < 0:
            raise BadRequest("invalid Content-Length", 400, "bad_length")
        if size > max_bytes:
            raise BadRequest(f"request body exceeds {max_bytes} bytes", 413, "payload_too_large")
        if size == 0:
            return b""
        return self.rfile.read(size)

    def _parse(self) -> Request:
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        max_bytes = int(getattr(self.server.app.service, "max_payload_bytes", 16 * 1024 * 1024))
        body = self._read_body(max_bytes)
        return Request(
            method=self.command,
            path=urllib.parse.unquote(parsed.path),
            query=query,
            headers=self.headers,
            body=body,
            client=self.client_address,
        )

    def _write(self, resp: Response) -> None:
        try:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, x-api-key, anthropic-version")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            for key, value in resp.headers.items():
                self.send_header(key, value)
            streaming = resp.stream is not None
            if streaming:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(resp.body)))
            if resp.close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()

            if streaming:
                self._write_chunked(resp.stream or [])
            elif resp.body:
                self.wfile.write(resp.body)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise
        except OSError as exc:
            log.debug("write failed: %s", exc)

    def _write_chunked(self, frames: Iterable[bytes]) -> None:
        count = 0
        try:
            for frame in frames:
                if not frame:
                    continue
                self.wfile.write(f"{len(frame):X}\r\n".encode("ascii"))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                count += 1
        except (BrokenPipeError, ConnectionResetError):
            raise
        finally:
            try:
                if count and not self.close_connection:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    # -- verbs --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def do_OPTIONS(self) -> None:  # noqa: N802
        resp = Response(status=204, body=b"", content_type=TEXT_TYPE)
        resp.headers["Content-Length"] = "0"
        self._write(resp)

    def _handle(self) -> None:
        app: Application = self.server.app  # type: ignore[attr-defined]
        try:
            req = self._parse()
        except BadRequest as exc:
            self._write(error_response(exc.status, exc.message, exc.code))
            return
        try:
            resp = app.dispatch(req)
        except BadRequest as exc:
            resp = error_response(exc.status, exc.message, exc.code)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("dispatch failed for %s %s", req.method, req.path)
            resp = error_response(500, f"internal error: {exc}", "internal_error")
        self._write(resp)


def serve(app: Application, host: str, port: int) -> GatewayServer:
    server = GatewayServer((host, port), app)
    log.info("listening on http://%s:%s", host, port)
    return server
