"""上游身份：域名、UA、以及官方客户端那套"看得见的风控头"。

上游不只看 token，还会看你是谁：Origin/Referer 域、UA 的品牌名、语言、机器码、
会话码、对话头族、用量归属头。任何一项缺失或不自洽都会让请求变成"网关特征"，
轻则降级，重则风控。这里把这些规则集中成一个地方，改头只改这个文件。

两套域（realm）：

    cn      国内版，copilot.tencent.com，前端域 www.codebuddy.cn，平台名 WorkBuddy
    global  国际版，www.workbuddy.ai，   前端域 www.workbuddy.ai，平台名 WorkBuddy AI

同一个 /v2 API，只是域名、Origin、语言与"无企业"的声明方式不同。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any, Dict, Optional

from . import util

# --------------------------------------------------------------------------
# realm 常量
# --------------------------------------------------------------------------

REALMS: Dict[str, Dict[str, str]] = {
    "cn": {
        "host": "https://copilot.tencent.com",
        "origin": "https://www.codebuddy.cn",
        "domain": "codebuddy.cn",
        "language": "zh-CN",
        "platform": "WorkBuddy",
    },
    "global": {
        "host": "https://www.workbuddy.ai",
        "origin": "https://www.workbuddy.ai",
        "domain": "www.workbuddy.ai",
        "language": "en-US",
        "platform": "WorkBuddy AI",
    },
}

DEFAULT_CLIENT_VERSION = "5.5.4"
DEFAULT_CLI_VERSION = "2.137.1"


def normalize_realm(value: Optional[str]) -> str:
    text = (value or "").strip().lower()
    if text in {"global", "intl", "international", "www", "workbuddy", "ai"}:
        return "global"
    return "cn"


def realm_info(realm: Optional[str]) -> Dict[str, str]:
    return REALMS[normalize_realm(realm)]


def host_for(realm: Optional[str]) -> str:
    env = os.environ.get("WB_UPSTREAM_HOST")
    if env:
        return env.rstrip("/")
    return realm_info(realm)["host"]


# --------------------------------------------------------------------------
# UA
# --------------------------------------------------------------------------


def user_agent(realm: Optional[str] = None, client_version: str = "", cli_version: str = "") -> str:
    """``WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1``（global 第二段换成 ``WorkBuddy AI``）。

    注意**只有第二段**按域切换：官方国际版是 ``WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2``，
    第一段恒为 ``WorkBuddy``。两段都写成平台名的结果是一个官方从不发出的 UA，
    而 UA 形状本身是会被判定的（送错平台段可能直接 403 11140）。
    """
    platform = realm_info(realm)["platform"]
    product = client_version or DEFAULT_CLIENT_VERSION
    cli = cli_version or DEFAULT_CLI_VERSION
    return f"WorkBuddy/{product} {platform}/{product} CLI/{cli}"


def resolve_user_agent(cfg: Any, realm: Optional[str] = None) -> str:
    """``upstream.user_agent`` 有值就原样用它，否则按域拼一个。

    覆盖的典型用法是"从官方客户端抄一个 UA 填进来"，此时任何自动拼接都会把它改坏；
    所以显式值一个字都不动（只去首尾空白）。
    """
    explicit = ""
    if cfg is not None:
        try:
            explicit = str(cfg.get("upstream.user_agent", "") or "").strip()
        except (AttributeError, TypeError):
            explicit = ""
    if explicit:
        return explicit
    if cfg is None:
        return user_agent(realm)
    return user_agent(
        realm,
        cfg.get("upstream.client_version", DEFAULT_CLIENT_VERSION) or DEFAULT_CLIENT_VERSION,
        cfg.get("upstream.cli_version", DEFAULT_CLI_VERSION) or DEFAULT_CLI_VERSION,
    )


# --------------------------------------------------------------------------
# 账号级稳定指纹
# --------------------------------------------------------------------------


def stable_id(uid: str, purpose: str) -> str:
    """按 uid 派生的稳定设备/会话码（36 hex）。

    用途 ``machine`` → X-Machine-ID（设备级），``session`` → X-Session-ID（固定会话）。
    同一 uid 恒同值、跨重启不变、不同账号互异：多开时上游看到的是"每号一台固定设备"，
    而不是同一个设备指纹下冒出一堆账号。
    """
    if not uid:
        return ""
    return hashlib.sha256(f"wb2a:{purpose}:{uid}".encode("utf-8")).hexdigest()[:36]


def machine_id(account: Any) -> str:
    explicit = getattr(account, "device_id", "") or ""
    if explicit:
        return explicit
    return stable_id(getattr(account, "uid", "") or "", "machine")


def session_id(account: Any) -> str:
    explicit = getattr(account, "session_id", "") or ""
    if explicit:
        return explicit
    return stable_id(getattr(account, "uid", "") or "", "session")


def product_of(account: Any) -> str:
    """身份形态：``workbuddy``（桌面端）/ ``cli`` / ``vscode``。

    三套头族不一样（见 ``chat_headers``），默认走桌面端，因为桌面端是
    上游最"正常"的流量形态，权限也最全。
    """
    value = (getattr(account, "product", "") or "").strip().lower()
    return value if value in {"workbuddy", "cli", "vscode"} else "workbuddy"


# --------------------------------------------------------------------------
# 消息 / 会话头族
# --------------------------------------------------------------------------


def new_message_id() -> str:
    """32 hex，对齐官方 X-Request-ID / X-Conversation-Message-ID。

    长度是硬要求：官方是 UUIDv4 去横线的 32 位，16 位一眼就是自造的。
    """
    return util.rand_hex(32)


def _valid_trace_id(text: str) -> bool:
    if len(text) not in (16, 32):
        return False
    return all(ch in "0123456789abcdefABCDEF" for ch in text)


class _TurnSalt:
    """进程启动随机盐：让会话头族 ID 无法被外部按 key 预计算，重启即换。"""

    def __init__(self) -> None:
        self.value = new_message_id()
        self._lock = threading.Lock()

    def get(self) -> str:
        return self.value


_salt = _TurnSalt()


def request_id_for_key(key: str) -> str:
    """会话键 → 稳定的 conversationRequestID（同 key 恒同值，纯派生不占内存）。"""
    if not key:
        return new_message_id()
    return hashlib.sha256(f"{_salt.get()}|{key}".encode("utf-8")).hexdigest()[:32]


def _text_of_content(content: Any) -> str:
    """把 OpenAI 的 content（字符串或 parts 数组）压成一段可哈希的文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
                elif isinstance(part.get("image_url"), (dict, str)):
                    chunks.append("[image]")
            elif isinstance(part, str):
                chunks.append(part)
        return "\n".join(chunks)
    return ""


