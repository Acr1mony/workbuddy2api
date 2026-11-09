"""Persistence: accounts, gateway API keys, runtime settings.

Everything lives in ``<data_dir>`` as plain JSON so an operator can back it up
with a file copy and diff it with git.  Writes are atomic and serialised by a
per-file lock, which is enough because a single gateway process owns the files
(the Docker/NAS deployments run one process by design).

Record shapes are *forward compatible*: unknown keys read from disk are kept in
``extra`` and written back, so downgrading does not silently drop a field.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import util
from .log import get_logger

log = get_logger("store")

ACCOUNT_FILE = "accounts.json"
KEY_FILE = "api_keys.json"
RUNTIME_FILE = "runtime.json"


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def _b64pad(raw: str) -> Dict[str, Any]:
    """Decode a JWT payload without verifying it.

    Only used to *read* expiry/uid hints from an upstream token; verification is
    the upstream's job.  Failure returns ``{}``.
    """
    import base64
    import json

    parts = (raw or "").split(".")
    if len(parts) < 2:
        return {}
    body = parts[1]
    body += "=" * (-len(body) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


@dataclass
class Account:
    """One upstream WorkBuddy / CodeBuddy account.

    ``uid`` is the upstream account id and the primary key everywhere; ``id`` is
    a local surrogate kept stable even if the upstream never returns a uid.
    """

    uid: str = ""
    id: str = ""
    realm: str = "cn"  # "cn" | "global"
    nickname: str = ""
    email: str = ""
    avatar: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0  # epoch seconds; 0 = unknown
    device_token: str = ""
    device_id: str = ""  # stable per account, derived from id
    session_id: str = ""  # stable per account, derived from id
    proxy: str = ""  # per-account egress: http://user:pass@host:port
    group: str = ""
    disabled: bool = False
    paused: bool = False  # 人工"先别用它"：不参与调度，但保留令牌与窗口（与 disabled 区分）
    note: str = ""
    # --- upstream identity -------------------------------------------------
    enterprise_id: str = ""  # 企业号才有；决定签到/成长体系是否可用
    domain: str = ""  # 登录时上游返回的域，发在 X-Domain
    product: str = "workbuddy"  # workbuddy | cli | vscode（决定头族形态）
    source: str = "oauth"  # oauth | import | api-key
    static_key: bool = False  # ck_/pt_ 静态密钥：不可刷新，永不过期
    weight: float = 1.0  # 上游调度权重
    tier: str = ""  # 套餐名，仅展示
    restricted_until: float = 0.0  # 11140 触发的 realm 级限制
    added_at: float = 0.0
    updated_at: float = 0.0
    last_used_at: float = 0.0
    last_error: str = ""
    # --- mutable runtime state (persisted so restarts keep cooldowns) ------
    credit: Optional[float] = None
    credit_at: float = 0.0
    # 余额的"什么时候过期"视角：{total, expiring_soon, buckets{d3,d7,d30,later,expired},
    # soonest_expire_at, package_count}。上游把额度拆成几十个包，只看总额会在最大那个
    # 包到期的早上突然归零，面板必须能说清"这些钱分别什么时候没"。
    credit_buckets: Dict[str, Any] = field(default_factory=dict)
    cooldown_until: float = 0.0
    cooldown_reason: str = ""
    cooldown_level: str = ""  # "" | "soft" | "hard" | "auth" | "breaker"
    failures: int = 0
    state: str = "ready"  # ready | cooldown | auth_dead | disabled

    # --- 池状态：五类独立时间窗（见 pool.py 顶部说明）---------------------
    balance_until: float = 0.0  # 402 无余额 → 次日 04:00
    breaker_until: float = 0.0  # 5xx 熔断
    degrade_until: float = 0.0  # 未知失败降级
    soft_streak: int = 0  # 凭证级 429 连击
    unscoped_streak: int = 0  # 无 reset 时钟的裸 429 连击
    consecutive_fails: int = 0  # 只有"未分类失败"才喂它
    breaker_retries: int = 0
    model_cooldowns: Dict[str, Any] = field(default_factory=dict)  # model -> {until, reason, hits}
    last_fail_at: float = 0.0
    last_soft_at: float = 0.0
    # 只在进程内有效，不落盘（重启后并发槽与 LRU 序号本来就该归零）
    used_seq: int = 0
    in_flight: int = 0
    last_chat_at: float = 0.0  # 上一次真的打到上游的时间，给请求节奏用

    extra: Dict[str, Any] = field(default_factory=dict)

    # -- helpers ------------------------------------------------------------
    @property
    def key(self) -> str:
        return self.uid or self.id

    @property
    def label(self) -> str:
        return self.nickname or self.email or util.mask_secret(self.uid, 4) or "account"

    @property
    def enabled(self) -> bool:
        """``disabled`` 的反面。面板与路由里读 ``enabled`` 更符合直觉。"""
        return not self.disabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self.disabled = not bool(value)

    def ensure_ids(self) -> "Account":
        """补齐本地 id 与派生的设备/会话码（导入或新建账号后调用）。"""
        if not self.id:
            self.id = self.uid or util.telegram_id(f"account:{util.rand_hex(16)}", 24)
        if not self.device_id:
            self.device_id = util.telegram_id(f"wb2a:device:{self.id}", 36)
        if not self.session_id:
            self.session_id = util.telegram_id(f"wb2a:session:{self.id}", 36)
        return self

    def token_expired(self, skew: float = 120.0) -> bool:
        return bool(self.expires_at) and self.expires_at - skew <= util.now()

    def in_cooldown(self, now: Optional[float] = None) -> bool:
        return self.cooldown_until > (now if now is not None else util.now())

    def masked(self) -> Dict[str, Any]:
        """Panel-safe view: no raw tokens, ever."""
        return {
            "uid": self.uid,
            "id": self.id,
            "realm": self.realm,
            "nickname": self.nickname,
            "email": self.email,
            "avatar": self.avatar,
            "group": self.group,
            "disabled": self.disabled,
            "paused": self.paused,
            "note": self.note,
            "added_at": self.added_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "last_error": self.last_error,
            "credit": self.credit,
            "credit_at": self.credit_at,
            "credit_buckets": self.credit_buckets,
            "cooldown_until": self.cooldown_until,
            "cooldown_reason": self.cooldown_reason,
            "cooldown_level": self.cooldown_level,
            "failures": self.failures,
            "state": self.state,
            "balance_until": self.balance_until,
            "breaker_until": self.breaker_until,
            "degrade_until": self.degrade_until,
            "soft_streak": self.soft_streak,
            "unscoped_streak": self.unscoped_streak,
            "consecutive_fails": self.consecutive_fails,
            "model_cooldowns": {m: (v or {}).get("until", 0) for m, v in self.model_cooldowns.items()},
            "in_flight": self.in_flight,
            "has_access_token": bool(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
            "token_expires_at": self.expires_at,
            "token_expired": self.token_expired(0),
            "proxy": util.mask_secret(self.proxy) if self.proxy else "",
            "device_token": "set" if self.device_token else "",
            "enterprise_id": util.mask_secret(self.enterprise_id, 4) if self.enterprise_id else "",
            "enterprise": bool(self.enterprise_id),
            "domain": self.domain,
            "product": self.product,
            "source": self.source,
            "static_key": self.static_key,
            "weight": self.weight,
            "tier": self.tier,
            "restricted_until": self.restricted_until,
        }

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "uid": self.uid,
            "id": self.id,
            "realm": self.realm,
            "nickname": self.nickname,
            "email": self.email,
            "avatar": self.avatar,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "device_token": self.device_token,
            "device_id": self.device_id,
            "session_id": self.session_id,
            "proxy": self.proxy,
            "group": self.group,
            "disabled": self.disabled,
            "paused": self.paused,
            "note": self.note,
            "enterprise_id": self.enterprise_id,
            "domain": self.domain,
            "product": self.product,
            "source": self.source,
            "static_key": self.static_key,
            "weight": self.weight,
            "tier": self.tier,
            "restricted_until": self.restricted_until,
            "added_at": self.added_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "last_error": self.last_error,
            "credit": self.credit,
            "credit_at": self.credit_at,
            "credit_buckets": self.credit_buckets,
            "cooldown_until": self.cooldown_until,
            "cooldown_reason": self.cooldown_reason,
            "cooldown_level": self.cooldown_level,
            "failures": self.failures,
            "state": self.state,
            "balance_until": self.balance_until,
            "breaker_until": self.breaker_until,
            "degrade_until": self.degrade_until,
            "soft_streak": self.soft_streak,
            "unscoped_streak": self.unscoped_streak,
            "consecutive_fails": self.consecutive_fails,
            "breaker_retries": self.breaker_retries,
            "model_cooldowns": self.model_cooldowns,
            "last_fail_at": self.last_fail_at,
            "last_soft_at": self.last_soft_at,
        }
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Account":
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}  # type: ignore[attr-defined]
        kwargs = {key: data[key] for key in known if key in data}
        extra = {key: value for key, value in data.items() if key not in known}
        account = cls(**kwargs)
        account.extra = extra
        account.realm = account.realm or "cn"
        if not account.id:
            account.id = account.uid or util.telegram_id(f"account:{util.rand_hex(16)}", 24)
        if not account.device_id:
            account.device_id = util.telegram_id(f"wb2a:device:{account.id}", 36)
        if not account.session_id:
            account.session_id = util.telegram_id(f"wb2a:session:{account.id}", 36)
        return account

    # -- upstream payload hints --------------------------------------------
    def token_claims(self) -> Dict[str, Any]:
        return _b64pad(self.access_token)


@dataclass
class ApiKey:
    """A gateway key with its own permissions and counters."""

    key: str = ""
    name: str = ""
    realm: str = ""  # "" = follow request / any realm
    models: List[str] = field(default_factory=list)  # whitelist, supports "prefix*"
    excluded_models: List[str] = field(default_factory=list)
    accounts: List[str] = field(default_factory=list)  # allowed account uids, empty=all
    groups: List[str] = field(default_factory=list)
    disabled: bool = False
    max_concurrency: int = 0  # 每 key 同时在飞的请求上限；0 = 不限
    rate_limit: int = 0  # 每分钟请求数上限（RPM）；0 = 不限
    created_at: float = 0.0
    last_used_at: float = 0.0
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "key": self.key,
            "name": self.name,
            "realm": self.realm,
            "models": list(self.models),
            "excluded_models": list(self.excluded_models),
            "accounts": list(self.accounts),
            "groups": list(self.groups),
            "disabled": self.disabled,
            "max_concurrency": self.max_concurrency,
            "rate_limit": self.rate_limit,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ApiKey":
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}  # type: ignore[attr-defined]
        kwargs = {key: data[key] for key in known if key in data}
        extra = {key: value for key, value in data.items() if key not in known}
        item = cls(**kwargs)
        item.extra = extra
        item.models = [str(m) for m in item.models or []]
        item.excluded_models = [str(m) for m in item.excluded_models or []]
        item.accounts = [str(a) for a in item.accounts or []]
        item.groups = [str(g) for g in item.groups or []]
        return item

    def masked(self) -> Dict[str, Any]:
        data = self.to_dict()
        data["key"] = util.mask_secret(self.key, 8)
        return data

    # -- permission checks --------------------------------------------------
    @staticmethod
    def _match(pattern: str, model: str) -> bool:
        pattern = pattern.strip().lower()
        model = model.lower()
        if not pattern:
            return False
        if pattern.endswith("*"):
            return model.startswith(pattern[:-1])
        return model == pattern

    def allows_model(self, model: str) -> bool:
        if any(self._match(p, model) for p in self.excluded_models):
            return False
        if not self.models:
            return True
        return any(self._match(p, model) for p in self.models)


class Store:
    """Loads and mutates the JSON state files."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = os.path.abspath(data_dir)
        util.ensure_dir(self.data_dir)
        self._lock = threading.RLock()
        self.accounts: Dict[str, Account] = {}
        self.keys: Dict[str, ApiKey] = {}
        self.runtime: Dict[str, Any] = {}
        self.load()

    # -- paths --------------------------------------------------------------
    def _path(self, name: str) -> str:
        return os.path.join(self.data_dir, name)

    # -- load / save --------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            # 严格解析：这两个文件读错了会变成"空账号池 / 没有 key 继续跑"，
            # 随后下一次 save 把空库写回去——等于一夜之间把账号和密钥全丢了。
            # 损坏时 read_json 已经备份成 .corrupt-<ts>，这里直接抛给上层退出。
            raw_accounts = util.read_json(self._path(ACCOUNT_FILE), {}, strict=True)
            self.accounts = {}
            if isinstance(raw_accounts, dict):
                entries = raw_accounts.get("accounts", raw_accounts)
                if isinstance(entries, list):
                    items = entries
                elif isinstance(entries, dict):
                    items = list(entries.values())
                else:
                    items = []
            elif isinstance(raw_accounts, list):
                items = raw_accounts
            else:
                items = []
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                account = Account.from_dict(entry)
                if account.key:
                    self.accounts[account.key] = account

            raw_keys = util.read_json(self._path(KEY_FILE), [], strict=True)
            self.keys = {}
            if isinstance(raw_keys, dict):
                raw_keys = raw_keys.get("keys", [])
            for entry in raw_keys if isinstance(raw_keys, list) else []:
                if not isinstance(entry, dict) or not entry.get("key"):
                    continue
                item = ApiKey.from_dict(entry)
                self.keys[item.key] = item

            raw_runtime = util.read_json(self._path(RUNTIME_FILE), {})
            self.runtime = raw_runtime if isinstance(raw_runtime, dict) else {}

    def save_accounts(self) -> None:
        with self._lock:
            payload = {
                "version": 1,
                "updated_at": util.now(),
                "accounts": [account.to_dict() for account in self.accounts.values()],
            }
            util.atomic_write_json(self._path(ACCOUNT_FILE), payload)

    def save_keys(self) -> None:
        with self._lock:
            payload = {
                "version": 1,
                "updated_at": util.now(),
                "keys": [item.to_dict() for item in self.keys.values()],
            }
            util.atomic_write_json(self._path(KEY_FILE), payload)

    def save_runtime(self) -> None:
        with self._lock:
            util.atomic_write_json(self._path(RUNTIME_FILE), self.runtime)

    # -- accounts -----------------------------------------------------------
    def get_account(self, key: str) -> Optional[Account]:
        with self._lock:
            return self.accounts.get(key)

    def list_accounts(self, realm: str = "") -> List[Account]:
        with self._lock:
            items = list(self.accounts.values())
        if realm:
            items = [item for item in items if item.realm == realm]
        return items

    def upsert_account(self, account: Account, *, save: bool = True) -> Account:
        with self._lock:
            existing = self.accounts.get(account.key)
            if existing is not None:
                # Preserve counters that the login flow knows nothing about.
                account.credit = existing.credit if account.credit is None else account.credit
                account.credit_at = existing.credit_at
                account.added_at = existing.added_at or util.now()
                account.group = account.group or existing.group
                account.note = account.note or existing.note
                account.extra = {**existing.extra, **account.extra}
            account.updated_at = util.now()
            account.added_at = account.added_at or util.now()
            self.accounts[account.key] = account
            if save:
                self.save_accounts()
            return account

    def update_credit(self, account: Account, credits: Any, *, save: bool = True) -> Account:
        """记一次余额读数；credits=None 表示"读不到"，不能当 0 写回去。"""
        with self._lock:
            account.credit = credits
            account.credit_at = util.now()
            self.accounts[account.key] = account
            if save:
                self.save_accounts()
            return account

    def remove_account(self, key: str) -> bool:
        with self._lock:
            existed = self.accounts.pop(key, None) is not None
            if existed:
                self.save_accounts()
            return existed

    # -- api keys -----------------------------------------------------------
    def get_key(self, key: str) -> Optional[ApiKey]:
        with self._lock:
            return self.keys.get(key)

    def list_keys(self) -> List[ApiKey]:
        with self._lock:
            return list(self.keys.values())

    def add_key(self, item: ApiKey, save: bool = True) -> ApiKey:
        with self._lock:
            item.created_at = item.created_at or util.now()
            self.keys[item.key] = item
            if save:
                self.save_keys()
            return item

    def remove_key(self, key: str) -> bool:
        with self._lock:
            existed = self.keys.pop(key, None) is not None
            if existed:
                self.save_keys()
            return existed

    def touch_key(self, key: str, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        """Update per-key counters; the file is flushed by the caller's cadence."""
        with self._lock:
            item = self.keys.get(key)
            if item is None:
                return
            item.requests += 1
            item.prompt_tokens += max(0, int(prompt_tokens or 0))
            item.completion_tokens += max(0, int(completion_tokens or 0))
            item.last_used_at = util.now()

    def flush_keys(self) -> None:
        with self._lock:
            self.save_keys()
