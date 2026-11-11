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
import json
import random
import threading
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

# Bulk-capable actions, and the subset that talks to the upstream (those get
# spaced out — a batch is still a burst from one egress IP).
BULK_ACTIONS = ("enable", "disable", "pause", "resume", "test", "refresh", "balance", "checkin", "report")
SLOW_ACTIONS = ("test", "refresh", "balance", "checkin", "report")


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
        # 登录计数是"多线程一起写"的：不加锁会丢计数（限流形同虚设），
        # 不设上限则一个刷子能把字典撑到内存里（每 IP 一条，永不回收）。
        self._attempts_lock = threading.Lock()
        self._attempts_max_ips = 4096

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
        with self._attempts_lock:
            history = [stamp for stamp in self._attempts.get(ip, []) if now - stamp < LOGIN_WINDOW]
            if history:
                self._attempts[ip] = history
            else:
                self._attempts.pop(ip, None)
            return len(history) >= LOGIN_MAX_ATTEMPTS

    def _note_attempt(self, ip: str) -> None:
        with self._attempts_lock:
            self._attempts.setdefault(ip, []).append(time.time())
            if len(self._attempts) > self._attempts_max_ips:
                # 只保留最近在失败的那些 IP：被刷时宁可丢老记录也不能让字典无限长。
                keep = sorted(self._attempts.items(), key=lambda kv: kv[1][-1], reverse=True)
                self._attempts = dict(keep[: self._attempts_max_ips // 2])


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
    router.add("POST", r"/api/accounts/bulk", lambda req: _bulk_action(panel, req))
    router.add("GET", r"/api/accounts/export", lambda req: _export_accounts(panel, req))
    router.add("POST", r"/api/accounts/import", lambda req: _import_accounts(panel, req))
    # 单号详情放在 export/import 之后：路由器按注册顺序匹配，否则 "export" 会被
    # 当成一个 uid。
    router.add("GET", r"/api/accounts/(?P<uid>[^/]+)", lambda req, uid: _account_detail(panel, req, uid))
    router.add(
        "POST",
        r"/api/accounts/(?P<uid>[^/]+)/(?P<action>enable|disable|pause|resume|delete|test|refresh|checkin|balance|report)",
        lambda req, uid, action: _account_action(panel, req, uid, action),
    )

    # -- 设置 / 用量 / 日志 / 调度 ------------------------------------------
    router.add("GET", r"/api/settings", lambda req: _settings_get(panel, req))
    router.add("POST", r"/api/settings", lambda req: _settings_post(panel, req))
    router.add("GET", r"/api/usage", lambda req: _usage(panel, req))
    router.add("GET", r"/api/usage/recent", lambda req: _usage_recent(panel, req))
    router.add("GET", r"/api/logs", lambda req: _logs(panel, req))
    router.add("GET", r"/api/pool", lambda req: _pool(panel, req))
    router.add("GET", r"/api/metrics", lambda req: _metrics(panel, req))
    router.add("GET", r"/api/models", lambda req: _models(panel, req))
    router.add("GET", r"/api/credits/history", lambda req: _credit_history(panel, req))
    router.add("GET", r"/api/scheduler", lambda req: _scheduler(panel, req))
    router.add("POST", r"/api/scheduler/trigger", lambda req: _scheduler_trigger(panel, req))
    # -- 观测：逐请求明细 / 失败报文 / 出口池 --------------------------------
    router.add("GET", r"/api/requests", lambda req: _requests(panel, req))
    router.add("GET", r"/api/dumps", lambda req: _dumps(panel, req))
    router.add("GET", r"/api/dumps/(?P<request_id>[^/]+)", lambda req, request_id: _dump(panel, req, request_id))
    router.add(
        "GET",
        r"/api/dumps/(?P<request_id>[^/]+)/preview",
        lambda req, request_id: _dump_preview(panel, req, request_id),
    )
    router.add(
        "POST",
        r"/api/dumps/(?P<request_id>[^/]+)/replay",
        lambda req, request_id: _dump_replay(panel, req, request_id),
    )
    router.add("GET", r"/api/proxies", lambda req: _proxies(panel, req))
    router.add("POST", r"/api/proxies", lambda req: _proxies_post(panel, req))


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
    # compare_digest 只收"同类型字节串"或纯 ASCII 的 str：中文密码走 str 会抛
    # TypeError，表现是 500 且永远登不进去。统一 encode 再比。
    if not hmac.compare_digest(given.encode("utf-8"), panel.password().encode("utf-8")):
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


def _export_accounts(panel: Panel, req: Any) -> Response:
    """导出账号（含 refresh token）——备份用，代价是这份文件等同于账号密码。

    不做"脱敏导出"：脱敏后的备份 restore 不回来，等于假备份。所以老实导出，
    但用 ``Content-Disposition`` 让浏览器存成文件、``Cache-Control: no-store``
    防止被中间层缓存，并在文件里写一句人话警告。
    """
    panel.require(req)
    accounts = [account.to_dict() for account in panel.store.list_accounts("")]
    payload = {
        "version": 1,
        "exported_at": time.time(),
        "warning": "本文件含 refresh token，等同于账号密码。别提交到仓库，别贴到聊天软件里。",
        "accounts": accounts,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        200,
        body,
        headers={
            "Content-Disposition": 'attachment; filename="wb2api-accounts.json"',
            "Cache-Control": "no-store",
        },
    )


def _import_accounts(panel: Panel, req: Any) -> Response:
    """导入账号（merge 语义：同 uid 覆盖，其余保留）。

    ``delete`` 一类的破坏性合并故意不做：导入的用途是"换机器/重装后把号搬回来"，
    不是同步删除。
    """
    panel.require(req)
    body = req.json_body()
    items = body.get("accounts") if isinstance(body, dict) else body
    if not isinstance(items, list):
        return error_response(400, "需要 {\"accounts\": [...]} 或直接给数组", "bad_request")
    added = updated = skipped = 0
    for raw in items[:500]:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        try:
            account = Account.from_dict(raw)
        except Exception:  # 结构不认识的行跳过，别让一行坏数据毁掉整次导入
            skipped += 1
            continue
        if not (account.uid or account.access_token or account.refresh_token):
            skipped += 1
            continue
        account.ensure_ids()
        existed = bool(account.uid) and panel.store.get_account(account.uid) is not None
        panel.store.upsert_account(account)
        if existed:
            updated += 1
        else:
            added += 1
    log.info("导入账号：新增 %d，更新 %d，跳过 %d", added, updated, skipped)
    return json_response({"ok": True, "added": added, "updated": updated, "skipped": skipped})


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


def _account_detail(panel: Panel, req: Any, uid: str) -> Response:
    """单个账号的全貌：为什么现在不接单 + 它最近花掉了什么。

    列表页只有一行，真正的因果都在这里：冷却到几点、因为什么、熔断重试到第几档、
    哪些模型被单独封了、这个号今天消耗了多少积分、最近几次请求是成是败。
    """
    panel.require(req)
    account = panel.store.get_account(uid)
    if account is None:
        return error_response(404, "账号不存在", "unknown_account")
    now = time.time()
    wait, window = throttle_until(account, "")
    row = account.masked()
    row["throttle_for"] = int(max(0.0, (wait or 0) - now))
    row["throttle_reason"] = window
    row["in_flight"] = getattr(account, "in_flight", 0)

    summary = panel.service.ledger.summary()
    usage = dict(summary.get("by_account", {}).get(account.uid) or {})
    recent = [
        item
        for item in summary.get("recent") or []
        if str(item.get("account") or "") == account.uid
    ][:20]
    return json_response(
        {
            "account": row,
            "usage": usage,
            "recent": recent,
            "model_cooldowns": {
                model: {"until": until, "for": int(max(0.0, float(until or 0) - now))}
                for model, until in (row.get("model_cooldowns") or {}).items()
            },
            # "钱"的视角：包清单按到期日排好、最近 30 天的变动，以及净变化。
            "credits": _credit_view(panel, account),
        }
    )


def _credit_view(panel: Panel, account: Any) -> Dict[str, Any]:
    """余额的三个面：现在有多少、什么时候过期、最近怎么变的。"""
    buckets = dict(getattr(account, "credit_buckets", {}) or {})
    history = panel.service.credit_book.history(account.uid, 40) if hasattr(panel.service, "credit_book") else []
    delta = panel.service.credit_book.delta(account.uid, 7) if hasattr(panel.service, "credit_book") else {}
    return {
        "total": getattr(account, "credit", None),
        "read_at": getattr(account, "credit_at", 0.0) or 0.0,
        "expiring_soon": buckets.get("expiring_soon") or 0.0,
        "soonest_expire_at": buckets.get("soonest_expire_at") or 0.0,
        "buckets": buckets.get("buckets") or {},
        "package_count": buckets.get("package_count") or 0,
        "history": history,
        "delta": delta,
    }


def _credit_history(panel: Panel, req: Any) -> Response:
    """跨账号的余额变动：``?uid=&limit=&days=``。"""
    panel.require(req)
    uid = str(req.arg("uid") or "").strip()
    limit = int(max(1, min(1000, int(req.arg("limit", 120) or 120))))
    days = int(max(1, min(365, int(req.arg("days", 30) or 30))))
    rows = panel.service.credit_book.history(uid, limit) if hasattr(panel.service, "credit_book") else []
    # `days` 不是装饰：面板按"最近 N 天"取数，台账里可能有更老的样本。
    cutoff = time.time() - days * 86400
    rows = [row for row in rows if float(row.get("at") or 0) >= cutoff]
    return json_response({"items": rows, "uid": uid, "days": days})


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
    if action == "pause":
        account.paused = True
        account.state = "paused"
        panel.store.upsert_account(account)
        return json_response({"ok": True, "paused": True})
    if action == "resume":
        account.paused = False
        account.state = "ready"
        panel.store.upsert_account(account)
        return json_response({"ok": True, "paused": False})
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
        info = panel.service.refresh_credit(account, account.realm, note="panel")
        return json_response({
            "ok": bool(info.get("ok")),
            "credits": info.get("credits"),
            "expiring_soon": info.get("expiring_soon"),
            "buckets": info.get("buckets"),
            "soonest_expire_at": info.get("soonest_expire_at"),
            "package_count": info.get("package_count"),
            "packages": info.get("packages"),
        })
    if action == "report":
        if getattr(account, "enterprise_id", ""):
            return json_response({"ok": False, "message": "企业号没有活跃体系，上报是零收益"})
        ok = panel.billing.report(account, account.realm)
        if ok:
            account.extra["reported_at"] = util.now()
            panel.store.upsert_account(account)
        return json_response({"ok": ok, "message": "已上报" if ok else "上报失败（不影响任何请求）"})
    return error_response(400, f"未知操作 {action}", "bad_action")


def _bulk_action(panel: Panel, req: Any) -> Response:
    """Run one action over many accounts.

    The panel used to need one click per account to test/refresh a pool of
    forty. Doing that by hand is how people end up writing a shell loop that
    hits the upstreams all at once — so this walks the list one at a time and
    reports per-account results instead of failing the whole batch.
    """
    panel.require(req)
    body = req.json_body()
    action = str(body.get("action") or "")
    if action not in BULK_ACTIONS:
        return error_response(400, f"未知批量操作 {action}", "bad_action")
    uids = body.get("uids")
    if not isinstance(uids, list):
        uids = []
    if not uids and body.get("all"):
        realm = identity.normalize_realm(body.get("realm")) if body.get("realm") else ""
        uids = [account.uid for account in panel.store.list_accounts(realm)]
    uids = [str(uid) for uid in uids[:200]]
    if not uids:
        return error_response(400, "没有选中账号", "empty_selection")

    results: List[Dict[str, Any]] = []
    ok_count = 0
    for index, uid in enumerate(uids):
        account = panel.store.get_account(uid)
        if account is None:
            results.append({"uid": uid, "ok": False, "detail": "账号不存在"})
            continue
        # Network-bound actions get the same spacing the scheduler uses: a batch
        # of forty "test" calls fired in one millisecond is a burst from one IP.
        if action in SLOW_ACTIONS and index:
            time.sleep(random.uniform(0.8, 2.0))
        try:
            result = _bulk_one(panel, account, action)
        except Exception as exc:  # one bad account must not abort the batch
            result = {"uid": uid, "ok": False, "detail": str(exc)}
        result.setdefault("uid", uid)
        ok_count += 1 if result.get("ok") else 0
        results.append(result)

    return json_response(
        {"ok": ok_count == len(results), "action": action, "total": len(results), "ok_count": ok_count, "results": results}
    )


def _bulk_one(panel: Panel, account: Account, action: str) -> Dict[str, Any]:
    if action == "enable":
        account.enabled = True
        panel.store.upsert_account(account)
        return {"ok": True, "detail": "已启用"}
    if action == "disable":
        account.enabled = False
        panel.store.upsert_account(account)
        return {"ok": True, "detail": "已禁用"}
    if action == "pause":
        account.paused = True
        account.state = "paused"
        panel.store.upsert_account(account)
        return {"ok": True, "detail": "已暂停"}
    if action == "resume":
        account.paused = False
        account.state = "ready"
        panel.store.upsert_account(account)
        return {"ok": True, "detail": "已恢复"}
    if action == "test":
        ok, detail = probe_account(panel.service, account)
        note = detail.get("sample") or detail.get("error") or ""
        return {"ok": ok, "detail": note, "model": detail.get("model")}
    if action == "refresh":
        panel.auth.refresh(account, force=True)
        panel.store.upsert_account(account)
        return {"ok": True, "detail": "令牌已续期", "expires_at": account.expires_at}
    if action == "balance":
        info = panel.service.refresh_credit(account, account.realm, note="bulk")
        value = info.get("credits")
        # unknown is not zero: the account is fine, we just could not read it
        return {
            "ok": bool(info.get("ok")),
            "detail": "读不到余额" if value is None else f"{value:.2f}",
            "credits": value,
            "expiring_soon": info.get("expiring_soon"),
        }
    if action == "checkin":
        ok, message = panel.billing.checkin(account, account.realm)
        return {"ok": ok, "detail": message}
    if action == "report":
        if getattr(account, "enterprise_id", "") or getattr(account, "product", "") == "enterprise":
            return {"ok": False, "detail": "企业号没有活跃体系，上报是零收益"}
        try:
            panel.billing.report(account, account.realm)
        except Exception as exc:  # 单个号失败不该让整批停
            return {"ok": False, "detail": str(exc)[:120]}
        account.extra["reported_at"] = util.now()
        panel.store.upsert_account(account)
        return {"ok": True, "detail": "已上报"}
    return {"ok": False, "detail": f"批量不支持 {action}"}


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
            "prompt_append": cfg.get("prompt.append") or "",
            "listen": cfg.get("listen"),
            # Editable pool/schedule knobs. Values are strings-or-numbers exactly
            # as the config holds them (durations stay human: "30m", "200ms").
            "tunables": _tunables(panel),
        }
    )