def turn_key(body: Dict[str, Any]) -> str:
    """一眼「对话轮」：最后一条 user 消息的序号 + 文本。

    为什么是最后一条：一次用户发送内的所有上游调用（多轮 tool call、换号重试）
    body 里最后一条 user 消息恒定 → 同一个聚合键；用户发下一条 → 换键。
    为什么不取第一条：首条整段会话不变，会把所有轮并成一个。
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return ""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _text_of_content(message.get("content"))
        if not text:
            return ""
        digest = hashlib.sha256(f"{index}\x00{text}".encode("utf-8")).hexdigest()[:16]
        return digest
    return ""


def conversation_id_from_body(body: Dict[str, Any]) -> str:
    """透传客户端给的 conversationId；没给就返回空（不伪造会话）。"""
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        for key in ("conversation_id", "conversationId"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("conversation_id", "conversationId"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


# ChatMeta：一次出站请求的会话头族上下文
class ChatMeta:
    __slots__ = ("conversation_id", "conversation_request_id", "trace_id", "message_id")

    def __init__(
        self,
        conversation_id: str = "",
        conversation_request_id: str = "",
        trace_id: str = "",
        message_id: str = "",
    ) -> None:
        self.conversation_id = conversation_id or new_message_id()
        self.conversation_request_id = conversation_request_id or new_message_id()
        self.trace_id = trace_id or self.conversation_request_id
        self.message_id = message_id or new_message_id()

    @classmethod
    def for_request(
        cls, body: Dict[str, Any], inbound_trace: str = "", session_hint: str = ""
    ) -> "ChatMeta":
        """按请求体生成会话头族。

        ``X-Conversation-ID`` 必须**跨轮稳定**：客户端不带 conversation_id 时，
        官方客户端也不会每一轮换一个新会话。用会话键派生（同一段对话恒定），
        而不是每轮随机——否则上游看到的是"一个客户端在几分钟里开了几十个会话"。
        """
        key = turn_key(body)
        conversation = conversation_id_from_body(body)
        if not conversation:
            # 客户端没给会话 id 时用会话键派生：同一段对话恒定，跨轮不变。
            # 每轮随机等于"一个客户端几分钟内开了几十个一次性会话"，是最扎眼的机器形态。
            seed = session_hint or key
            if seed:
                conversation = hashlib.sha256(f"wb2a:conv:{seed}".encode("utf-8")).hexdigest()[:32]
        return cls(
            conversation_id=conversation,
            conversation_request_id=request_id_for_key(key) if key else "",
            trace_id=inbound_trace,
        )


# --------------------------------------------------------------------------
# 请求头组装
# --------------------------------------------------------------------------


def _attribution(client_name: str, client_version: str) -> Dict[str, str]:
    name = client_name or "WorkBuddy"
    if name == "SaaS":
        return {"X-Product": "SaaS"}
    return {
        "X-Agent-Purpose": "conversation",
        "X-IDE-Name": name,
        "X-IDE-Type": name,
        "X-IDE-Version": client_version or DEFAULT_CLIENT_VERSION,
        "X-Product": name,
    }


def common_headers(account: Any, cfg: Any, *, accept: str = "application/json",
                   include_ids: bool = True) -> Dict[str, str]:
    realm = normalize_realm(getattr(account, "realm", "cn"))
    info = realm_info(realm)
    headers = {
        "Content-Type": "application/json",
        "Accept": accept,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": info["origin"],
        "Referer": info["origin"] + "/",
        "User-Agent": resolve_user_agent(cfg, realm),
        "X-CodeBuddy-Request": "1",
        "Accept-Language": info["language"],
    }
    if include_ids:
        machine = machine_id(account)
        session = session_id(account)
        if machine:
            headers["X-Machine-ID"] = machine
        if session:
            headers["X-Session-ID"] = session
    # 明确要明文：gzip 的 SSE 一旦解压出问题，客户端看到的是"空回复"，日志里
    # 一句错都没有（我们也解压，这只是少一个变量）。
    headers["Accept-Encoding"] = "identity"
    return headers


def chat_headers(
    account: Any,
    cfg: Any,
    *,
    meta: Optional[ChatMeta] = None,
    client_ip: str = "",
) -> Dict[str, str]:
    """chat/completions 出站头：common + 账号 + 归属 + 会话头族 + 设备风控。"""
    realm = normalize_realm(getattr(account, "realm", "cn"))
    info = realm_info(realm)
    product = product_of(account)
    # hub 5.8.4 的抓包里 chat 请求不带 X-Machine-ID/X-Session-ID，panel 带；两边都能跑通，
    # 说明这是可选项。默认跟 panel（带），想更接近旧客户端就关掉。
    include_ids = True
    if cfg is not None:
        include_ids = bool(cfg.get("upstream.send_stable_ids", True))
    headers = common_headers(account, cfg, accept="application/json, text/event-stream",
                             include_ids=include_ids)
    access_token = getattr(account, "access_token", "") or ""
    uid = getattr(account, "uid", "") or ""

    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
        # 静态密钥（ck_/pt_）同时要 X-API-Key，否则模型目录会静默缺项。
        if access_token.startswith(("ck_", "pt_")):
            headers["X-API-Key"] = access_token
    else:
        headers["X-No-Authorization"] = "1"
    if uid:
        headers["X-User-Id"] = uid
    else:
        headers["X-No-User-Id"] = "1"

    # 归属 / 身份头：桌面端形态，和 UA 里的品牌名保持一致。
    client_name = (cfg.get("upstream.client_name", "") if cfg else "") or ""
    client_version = (cfg.get("upstream.client_version", "") if cfg else "") or DEFAULT_CLIENT_VERSION
    headers["X-Agent-Intent"] = "craft"
    headers["X-Agent-Type"] = "main"
    headers.update(_attribution(client_name, client_version))

    if realm == "global":
        headers["X-No-Enterprise-Id"] = "1"
        headers["X-Domain"] = info["domain"]
    else:
        enterprise = getattr(account, "enterprise_id", "") or ""
        if enterprise:
            headers["X-Enterprise-Id"] = enterprise
            headers["X-Tenant-Id"] = enterprise
        else:
            headers["X-No-Enterprise-Id"] = "1"
        domain = getattr(account, "domain", "") or ""
        if domain:
            headers["X-Domain"] = domain
        else:
            headers.setdefault("X-Domain", info["domain"])
        headers["X-No-Department-Info"] = "1"

    if product == "cli":
        # 官方 CLI 的指纹：node + stainless 那一套，另一条能走通的路。
        headers["X-Agent-Runtime"] = "other"
        headers["X-IDE-Name"] = "CLI"
        headers["X-IDE-Type"] = "CLI"
        headers.pop("Origin", None)
        headers.pop("Referer", None)
        headers.pop("X-Requested-With", None)
        headers["User-Agent"] = f"CLI/{DEFAULT_CLI_VERSION} CodeBuddy/{DEFAULT_CLI_VERSION}"

    if cfg is not None and cfg.get("upstream.passthrough_ip", False) and client_ip:
        headers["X-Forwarded-For"] = client_ip
        headers["X-Real-IP"] = client_ip
        headers["X-Client-IP"] = client_ip

    device_token = device_token_for(account, cfg)
    if device_token:
        headers["X-Device-Token"] = device_token

    meta = meta or ChatMeta()
    headers["X-Conversation-ID"] = meta.conversation_id
    headers["X-Conversation-Request-ID"] = meta.conversation_request_id
    headers["X-Conversation-Message-ID"] = meta.message_id
    headers["X-Request-ID"] = meta.message_id
    headers["X-Root-Request-ID"] = meta.conversation_request_id
    headers["X-Trace-ID"] = meta.trace_id
    trace = meta.conversation_request_id if _valid_trace_id(meta.conversation_request_id) else meta.message_id
    headers["X-B3-TraceId"] = trace
    headers["X-B3-SpanId"] = meta.message_id[:16]
    headers["X-B3-Sampled"] = "1"
    # 官方客户端的链路头还有 traceparent / b3 两个单值形式，一起发齐。
    span = meta.message_id[:16]
    headers["traceparent"] = f"00-{trace}-{span}-01"
    headers["b3"] = f"{trace}-{span}-1"
    return headers


def refresh_headers(account: Any, cfg: Any) -> Dict[str, str]:
    """refresh 端点：X-Refresh-Token 只允许出现在这里。

    ``X-Auth-Refresh-Source`` 是分域的：国内 ``workbuddy``，国际 ``plugin``。
    """
    realm = normalize_realm(getattr(account, "realm", "cn"))
    headers = common_headers(account, cfg)
    headers["X-Refresh-Token"] = getattr(account, "refresh_token", "") or ""
    headers["X-Auth-Refresh-Source"] = "workbuddy" if realm == "cn" else "plugin"
    uid = getattr(account, "uid", "") or ""
    if uid:
        headers["X-User-Id"] = uid
    domain = getattr(account, "domain", "") or realm_info(realm)["domain"]
    headers["X-Domain"] = domain
    enterprise = getattr(account, "enterprise_id", "") or ""
    if enterprise:
        headers["X-Enterprise-Id"] = enterprise
    return headers


def billing_headers(account: Any, cfg: Any) -> Dict[str, str]:
    """配额/签到/报表端点头：不走 common，但归属头族要保持一致。"""
    realm = normalize_realm(getattr(account, "realm", "cn"))
    headers = {
        "Authorization": f"Bearer {getattr(account, 'access_token', '') or ''}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-CodeBuddy-Request": "1",
        "Accept-Language": realm_info(realm)["language"],
        "Origin": realm_info(realm)["origin"],
        "Referer": realm_info(realm)["origin"] + "/",
        "User-Agent": resolve_user_agent(cfg, realm),
    }
    uid = getattr(account, "uid", "") or ""
    if uid:
        headers["X-User-Id"] = uid
    # 分域：Go 版一律发 X-Enterprise-Id + X-Device-Token，到了 global 域会把
    # "腾讯设备"这个词带过去，属于自报身份。这里按域分开。
    if realm == "global":
        headers["X-No-Enterprise-Id"] = "1"
        headers["X-Domain"] = realm_info(realm)["domain"]
        return headers
    enterprise = getattr(account, "enterprise_id", "") or ""
    if enterprise:
        headers["X-Enterprise-Id"] = enterprise
        headers["X-Tenant-Id"] = enterprise
    domain = getattr(account, "domain", "") or realm_info(realm)["domain"]
    headers["X-Domain"] = domain
    device_token = device_token_for(account, cfg)
    if device_token:
        headers["X-Device-Token"] = device_token
    return headers


def anonymous_headers(realm: str, cfg: Any) -> Dict[str, str]:
    """登录流程（还没有账号）用的头。"""
    info = realm_info(realm)
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": info["origin"],
        "Referer": info["origin"] + "/",
        "User-Agent": resolve_user_agent(cfg, realm),
        "X-CodeBuddy-Request": "1",
        "Accept-Language": info["language"],
    }


# --------------------------------------------------------------------------
# 设备令牌
# --------------------------------------------------------------------------

_device_cache: Dict[str, str] = {}
_device_cached_at: Dict[str, float] = {}
_device_lock = threading.Lock()


def device_token_for(account: Any, cfg: Any) -> str:
    """设备令牌优先级：账号自带 > 配置 > 文件 > 空（空则不注入）。

    这个头的来源是桌面客户端本地的 QIMEI/TuringShield SDK，网关伪造不出真实值，
    所以默认策略是"有就发、没有就不发"，而且**只在国内域名下发**：国际版客户端
    根本没有这个头，带上它反而成了跨账号关联键。
    """
    mode = (cfg.get("upstream.device_token_mode", "auto") if cfg else "auto") or "auto"
    if mode == "never":
        return ""
    if mode == "auto":
        if normalize_realm(getattr(account, "realm", "cn")) != "cn":
            return ""
        if product_of(account) == "cli":
            return ""
    own = getattr(account, "device_token", "") or ""
    if own:
        return own
    if cfg is None:
        return ""
    configured = cfg.get("upstream.device_token", "") or ""
    if configured:
        return configured
    path = cfg.get("upstream.device_token_file", "") or ""
    if not path:
        return ""
    with _device_lock:
        cached = _device_cache.get(path)
        if cached and _device_cached_at.get(path, 0.0) + 300.0 > util.now():
            return cached
        token = ""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                token = handle.read(1024).strip()
        except OSError:
            token = ""
        _device_cache[path] = token
        _device_cached_at[path] = util.now()
        return token


def clear_device_cache() -> None:
    with _device_lock:
        _device_cache.clear()
        _device_cached_at.clear()


# --------------------------------------------------------------------------
# 便于调试
# --------------------------------------------------------------------------


def describe(account: Any) -> str:
    realm = normalize_realm(getattr(account, "realm", "cn"))
    return json.dumps(
        {
            "realm": realm,
            "host": host_for(realm),
            "origin": realm_info(realm)["origin"],
            "user_agent": user_agent(realm),
            "machine": machine_id(account),
            "session": session_id(account),
        },
        ensure_ascii=False,
    )
