"""Configuration: defaults, file loading, environment overrides, validation.

Precedence (lowest to highest): ``DEFAULTS`` -> ``config.json`` -> ``WB_*`` env.

The file is intentionally flat-ish JSON: an operator should be able to edit it
in Notepad, and the panel only ever writes a handful of keys back.  Unknown keys
are preserved on save so a newer version's settings survive a downgrade.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Dict, List, Optional, Tuple

#: ``validate()`` 里用来标记"可以放行的告警"的前缀（doctor 据此决定退出码）。
WARNING_PREFIX = "warning: "

from . import util

DEFAULTS: Dict[str, Any] = {
    # --- process -----------------------------------------------------------
    "listen": "127.0.0.1:8788",
    "data_dir": "./data",
    "config_version": 1,
    # Initial gateway key; the panel-managed key store takes over once it has
    # at least one key.  Empty means "only the panel-managed keys work".
    "api_key": "",
    "log_level": "INFO",
    # --- http --------------------------------------------------------------
    "server": {
        "max_payload_mb": 16,
        # 读请求头/体的超时（秒）：慢速客户端一个连接占一条线程，必须有人掐它。
        "request_timeout": 30,
        # 写响应的超时（秒）：客户端不读，写到缓冲区满就会永久阻塞。
        "write_timeout_seconds": 120,
        "shutdown_grace": 5,
        # 只有在反向代理后面才打开：开了就信任 X-Forwarded-For / X-Real-IP，
        # 否则任何人带一个头就能绕开面板登录限流。
        "trust_proxy": False,
    },
    "panel": {
        "enabled": True,
        "password": "admin",
        "session_hours": 12,
        "readonly": False,
    },
    "logging": {
        "level": "INFO",
        "dir": "./logs",
    },
    # 用量台账（append-only JSONL，按天一个文件）
    "usage": {
        "retention_days": 7,
    },
    # --- upstream ----------------------------------------------------------
    "upstream": {
        "timeout_seconds": 120,
        "header_timeout_seconds": 120,
        "idle_timeout_seconds": 300,
        "user_agent": "",
        "client_version": "",
        "cli_version": "",
        "client_name": "",
        "device_token": "",
        "device_token_file": "",
        "device_token_mode": "auto",
        # chat 请求是否带 X-Machine-ID / X-Session-ID（由 uid 派生的稳定 id）。
        # panel 的抓包带、hub 5.8.4 的抓包不带，两边都能跑通；带上是更"像插件"的形状，
        # 但如果上游开始按机器 id 做频率风控，关掉它就是最后一道退路。
        "send_stable_ids": True,
        "ca_bundle": "",
        "verify_tls": True,
        "passthrough_ip": False,
        "stream_heartbeat_seconds": 15,
        # 出口级退避：链路抖动（超时/5xx）记在"链路"上，不记在账号上。
        # 记在账号上的后果是：一次网络抖动 → 整池同时熔断 → 运维去重登、换号、加号，全是白费。
        "egress_base_seconds": 1.0,
        "egress_max_seconds": 15.0,
        "egress_reset_seconds": 60.0,
        "egress_jitter": 0.25,
        # 客户端没给 conversation_id 时用会话键派生（跨轮稳定），别每轮换一个新会话
        "derive_conversation_id": True,
        "hosts": {"cn": "", "global": ""},
    },
    "cooldown": {
        "soft_rate": "600s",
        "soft_rate_max": "2h",
        "hard_rate": "10m",
        "hard_rate_max": "6h",
        "auth_dead": "24h",
    },
    "pool": {
        # 域键统一用 cn / global；读取时仍接受旧键名 domestic / international
        "strategy": {"cn": "weighted_random", "global": "weighted_random"},
        "max_in_flight": 3,
        "max_in_flight_global": 2,
        "breaker_threshold": 3,
        "breaker_cooldown": "30m",
        "breaker_cooldown_max": "6h",
        "degrade_threshold": 5,
        "degrade_cooldown": "10m",
        "degrade_cooldown_max": "2h",
        # 11140 连坐：900 秒内 2 个号被风控就整域暂停这么久（0 = 关闭）
        "restriction_hold_seconds": 1800,
        "min_pick_gap_ms": 100,
        # 同一账号两次出站请求的最小间隔。真人不会 200ms 连发，被 agent 驱动的
        # 客户端会；对上游来说"同一机器 id 高频调用"是最像脚本的特征。
        # 0 = 关闭。
        "min_request_interval": "200ms",
        # 内容审核 / 边缘 WAF 拦截（403 但不是 11140）时的短避让。它跟账号健康无关：
        # 换号、刷令牌都没用，反而是"换号继续打"最容易连坐。
        "waf_cooldown": "60s",
        # 配额耗尽（402 / 6004）之后的验证探针：解禁点 + grace 打一发 16 token 的
        # 最小请求确认额度回来了；没恢复就退到 retry 之后再探。避免让真实流量去撞。
        "quota_probe_grace": "60s",
        "quota_probe_retry": "6h",
        "prefer_expiring": True,
        "expiring_soon": "168h",
        "model_rates": {},
        # 余额闸门：余额低于它就不派发**收费**模型（0 = 关闭）。
        # 默认关闭：只有一个号的用户被这道闸门拦住就等于网关全挂，而真实账号余额
        # 常常就是十几个积分。想省钱的场景（多号、有人专门跑贵模型）再打开。
        "credit_floor": 0,
        "idle_weight_per_hour": 0.5,
        "idle_weight_max": 5.0,
        "rate_limit_retries": 2,
        "cross_domain_fallback": False,
    },
    "session_sticky": {"enabled": True, "ttl": "30m", "gc_interval": "5m", "page_size": 3},
    "schedule": {        # 键名必须和 scheduler.py 读的一致，否则配置改了等于没改
        "token_refresh_enabled": True,
        "keepalive_minutes": 60,
        "balance_refresh_enabled": True,
        "balance_minutes": 180,
        "checkin_enabled": True,
        "checkin_hour": 9,
        "checkin_realms": ["cn"],
        # 活跃上报：点亮连登天数 / 解锁 first_buddy 任务。企业号会自动跳过（零收益）。
        "report_enabled": True,
        "report_hour": 10,
        "report_realms": ["cn", "global"],
        # 配额耗尽后到期探针：到解禁点主动打一发 16 token 的最小请求确认额度回来了
        "quota_probe_enabled": True,
        "quota_probe_minutes": 10,
    },
    # 模型目录：hidden 支持 "prefix*" 通配；default 是请求里没带 model 时的兜底
    "models": {"hidden": [], "default": "",
               # 客户端写的名字 → 上游真名。可以拿它把 gpt-4o / sonnet 这类
               # 现成客户端里写死的模型名接到上游存在的模型上。
               "aliases": {},
               # 上下文长度只在"知道"的时候才报：上游清单里通常没有这个字段，
               # 客户端拿不到会因为低估窗口而提前截断，所以允许在这里补。
               "context_lengths": {},
               # force 刷新模型目录的最小间隔（秒）：面板连点、多进程同时冷启动
               # 都会变成一串 /v3/config 请求，那正是"同一机器 id 高频拉目录"的形状。
               "min_refresh_seconds": 30},
    # 出站请求体的反指纹净化。默认开：上游对 Claude Code / Codex 模板句和
    # 字面量 "11-128" 是**逐字匹配**，命中就整单拒（400 11-128）。
    # 想要原样透传自己的 prompt 就显式改成 "passthrough"。
    "prompt": {"mode": "sanitize", "file": "", "content_retry": True},
    "features": {},
}

ENV_OVERRIDES: Dict[str, str] = {
    "WB_LISTEN": "listen",
    "WB_DATA_DIR": "data_dir",
    "WB_API_KEY": "api_key",
    "WB_LOG_LEVEL": "log_level",
    "WB_PANEL_PASSWORD": "panel.password",
    "WB_PANEL_ENABLED": "panel.enabled",
    "WB_UPSTREAM_TIMEOUT": "upstream.timeout_seconds",
    "WB_UPSTREAM_IDLE_TIMEOUT": "upstream.idle_timeout_seconds",
    "WB_USER_AGENT": "upstream.user_agent",
    "WB_CLIENT_VERSION": "upstream.client_version",
    "WB_CLI_VERSION": "upstream.cli_version",
    "WB_CLIENT_NAME": "upstream.client_name",
    "WB_DEVICE_TOKEN": "upstream.device_token",
    "WB_DEVICE_TOKEN_FILE": "upstream.device_token_file",
    "WB_CA_BUNDLE": "upstream.ca_bundle",
    "WB_MAX_PAYLOAD_MB": "server.max_payload_mb",
    "WB_MAX_IN_FLIGHT": "pool.max_in_flight",
    "WB_CREDIT_FLOOR": "pool.credit_floor",
}

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}


# 旧版本用 domestic / international 当域键；读配置时保留回落，
# 这样升级不需要用户改配置文件（两套键名同时存在时以新键为准）。
LEGACY_REALM_KEYS = {"cn": "domestic", "global": "international"}


def realm_value(cfg: Any, prefix: str, realm: str, default: Any = None) -> Any:
    """读 ``<prefix>.<realm>``，缺失时回落旧键名。cfg 可以是 Config 或普通 dict。"""
    if cfg is None:
        return default
    getter = cfg.get if hasattr(cfg, "get") else None
    if getter is None:
        return default
    value = getter(prefix + "." + realm)
    if value in (None, ""):
        legacy = LEGACY_REALM_KEYS.get(realm)
        if legacy:
            value = getter(prefix + "." + legacy)
    return default if value in (None, "") else value


def _coerce(value: str, template: Any) -> Any:
    """Convert an env string to the type of the default it overrides."""
    if isinstance(template, bool):
        lowered = value.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        return template
    if isinstance(template, int) and not isinstance(template, bool):
        try:
            return int(value.strip())
        except ValueError:
            return template
    if isinstance(template, float):
        try:
            return float(value.strip())
        except ValueError:
            return template
    if isinstance(template, list):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def deep_merge(base: Dict[str, Any], overlay: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Recursive dict merge that keeps unknown keys from *overlay*."""
    result = copy.deepcopy(base)
    if not overlay:
        return result
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class Config:
    """Mutable configuration object shared by the whole process."""

    def __init__(self, path: Optional[str] = None, values: Optional[Dict[str, Any]] = None) -> None:
        self.path = os.path.abspath(path) if path else None
        self.values: Dict[str, Any] = deep_merge(DEFAULTS, values or {})
        self._env_applied: Dict[str, str] = {}

    # -- construction -------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str]) -> "Config":
        # 严格解析：损坏的 config.json 静默回落 DEFAULTS 等于"api_key 空了（开放
        # 模式）、面板密码回 admin、data_dir 换地方"——比直接起不来危险得多。
        values = util.read_json(path, None, strict=True) if path else None
        config = cls(path, values if isinstance(values, dict) else None)
        config.apply_env(os.environ)
        return config

    def apply_env(self, environ: Dict[str, str]) -> None:
        for env_name, dotted in ENV_OVERRIDES.items():
            raw = environ.get(env_name)
            if raw is None or raw == "":
                continue
            template = self.get(dotted)
            self.set(dotted, _coerce(raw, template))
            self._env_applied[dotted] = env_name

    # -- access -------------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.values
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.values
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def as_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self.values)

    # -- persistence --------------------------------------------------------
    def save(self) -> None:
        """就地写回配置文件；内存配置（path 为空）直接跳过。

        数据目录可能是第一次创建（默认路径是 ``<data_dir>/config.json``），
        所以这里顺手把父目录补出来，免得第一次改设置就 FileNotFoundError。
        """
        if not self.path:
            return
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        util.atomic_write_json(self.path, self.values)

    def public_dict(self) -> Dict[str, Any]:
        """Config view for the panel: secrets replaced by a mask marker."""
        data = self.as_dict()
        # 初始网关密钥同样是秘密：面板只需要知道"设没设"，不需要明文。
        # 与 panel.password_set 保持一致，未设置时也返回 False，面板无需区分"缺失"和"未设置"。
        data["api_key_set"] = bool(data.get("api_key"))
        data["api_key"] = ""
        upstream = data.setdefault("upstream", {})
        if upstream.get("device_token"):
            upstream["device_token_set"] = True
        upstream["device_token"] = ""
        panel = data.setdefault("panel", {})
        panel["password_set"] = bool(panel.get("password"))
        panel["password"] = ""
        return data

    # -- derived values -----------------------------------------------------
    @property
    def data_dir(self) -> str:
        return os.path.abspath(self.get("data_dir", "./data"))

    def path_in_data(self, *parts: str) -> str:
        return os.path.join(self.data_dir, *parts)

    def listen_address(self) -> Tuple[str, int]:
        raw = str(self.get("listen", "127.0.0.1:8788")).strip()
        if raw.startswith(":"):
            return "0.0.0.0", int(raw[1:])
        if ":" in raw:
            host, port = raw.rsplit(":", 1)
            return (host or "127.0.0.1"), int(port)
        return "127.0.0.1", int(raw or 8788)

    def upstream_timeout(self) -> float:
        return float(self.get("upstream.timeout_seconds", 120))

    def idle_timeout(self) -> float:
        return float(self.get("upstream.idle_timeout_seconds", 300))

    # -- validation ---------------------------------------------------------
    def validate(self) -> List[str]:
        problems: List[str] = []
        try:
            host, port = self.listen_address()
            if not (0 < port < 65536):
                problems.append(f"listen port out of range: {port}")
            del host
        except (ValueError, TypeError):
            problems.append(f"listen is not host:port -> {self.get('listen')!r}")

        if int(self.get("server.max_payload_mb", 16)) <= 0:
            problems.append("server.max_payload_mb must be > 0")
        if self.get("upstream.verify_tls") is False and not self.get("upstream.ca_bundle"):
            problems.append("upstream.verify_tls=false without a ca_bundle is unsafe")
        for dotted in ("pool.max_in_flight", "pool.max_in_flight_global"):
            if int(self.get(dotted, 0)) < 1:
                problems.append(f"{dotted} must be >= 1")
        if int(self.get("pool.credit_floor", 0)) < 0:
            problems.append("pool.credit_floor must be >= 0")
        for dotted in (
            "cooldown.soft_rate",
            "cooldown.hard_rate",
            "pool.breaker_cooldown",
            "session_sticky.ttl",
        ):
            if util.parse_duration(self.get(dotted), -1) < 0:
                problems.append(f"{dotted} is not a duration: {self.get(dotted)!r}")
        if not os.path.isdir(self.data_dir):
            # 首启时目录还不存在是正常的（serve 会创建）。用前缀区分"告警"和"错误"，
            # 否则 doctor 在全新安装上永远返回 1，谁都不会再信它的退出码。
            problems.append(f"{WARNING_PREFIX}data_dir does not exist yet (will be created): {self.data_dir}")
        return problems

    def validate_split(self) -> Tuple[List[str], List[str]]:
        """``(errors, warnings)``：只有 errors 该让 doctor 失败。"""
        errors: List[str] = []
        warnings: List[str] = []
        for problem in self.validate():
            if problem.startswith(WARNING_PREFIX):
                warnings.append(problem[len(WARNING_PREFIX):])
            else:
                errors.append(problem)
        return errors, warnings