# (config path, label, kind, restart_required)
# kind: "int" | "float" | "duration" | "str"
TUNABLES = (
    ("pool.max_in_flight", "同账号并发上限", "int", False),
    ("pool.max_in_flight_global", "global 域并发上限", "int", False),
    ("pool.credit_floor", "余额闸门（0=关闭；低于此值不发收费模型）", "float", False),
    ("pool.min_request_interval", "同账号最小请求间隔", "duration", False),
    ("pool.waf_cooldown", "WAF 软避让", "duration", False),
    ("pool.breaker_threshold", "熔断阈值（连续失败次数）", "int", False),
    ("pool.breaker_cooldown", "熔断首档时长", "duration", False),
    ("pool.restriction_hold_seconds", "整域暂停时长（触发风控连坐时）", "duration", False),
    ("pool.strategy.cn", "cn 域选号策略", "str", False),
    ("pool.strategy.global", "global 域选号策略", "str", False),
    ("session_sticky.ttl", "会话粘滞有效期", "duration", False),
    ("session_sticky.page_size", "一个会话在单号上最多跑几轮", "int", False),
    ("schedule.keepalive_minutes", "令牌保活间隔（分钟）", "int", False),
    ("schedule.balance_minutes", "余额刷新间隔（分钟）", "int", False),
    ("schedule.checkin_hour", "每日签到小时（0-23）", "int", False),
    ("upstream.timeout_seconds", "上游响应超时（秒）", "float", False),
    ("upstream.idle_timeout_seconds", "流式空闲超时（秒）", "float", False),
    ("server.max_payload_mb", "请求体上限（MB）", "float", True),
)


