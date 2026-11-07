"""上游登录与令牌刷新。

两条链路：

* **浏览器登录（设备流，无 PKCE）** —— 网关拿一个 ``state`` 给用户，用户在浏览器里
  完成登录，网关轮询 ``/v2/plugin/auth/token`` 取回令牌。``state`` 是上游签发的，
  网关不构造也不解析 ``authUrl``。
* **刷新** —— ``POST /v2/plugin/auth/token/refresh``，令牌放在 ``X-Refresh-Token``
  头里，body 为空。刷新令牌是**一次性的**：并发刷新会让第二次请求带着已消费的
  refresh token 上去，上游返回 12153 并废掉整条链。所以这里强制单飞。

错误语义直接决定账号是被"冷却"还是被"判死"，见 :func:`classify_refresh_failure`。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from . import identity, util
from .log import get_logger

log = get_logger("auth")

STATE_PATH = "/v2/plugin/auth/state"
TOKEN_PATH = "/v2/plugin/auth/token"
ACCOUNT_PATH = "/v2/plugin/login/account"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"

LOGIN_PENDING = 11217  # 上游"还没扫码"的业务码，不是错误
REFRESH_TOKEN_DEAD = 12153  # 刷新令牌已被消费/失效
STATE_TTL = 600.0  # state 有效期（秒）
DEFAULT_EXPIRES_IN = 3600.0
MAX_EXPIRES_IN = 400 * 86400.0  # 上游偶尔回 10 年，明显是垃圾值，不采信


@dataclass
class LoginSession:
    """一次进行中的浏览器登录。"""

    state: str
    realm: str
    url: str
    created_at: float = field(default_factory=util.now)
    status: str = "pending"  # pending | ok | error
    message: str = ""
    uid: str = ""

    @property
    def expired(self) -> bool:
        return util.now() - self.created_at > STATE_TTL

    def view(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "realm": self.realm,
            "url": self.url,
            "status": self.status,
            "message": self.message,
            "uid": self.uid,
            "expires_in": max(0, int(STATE_TTL - (util.now() - self.created_at))),
        }


class LoginError(Exception):
    pass


class AuthFlow:
    def __init__(self, client: Any, store: Any, cfg: Any) -> None:
        self.client = client  # upstream.UpstreamClient
        self.store = store
        self.cfg = cfg
        self._sessions: Dict[str, LoginSession] = {}
        self._lock = threading.Lock()
        self._refresh_locks = util.KeyedLock()

    # -- 登录 ---------------------------------------------------------------
    def start_login(self, realm: str = "cn", proxy: str = "") -> LoginSession:
        """向上游申请一个 state，返回用户需要打开的 URL。"""
        realm = identity.normalize_realm(realm)
        url = identity.host_for(realm) + STATE_PATH + "?platform=CLI"
        headers = identity.anonymous_headers(realm, self.cfg)
        try:
            resp = self.client.post(url, headers, {}, proxy=proxy)
            payload = resp.json()
        except Exception as exc:  # 网络/解析错误
            raise LoginError(f"无法向上游申请登录 state：{exc}") from exc
        if payload.get("code") not in (0, "0", None):
            raise LoginError(f"上游拒绝登录请求：{payload.get('msg') or payload.get('code')}")
        data = payload.get("data") or {}
        state = str(data.get("state") or "")
        auth_url = str(data.get("authUrl") or "")
        if not state or not auth_url:
            raise LoginError("上游没有返回 state/authUrl，登录接口可能已变更")
        session = LoginSession(state=state, realm=realm, url=auth_url)
        with self._lock:
            self._gc_sessions()
            self._sessions[state] = session
        log.info("登录已发起 realm=%s state=%s", realm, state[:8])
        return session

    def poll_login(self, state: str, proxy: str = "") -> Dict[str, Any]:
        """轮询登录结果；成功时直接落盘账号并返回 masked 视图。"""
        session = self._sessions.get(state)
        if session is None:
            return {"status": "error", "message": "登录会话不存在或网关已重启，请重新发起登录"}
        if session.status == "ok":
            return session.view()
        if session.expired:
            session.status = "error"
            session.message = "登录超时（10 分钟），请重新发起"
            return session.view()

        url = identity.host_for(session.realm) + TOKEN_PATH + "?state=" + util.quote(state)
        headers = identity.anonymous_headers(session.realm, self.cfg)
        try:
            resp = self.client.get(url, headers, proxy=proxy)
            payload = resp.json()
        except Exception as exc:
            return {"status": "pending", "message": f"等待中（{exc}）", "state": state}

        code = payload.get("code")
        if code == LOGIN_PENDING:
            session.message = payload.get("msg") or "等待浏览器完成登录"
            return session.view()
        if code not in (0, "0", None) or not isinstance(payload.get("data"), dict):
            session.message = str(payload.get("msg") or f"登录失败（code={code}）")
            # 其它业务码既可能是"还没好"，也可能是真失败：保持 pending 让用户重试，
            # 只有拿到明确错误文案时才把消息透出去。
            return session.view()

        tokens = payload["data"]
        account, error = self._account_from_tokens(tokens, session.realm, proxy)
        if error:
            session.status = "error"
            session.message = error
            return session.view()

        self.store.upsert_account(account)
        session.status = "ok"
        session.uid = account.uid
        session.message = "登录成功"
        log.info("登录成功 realm=%s uid=%s", session.realm, account.uid)
        return session.view()

    def cancel_login(self, state: str) -> bool:
        with self._lock:
            return self._sessions.pop(state, None) is not None

    def _account_from_tokens(
        self, tokens: Dict[str, Any], realm: str, proxy: str = ""
    ) -> tuple:
        access = str(tokens.get("accessToken") or tokens.get("access_token") or "")
        refresh = str(tokens.get("refreshToken") or tokens.get("refresh_token") or "")
        if not access:
            return None, "上游没有返回 accessToken"
        claims = util.jwt_claims(access)
        uid = str(
            claims.get("sub")
            or claims.get("uid")
            or claims.get("userId")
            or tokens.get("uid")
            or ""
        )
        if not uid:
            # 没有 uid 就没法做稳定指纹，也没法去重；这种账号不能入库。
            return None, "无法从令牌中解析出账号 uid（上游可能改了令牌格式）"
        account = self.store.get_account(uid) or _new_account(uid)
        account.realm = realm
        account.access_token = access
        account.refresh_token = refresh or account.refresh_token
        account.static_key = access.startswith(("ck_", "pt_"))
        account.expires_at = _expiry_from(tokens, claims)
        account.domain = str(tokens.get("domain") or account.domain or identity.realm_info(realm)["domain"])
        account.enterprise_id = str(tokens.get("enterpriseId") or claims.get("enterpriseId") or account.enterprise_id or "")
        account.source = "oauth"
        account.state = "ready"
        account.cooldown_until = 0.0
        account.cooldown_reason = ""
        account.cooldown_level = ""
        account.failures = 0
        account.last_error = ""
        account.updated_at = util.now()
        if proxy:
            account.proxy = proxy
        if not account.added_at:
            account.added_at = util.now()

        # 昵称/企业信息是"尽力而为"：拿不到就用 uid 前缀，绝不让它阻塞登录。
        try:
            info_url = identity.host_for(realm) + ACCOUNT_PATH + "?state=" + util.quote(str(tokens.get("state") or ""))
            headers = identity.chat_headers(account, self.cfg)
            info = self.client.get(info_url, headers).json()
            data = info.get("data") or {}
            if isinstance(data, dict):
                account.nickname = str(data.get("nickname") or account.nickname or "")
                account.email = str(data.get("email") or account.email or "")
                account.enterprise_id = str(data.get("enterpriseId") or account.enterprise_id or "")
        except Exception:
            pass
        if not account.nickname:
            account.nickname = uid[:8]
        return account, ""

    # -- 刷新 ---------------------------------------------------------------
    def refresh(self, account: Any, *, force: bool = False, proxy: str = "") -> bool:
        """刷新一个账号的 access token。单飞；失败时把错误写回账号。

        返回 True 表示令牌可用（可能并没真的刷新）。
        """
        if account is None:
            return False
        if getattr(account, "static_key", False):
            return bool(account.access_token)  # 静态密钥不可刷新，也永不过期
        if not account.refresh_token:
            self._mark_dead(account, "没有 refresh token，需要重新登录")
            return False
        if not force and not account.token_expired(skew=300):
            return True

        with self._refresh_locks(account.key):
            # 双重检查：等锁期间别的线程可能已经刷新过了。
            fresh = self.store.get_account(account.uid) or account
            if not force and not fresh.token_expired(skew=300):
                return True
            return self._do_refresh(fresh, proxy=proxy or getattr(fresh, "proxy", "") or "")

    def _do_refresh(self, account: Any, proxy: str = "") -> bool:
        url = identity.host_for(account.realm) + REFRESH_PATH
        headers = identity.refresh_headers(account, self.cfg)
        try:
            resp = self.client.post(url, headers, {}, proxy=proxy)
            payload = resp.json()
        except Exception as exc:
            # 网络抖动 / 5xx：这是暂时性故障，账号留着别动，下次再刷。
            account.last_error = f"刷新失败（网络）：{exc}"
            account.failures += 1
            self.store.upsert_account(account)
            return False

        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]  # 偶发双层信封
        data = data if isinstance(data, dict) else {}
        access = str(data.get("accessToken") or data.get("access_token") or "")
        if payload.get("code") not in (0, "0", None) or not access:
            message = str(payload.get("msg") or "")
            code = payload.get("code")
            if _is_dead_refresh(payload, message):
                self._mark_dead(account, f"刷新令牌已失效（code={code}）：{message or '需要重新登录'}")
            else:
                account.last_error = f"刷新失败：{message or code}"
                account.failures += 1
                self.store.upsert_account(account)
                log.warning("刷新失败 uid=%s code=%s msg=%s", account.uid, code, message)
            return False

        account.access_token = access
        new_refresh = str(data.get("refreshToken") or data.get("refresh_token") or "")
        if new_refresh:
            account.refresh_token = new_refresh
        claims = util.jwt_claims(access)
        account.expires_at = _expiry_from(data, claims) or account.expires_at
        domain = str(data.get("domain") or "")
        if domain:
            account.domain = domain
        account.last_error = ""
        account.failures = 0
        if account.state == "auth_dead":
            account.state = "ready"
        account.cooldown_until = 0.0
        account.cooldown_reason = ""
        account.cooldown_level = ""
        account.updated_at = util.now()
        self.store.upsert_account(account)
        log.info("令牌已刷新 uid=%s expires=%s", account.uid, util.iso(account.expires_at))
        return True

    def ensure_token(self, account: Any, *, proxy: str = "") -> bool:
        """调用上游前调用：过期就刷，刷新失败返回 False。"""
        if getattr(account, "static_key", False):
            return bool(account.access_token)
        if not account.token_expired(skew=180):
            return bool(account.access_token)
        return self.refresh(account, proxy=proxy)

    # -- 内部 ---------------------------------------------------------------
    def _mark_dead(self, account: Any, reason: str) -> None:
        account.state = "auth_dead"
        account.last_error = reason
        account.cooldown_level = "auth"
        account.cooldown_reason = reason
        account.cooldown_until = util.now() + util.parse_duration(
            self.cfg.get("cooldown.auth_dead", "24h") if self.cfg else "24h"
        )
        account.updated_at = util.now()
        self.store.upsert_account(account)
        log.warning("账号判死 uid=%s：%s", account.uid, reason)

    def _gc_sessions(self) -> None:
        for key in [k for k, s in self._sessions.items() if s.expired and s.status != "pending"]:
            self._sessions.pop(key, None)
        if len(self._sessions) > 64:  # 兜底：别让泄漏的 state 撑爆内存
            for key in list(self._sessions)[: len(self._sessions) - 64]:
                self._sessions.pop(key, None)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _new_account(uid: str) -> Any:
    from .store import Account  # 延迟导入，避免循环

    return Account.from_dict({"uid": uid, "id": uid})


def _expiry_from(tokens: Dict[str, Any], claims: Dict[str, Any]) -> float:
    """上游给的时间戳可能是秒也可能是毫秒，还可能是 expiresIn；取最早的可信值。"""
    raw = tokens.get("expiresAt") or tokens.get("expires_at")
    expires = util.normalize_epoch(raw)
    if expires:
        return expires
    claim_exp = util.normalize_epoch(claims.get("exp"))
    if claim_exp:
        return claim_exp
    try:
        expires_in = float(tokens.get("expiresIn") or tokens.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0.0
    if 0 < expires_in < MAX_EXPIRES_IN:
        return util.now() + expires_in
    return 0.0


def _is_dead_refresh(payload: Dict[str, Any], message: str) -> bool:
    code = str(payload.get("code") or "")
    if code == str(REFRESH_TOKEN_DEAD):
        return True
    lowered = (message or "").lower()
    return "offline user session not found" in lowered or "refresh token" in lowered and "invalid" in lowered


def classify_refresh_failure(payload: Dict[str, Any]) -> str:
    """把一次刷新失败归成 ``dead`` / ``transient`` 两类。

    ``dead`` = 必须人工重新登录（账号进 auth_dead）；``transient`` = 稍后重试。
    """
    return "dead" if _is_dead_refresh(payload, str(payload.get("msg") or "")) else "transient"
