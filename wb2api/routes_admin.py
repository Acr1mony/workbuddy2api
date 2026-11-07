"""面板 API：账号、设置、用量、日志、调度。

设计取舍（与两个参考实现都不同）：

* **登录发令牌，不用 cookie 里的明文密码**。令牌是 HMAC 签名的 ``<exp>.<sig>``，
  放在 ``X-Panel-Token`` 头里（不放 query：query 会进 Referer 和访问日志）。
* **面板关闭时整个 ``/panel`` 与 ``/api`` 前缀 404**，不返回 401——一个不存在的
  路径比"需要密码"更少勾人去猜。
* **账号行永不含 token**。``Account.masked()`` 只给掩码与到期时间，UI 需要的
  信息全在里面，泄露面为零。
"""

from __future__ import annotations

import hmac
import time
from typing import Any, Dict, List, Optional

from . import identity, util
from .billing import Billing
from .httpserver import BadRequest, Response, Router, error_response, json_response
from .log import get_logger
from .probe import probe_account
from .service import ServiceError
from .pool import throttle_until
from .store import Account, ApiKey

log = get_logger("panel")

LOGIN_WINDOW = 60.0
LOGIN_MAX_ATTEMPTS = 5


class Panel:
    def __init__(self, service: Any, cfg: Any) -> None:
        from .httpserver import SessionAuth

        self.service = service
        self.cfg = cfg
        self.store = service.store
        self.auth = service.auth
        self.pool = service.pool
        self.billing = Billing(service.client, cfg)
        self.sessions = SessionAuth(self.store, ttl_seconds=float(cfg.get("panel.session_hours", 12)) * 3600)
        self._attempts: Dict[str, List[float]] = {}

    # -- 开关与鉴权 ---------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.cfg.get("panel.enabled"))

    def password(self) -> str:
        return str(self.cfg.get("panel.password") or "admin")

    def authenticated(self, req: Any) -> bool:
        token = req.header("x-panel-token") or req.arg("token")
        if not token:
            raw = req.header("cookie")
            for part in (raw or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "wb2a_panel":
                    token = value
                    break
        return self.sessions.verify(token)

    def require(self, req: Any) -> None:
        if not self.enabled():
            raise BadRequest("panel disabled", 404, "panel_disabled")
        if not self.authenticated(req):
            raise BadRequest("需要登录", 401, "unauthorized")

    def _throttled(self, ip: str) -> bool:
        now = time.time()
        history = [stamp for stamp in self._attempts.get(ip, []) if now - stamp < LOGIN_WINDOW]
        self._attempts[ip] = history
        return len(history) >= LOGIN_MAX_ATTEMPTS

    def _note_attempt(self, ip: str) -> None:
        self._attempts.setdefault(ip, []).append(time.time())


def register(router: Router, panel: Panel) -> None:
    # -- 登录 ---------------------------------------------------------------
    router.add("POST", r"/panel/login", lambda req: _login(panel, req))
    router.add("POST", r"/panel/logout", lambda req: _logout(panel, req))
    router.add("GET", r"/panel/status", lambda req: _status(panel, req))

    # -- 账号 ---------------------------------------------------------------
    router.add("GET", r"/api/accounts", lambda req: _accounts(panel, req))
    router.add("POST", r"/api/accounts/add", lambda req: _add_account(panel, req))
    router.add("POST", r"/api/accounts/set", lambda req: _set_account(panel, req))
    router.add("POST", r"/api/accounts/login/start", lambda req: _login_start(panel, req))
    router.add("GET", r"/api/accounts/login/poll", lambda req: _login_poll(panel, req))
    router.add(
        "POST",
        r"/api/accounts/(?P<uid>[^/]+)/(?P<action>enable|disable|delete|test|refresh|checkin|balance)",
        lambda req, uid, action: _account_action(panel, req, uid, action),
    )

    # -- 设置 / 用量 / 日志 / 调度 ------------------------------------------
    router.add("GET", r"/api/settings", lambda req: _settings_get(panel, req))
    router.add("POST", r"/api/settings", lambda req: _settings_post(panel, req))
    router.add("GET", r"/api/usage", lambda req: _usage(panel, req))
    router.add("GET", r"/api/usage/recent", lambda req: _usage_recent(panel, req))
    router.add("GET", r"/api/logs", lambda req: _logs(panel, req))
    router.add("GET", r"/api/pool", lambda req: _pool(panel, req))
    router.add("GET", r"/api/scheduler", lambda req: _scheduler(panel, req))
    router.add("POST", r"/api/scheduler/trigger", lambda req: _scheduler_trigger(panel, req))


# --------------------------------------------------------------------------
# 登录
# --------------------------------------------------------------------------


def _login(panel: Panel, req: Any) -> Response:
    if not panel.enabled():
        return error_response(404, "panel disabled", "panel_disabled")
    ip = req.client_ip or "?"
    if panel._throttled(ip):
        return error_response(429, "登录尝试过于频繁，请稍后再试", "too_many_attempts", kind="rate_limit_error")
    body = req.json_body()
    given = str(body.get("password") or "")
    if not hmac.compare_digest(given, panel.password()):
        panel._note_attempt(ip)
        log.warning("面板登录失败：%s", ip)
        return error_response(401, "密码错误", "bad_password")
    token, expires = panel.sessions.issue()
    # 同时发 cookie：前端把令牌放 localStorage（刷新方便），cookie 兜底那些
    # 直接用浏览器打开 /api/... 的场合。HttpOnly 是为了让 XSS 拿不到令牌。
    max_age = max(0, int(expires - time.time()))
    cookie = (
        f"{panel.sessions.COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
    )
    return json_response({"ok": True, "token": token, "expires_at": expires}, headers={"Set-Cookie": cookie})


def _logout(panel: Panel, req: Any) -> Response:
    cookie = f"{panel.sessions.COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
    return json_response({"ok": True}, headers={"Set-Cookie": cookie})


def _status(panel: Panel, req: Any) -> Response:
    keys = panel.store.list_keys()
    payload = {
        "enabled": panel.enabled(),
        "password_required": True,
        "is_default_password": panel.password() in ("admin", ""),
        "authenticated": panel.authenticated(req),
        "api_key_set": bool(panel.cfg.get("api_key")) or bool(keys),
        "version": _version(),
    }
    if not panel.enabled():
        return json_response({"enabled": False})
    return json_response(payload)


def _version() -> str:
    from . import VERSION

    return VERSION


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------


def _accounts(panel: Panel, req: Any) -> Response:
    panel.require(req)
    realm = identity.normalize_realm(req.arg("realm")) if req.arg("realm") else ""
    now = time.time()
    rows = []
    for account in panel.store.list_accounts(realm):
        row = account.masked()
        wait, window = throttle_until(account, "")
        row["throttle_for"] = int(max(0.0, (wait or 0) - now))
        row["throttle_reason"] = window
        row["in_flight"] = getattr(account, "in_flight", 0)
        rows.append(row)
    return json_response({"accounts": rows, "realm": realm or "all"})


def _add_account(panel: Panel, req: Any) -> Response:
    panel.require(req)
    body = req.json_body()
    realm = identity.normalize_realm(body.get("realm") or "cn")
    refresh_token = str(body.get("refresh_token") or "").strip()
    access_token = str(body.get("access_token") or body.get("token") or "").strip()
    if not refresh_token and not access_token:
        return error_response(400, "需要 refresh_token 或 access_token", "missing_token")
    account = Account(
        refresh_token=refresh_token,
        access_token=access_token,
        realm=realm,
        note=str(body.get("note") or ""),
        proxy=str(body.get("proxy") or ""),
        group=str(body.get("group") or ""),
    )
    account.ensure_ids()
    if not access_token:
        try:
            panel.auth.refresh(account, force=True)
        except Exception as exc:
            return error_response(400, f"用该 refresh_token 换令牌失败: {exc}", "refresh_failed")
    claimed = account.token_claims()
    account.uid = str(claimed.get("uid") or account.uid or "")
    account.enterprise_id = str(claimed.get("enterpriseId") or claimed.get("enterprise_id") or "")
    panel.store.upsert_account(account)
    log.info("新增账号 %s（%s）", account.label, realm)
    return json_response({"ok": True, "account": account.masked()})


def _set_account(panel: Panel, req: Any) -> Response:
    panel.require(req)
    body = req.json_body()
    account = panel.store.get_account(str(body.get("uid") or ""))
    if account is None:
        return error_response(404, "账号不存在", "unknown_account")
    for field, caster in (
        ("weight", float),
        ("note", str),
        ("group", str),
        ("proxy", str),
        ("enabled", bool),
    ):
        if field in body:
            try:
                setattr(account, field, caster(body[field]))
            except (TypeError, ValueError):
                return error_response(400, f"{field} 类型不对", "bad_field")
    account.updated_at = util.now()
    panel.store.upsert_account(account)
    return json_response({"ok": True, "account": account.masked()})


def _account_action(panel: Panel, req: Any, uid: str, action: str) -> Response:
    panel.require(req)
    account = panel.store.get_account(uid)
    if account is None:
        return error_response(404, "账号不存在", "unknown_account")

    if action == "enable":
        account.enabled = True
        panel.store.upsert_account(account)
        return json_response({"ok": True})
    if action == "disable":
        account.enabled = False
        panel.store.upsert_account(account)
        return json_response({"ok": True})
    if action == "delete":
        panel.store.remove_account(account.key)
        try:
            panel.service.affinity.drop(account.key)
        except Exception:  # 亲和表里没有也无所谓
            pass
        log.info("删除账号 %s", account.label)
        return json_response({"ok": True})
    if action == "test":
        return _test(panel, account)
    if action == "refresh":
        try:
            panel.auth.refresh(account, force=True)
        except Exception as exc:
            return error_response(400, f"刷新失败: {exc}", "refresh_failed")
        panel.store.upsert_account(account)
        return json_response({"ok": True, "expires_at": account.expires_at})
    if action == "checkin":
        ok, message = panel.billing.checkin(account, account.realm)
        return json_response({"ok": ok, "message": message})
    if action == "balance":
        value = panel.billing.credits(account, account.realm)
        if value is not None:
            account.credit = value
            account.credit_at = util.now()
            panel.store.upsert_account(account)
        return json_response({"ok": value is not None, "credits": value})
    return error_response(400, f"未知操作 {action}", "bad_action")


def _test(panel: Panel, account: Account) -> Response:
    """用一次最小请求确认账号真的能用（实现见 ``probe.py``，CLI 共用）。"""
    ok, detail = probe_account(panel.service, account)
    if ok:
        return json_response({"ok": True, "model": detail.get("model"), "sample": detail.get("sample", "")})
    return json_response({"ok": False, "model": detail.get("model"), "error": detail.get("error", "")})


def _login_start(panel: Panel, req: Any) -> Response:
    panel.require(req)
    body = req.json_body()
    realm = identity.normalize_realm(body.get("realm") or "cn")
    session = panel.auth.start_login(realm)
    return json_response({"ok": True, "state": session.state, "url": session.url, "realm": realm})


def _login_poll(panel: Panel, req: Any) -> Response:
    panel.require(req)
    state = req.arg("state")
    if not state:
        return error_response(400, "缺少 state", "missing_state")
    # poll_login 返回的是 dict（view() 的形状），不是 LoginSession：
    # 用属性访问会直接 500，而前端只看到"轮询失败"，很难定位。
    payload = dict(panel.auth.poll_login(state))
    status = str(payload.get("status") or "pending")
    payload["status"] = status
    if status == "ok":
        uid = payload.get("uid") or ""
        account = panel.store.get_account(uid) if uid else None
        payload["account"] = account.masked() if account is not None else None
    return json_response(payload)


# --------------------------------------------------------------------------
# 设置
# --------------------------------------------------------------------------


def _settings_get(panel: Panel, req: Any) -> Response:
    panel.require(req)
    cfg = panel.cfg
    return json_response(
        {
            "panel": {"enabled": cfg.get("panel.enabled"), "session_hours": cfg.get("panel.session_hours")},
            "api_keys": [key.masked() for key in panel.store.list_keys()],
            "default_api_key_set": bool(cfg.get("api_key")),
            "upstream": {
                "timeout_seconds": cfg.get("upstream.timeout_seconds"),
                "idle_timeout_seconds": cfg.get("upstream.idle_timeout_seconds"),
                "client_version": cfg.get("upstream.client_version"),
                "cli_version": cfg.get("upstream.cli_version"),
                "client_name": cfg.get("upstream.client_name"),
            },
            "pool": cfg.get("pool.strategy"),
            "prompt_mode": cfg.get("prompt.mode"),
            "listen": cfg.get("listen"),
        }
    )


def _normalize_key_items(value: Any) -> List[Dict[str, Any]]:
    """Accept a list, a single object, or a bare name.

    The panel sends a list, but a script writing `{"keys_add": {"name": "x"}}`
    or `{"keys_add": "x"}` should not turn into a 500 (`'str' object has no
    attribute 'get'`) — that is how a typo becomes an outage.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [{"name": value}]
    if isinstance(value, dict):
        return [value]
    items: List[Dict[str, Any]] = []
    for entry in value:
        if isinstance(entry, str):
            items.append({"name": entry})
        elif isinstance(entry, dict):
            items.append(entry)
    return items


def _delete_keys(panel: Panel, targets: Any) -> List[str]:
    """Delete by key value or by name; returns what actually went away."""
    if isinstance(targets, str):
        targets = [targets]
    removed: List[str] = []
    for raw in targets or []:
        needle = str(raw or "").strip()
        if not needle:
            continue
        if panel.store.get_key(needle) is not None:
            if panel.store.remove_key(needle):
                removed.append(needle)
            continue
        # Names are what the panel shows, so let a name mean "all of these".
        for key in panel.store.list_keys():
            if key.name == needle and panel.store.remove_key(key.key):
                removed.append(key.key)
    return removed


def _settings_post(panel: Panel, req: Any) -> Response:
    panel.require(req)
    body = req.json_body()
    cfg = panel.cfg
    if "panel_password" in body:
        password = str(body["panel_password"] or "")
        if len(password) < 6:
            return error_response(400, "密码至少 6 位", "weak_password")
        cfg.set("panel.password", password)
    if "prompt_mode" in body:
        mode = str(body["prompt_mode"])
        if mode not in ("passthrough", "sanitize"):
            return error_response(400, "prompt_mode 只能是 passthrough 或 sanitize", "bad_value")
        cfg.set("prompt.mode", mode)
    if "api_key" in body:
        cfg.set("api_key", str(body["api_key"] or ""))
    if "listen" in body:
        cfg.set("listen", str(body["listen"]))
    cfg.save()

    added: List[Dict[str, Any]] = []
    for item in _normalize_key_items(body.get("keys_add")):
        key = str(item.get("key") or util.rand_hex(24))
        models = item.get("models") or []
        if isinstance(models, str):
            models = [part.strip() for part in models.split(",") if part.strip()]
        created = ApiKey(
            key=key,
            name=str(item.get("name") or ""),
            realm=str(item.get("realm") or ""),
            models=[str(m) for m in models],
        )
        panel.store.add_key(created)
        # The plaintext key is returned exactly once — the panel cannot show it
        # again (it only ever holds the masked form).
        added.append({"name": created.name, "key": created.key, "realm": created.realm})
    deleted = _delete_keys(panel, body.get("keys_delete"))
    return json_response({"ok": True, "added": added, "deleted": deleted})


# --------------------------------------------------------------------------
# 用量 / 日志 / 调度
# --------------------------------------------------------------------------


def _usage(panel: Panel, req: Any) -> Response:
    panel.require(req)
    return json_response(panel.service.ledger.summary())


def _usage_recent(panel: Panel, req: Any) -> Response:
    panel.require(req)
    limit = max(1, min(500, int(req.arg("limit", "50") or 50)))
    return json_response({"items": panel.service.ledger.recent(limit)})


def _logs(panel: Panel, req: Any) -> Response:
    panel.require(req)
    limit = max(1, min(2000, int(req.arg("lines", "200") or 200)))
    return json_response({"lines": read_tail(panel.cfg.get("logging.dir") or "./logs", limit)})


def _pool(panel: Panel, req: Any) -> Response:
    panel.require(req)
    return json_response(panel.pool.stats())


def _scheduler(panel: Panel, req: Any) -> Response:
    panel.require(req)
    scheduler = getattr(panel.service, "scheduler", None)
    if scheduler is None:
        return json_response({"running": False, "jobs": []})
    return json_response(scheduler.snapshot())


def _scheduler_trigger(panel: Panel, req: Any) -> Response:
    panel.require(req)
    scheduler = getattr(panel.service, "scheduler", None)
    if scheduler is None:
        return error_response(404, "调度器未启动", "no_scheduler")
    name = req.json_body().get("job") or req.arg("job")
    ok = scheduler.trigger(str(name)) if name else scheduler.run_once()
    return json_response({"ok": ok})


def read_tail(directory: str, limit: int) -> List[str]:
    """读日志文件的最后 N 行（不把整个文件读进内存）。"""
    import os

    try:
        names = [name for name in os.listdir(directory) if name.endswith(".log")]
    except OSError:
        return []
    if not names:
        return []
    names.sort(key=lambda name: os.path.getmtime(os.path.join(directory, name)))
    path = os.path.join(directory, names[-1])
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            block = min(size, 256 * 1024)
            handle.seek(size - block)
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-limit:]