def _tunables(panel: Panel) -> List[Dict[str, Any]]:
    """Current values plus enough metadata for the panel to render inputs."""
    out: List[Dict[str, Any]] = []
    for path, label, kind, restart in TUNABLES:
        out.append(
            {
                "path": path,
                "label": label,
                "kind": kind,
                "restart": restart,
                "value": panel.cfg.get(path),
            }
        )
    return out


def _coerce_tunable(path: str, kind: str, raw: Any) -> Any:
    """Validate one tunable; raises ValueError with a human message."""
    if kind == "duration":
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            if raw <= 0:
                raise ValueError("时长必须大于 0")
            return f"{int(raw)}s"
        text = str(raw or "").strip()
        if not text:
            raise ValueError("时长不能为空")
        parsed = util.parse_duration(text, -1)
        if parsed is None or parsed < 0:
            raise ValueError(f"看不懂的时长：{text}（试 30s / 10m / 2h）")
        return text
    if kind == "int":
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError("需要整数")
        if value < 0:
            raise ValueError("不能是负数")
        if path == "schedule.checkin_hour" and value > 23:
            raise ValueError("小时需在 0-23 之间")
        return value
    if kind == "float":
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError("需要数字")
        if value < 0:
            raise ValueError("不能是负数")
        return value
    text = str(raw or "").strip()
    if path.startswith("pool.strategy.") and text not in ("weighted_random", "round_robin", "sequential_exhaust"):
        raise ValueError("策略只能是 weighted_random / round_robin / sequential_exhaust")
    return text


def _apply_tunables(panel: Panel, raw: Any) -> Optional[Response]:
    """Write ``{"tunables": {"pool.max_in_flight": 3, ...}}``; returns an error response on bad input."""
    if not isinstance(raw, dict):
        return None
    known = {path: (kind, restart) for path, _label, kind, restart in TUNABLES}
    cleaned: Dict[str, Any] = {}
    for path, value in raw.items():
        path = str(path)
        if path not in known:
            continue  # silently ignore unknown keys: forward compatibility
        kind, _restart = known[path]
        try:
            cleaned[path] = _coerce_tunable(path, kind, value)
        except ValueError as exc:
            return error_response(400, f"{path}：{exc}", "bad_value")
    for path, value in cleaned.items():
        panel.cfg.set(path, value)
    return None


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
    if "prompt_append" in body:
        # 网关自己的追加提示词：接在 system 末尾，不覆盖客户端写的那段。
        text = str(body["prompt_append"] or "")
        if len(text) > 4000:
            return error_response(400, "追加提示词最多 4000 字", "bad_value")
        cfg.set("prompt.append", text)
    if "api_key" in body:
        cfg.set("api_key", str(body["api_key"] or ""))
    if "listen" in body:
        cfg.set("listen", str(body["listen"]))
    if "tunables" in body:
        bad = _apply_tunables(panel, body["tunables"])
        if bad is not None:
            return bad
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
            max_concurrency=max(0, int(item.get("max_concurrency") or 0)),
            rate_limit=max(0, int(item.get("rate_limit") or 0)),
        )
        panel.store.add_key(created)
        # The plaintext key is returned exactly once — the panel cannot show it
        # again (it only ever holds the masked form).
        added.append({
            "name": created.name,
            "key": created.key,
            "realm": created.realm,
            "max_concurrency": created.max_concurrency,
            "rate_limit": created.rate_limit,
        })
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
    stats = panel.pool.stats()
    # 会话亲和也是池的一部分：命中率低说明粘滞没起作用（TTFT 会很差），
    # rotations 高说明有人在长会话里不停换页。
    stats["affinity"] = panel.service.affinity.stats()
    # WAF 波次：多个号在一分钟内都被边缘拦过，说明问题在出口而不在账号。
    stats["waf"] = {realm: panel.service.waf_wave(realm) for realm in ("cn", "global")}
    return json_response(stats)


def _metrics(panel: Panel, req: Any) -> Response:
    panel.require(req)
    return json_response(panel.service.metrics.snapshot())


def _models(panel: Panel, req: Any) -> Response:
    """配 key 时用的模型清单：两个域合在一起，让"限定模型"能做成多选。"""
    panel.require(req)
    return json_response(panel.service.catalog_view(req.arg("realm") or ""))


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


# --------------------------------------------------------------------------
# 观测：逐请求明细 / 失败报文 / 出口池
# --------------------------------------------------------------------------


def _require_writable(panel: Panel, req: Any) -> None:
    """只读面板：能看不能改（给"只想给同事看状态"的部署用）。"""
    if bool(panel.cfg.get("panel.readonly", False)) if panel.cfg else False:
        raise BadRequest("面板处于只读模式", 403, "readonly")


def _requests(panel: Panel, req: Any) -> Response:
    panel.require(req)
    limit = int(req.arg("limit", "200") or 200)
    rows = panel.service.reqlog.recent(
        limit,
        status=req.arg("status", ""),
        model=req.arg("model", ""),
        account=req.arg("account", ""),
        query=req.arg("q", ""),
    )
    return json_response({"items": rows, "stats": panel.service.reqlog.stats()})


def _dumps(panel: Panel, req: Any) -> Response:
    panel.require(req)
    limit = int(req.arg("limit", "50") or 50)
    return json_response({"items": panel.service.diag.recent(limit), "stats": panel.service.diag.stats()})


def _dump(panel: Panel, req: Any, request_id: str) -> Response:
    panel.require(req)
    record = panel.service.diag.get(request_id)
    if record is None:
        raise BadRequest("没有这条失败报文（可能已过期或从未落盘）", 404, "unknown_dump")
    return json_response(record)


def _dump_preview(panel: Panel, req: Any, request_id: str) -> Response:
    """净化前后对照：只做本地翻译，一个字节都不发上游。"""
    from . import replay as replay_mod
    from .errors import ServiceError

    panel.require(req)
    try:
        data = replay_mod.preview(panel.service, request_id, req.arg("realm", ""), req.arg("model", ""))
    except ServiceError as exc:
        return error_response(exc.status, exc.message, exc.code)
    return json_response(data)


def _dump_replay(panel: Panel, req: Any, request_id: str) -> Response:
    """照着实发一次。默认沿用当前 prompt 模式；``sanitize`` 可显式覆盖。"""
    from . import replay as replay_mod
    from .errors import ServiceError

    panel.require(req)
    _require_writable(panel, req)
    body = req.json_body()
    body = body if isinstance(body, dict) else {}
    sanitize = body.get("sanitize")
    try:
        result = replay_mod.replay(
            panel.service,
            request_id,
            sanitize=bool(sanitize) if sanitize is not None else None,
            realm=str(body.get("realm") or ""),
            model=str(body.get("model") or ""),
            request=req,
        )
    except ServiceError as exc:
        return error_response(exc.status, exc.message, exc.code)
    return json_response(result)


def _proxies(panel: Panel, req: Any) -> Response:
    panel.require(req)
    stats = panel.service.proxies.stats(req.arg("realm", ""))
    stats["enabled"] = panel.service.proxies.enabled()
    stats["realm"] = req.arg("realm", "")
    return json_response(stats)


def _proxies_post(panel: Panel, req: Any) -> Response:
    panel.require(req)
    _require_writable(panel, req)
    body = req.json_body()
    if not isinstance(body, dict):
        raise BadRequest("请求体必须是 JSON 对象", 400, "bad_request")
    action = str(body.get("action") or "add").strip().lower()
    pool = panel.service.proxies
    if action == "add":
        url = str(body.get("url") or "").strip()
        if not url:
            raise BadRequest("缺少 url", 400, "missing_url")
        added = pool.add(url, realm=str(body.get("realm") or ""), weight=body.get("weight", 1.0), note=str(body.get("note") or ""))
        if not added:
            raise BadRequest("这个出口已经在池里了（或者地址不合法）", 400, "duplicate_proxy")
    elif action in ("remove", "delete"):
        url = str(body.get("url") or "").strip()
        if not pool.remove(url):
            raise BadRequest("池里没有这个出口", 404, "unknown_proxy")
    elif action in ("update", "set"):
        url = str(body.get("url") or "").strip()
        if not pool.update(url, **{k: v for k, v in body.items() if k not in ("action", "url")}):
            raise BadRequest("池里没有这个出口", 404, "unknown_proxy")
    elif action == "reset":
        pool.reset_health()
    else:
        raise BadRequest("未知操作：%s" % action, 400, "bad_action")
    # 池子的真源是配置：改完就写回去，否则重启就回到旧列表。
    try:
        panel.cfg.set("pool.proxies", pool.to_config())
        panel.cfg.save()
    except Exception:
        log.debug("保存出口池配置失败", exc_info=True)
    stats = pool.stats("")
    stats["enabled"] = pool.enabled()
    return json_response(stats)
