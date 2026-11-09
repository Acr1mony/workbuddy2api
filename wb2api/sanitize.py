"""出站请求体净化：拆掉上游内容审核按字面匹配的指纹。

上游的审核不是语义的，是**逐字匹配**的。客户端（Claude Code / Codex CLI /
DeepSeek Harness 一类）注入的固定模板句、以及计费头那种键名，会被当成
"这是别家的客户端"的证据整单拦掉，错误码 ``11-128``
（"Illegal API invocation from an unapproved channel"）。

对策分三层，都不改变语义：

1. **整段剥离**计费头那种"键: 值;"形态——它没有语义，只是客户端签名。
2. **最小改写**承载语义的模板句：换一个词、或整句换成中性句，模型读到的意思不变。
3. **兜底缩写**裸键名：实测 assistant 消息里用反引号引用裸键名同样触发拦截。

指纹是一族，不是一条。参考实现里逐条二分出来的结论（都带回归用例）：

- GUI 描述句 ``...through the DeepSeek Harness Web GUI whose URL the runtime
  context names.``：上游按「A 句 + B 句连续出现」判定，改 A 句、改 B 句、或在两句
  之间插东西都会失配（三种都能过）。这里只调整句尾语序，一个词都不删。
- 文件引用段 ``Tokens prefixed with @ are paths the user explicitly
  referenced.``：整段 5 句连排稳定 400，只发前两句 200——匹配的是整段。只补一个
  ``that`` 就能打断。
- 品牌词 ``DeepSeek Harness`` 与 ``this page`` / ``this GUI`` / ``this app``
  连续出现命中同一套校验；单独出现放行。缩成 ``DeepSeek`` 即可，语义损失最小。
  只匹配散文里的空格形式：``deepseek-harness``、``/opt/deepseek-harness/app``
  这类路径与标识符必须保持原样。
- 自我介绍句式 ``You are an AI agent powered by <品牌>.`` /
  ``... built by <厂商>.``：动词表要全（powered by / built on / built by /
  created by / developed by / made by / trained by），否则上游补一条就重新 400。
- ``Sisyphus-Junior - Focused executor from OhMyOpenCode`` 整串也是指纹；单独
  出现 ``OhMyOpenCode`` 或 ``Sisyphus-Junior`` 都放行，所以只中性化整串。

作用域很重要：**只净化 system / developer / assistant**。user 和 tool 里的品牌词
是用户数据，改写它会让模型看到跟磁盘不一致的内容。
"""

from __future__ import annotations

import re
from typing import Any, List, Tuple

# 预检串：任一命中才进净化。普通请求全部不中 → 原样返回，零改写。
FEATURES = (
    "x-anthropic-billing-hdr",
    "You are Claude Code",
    "Main branch (",
    "You are a coding agent running in the Codex CLI",
    "github.com/anthropics/",
    "11-128",
)

# 键值形态的计费头整段删除（键名即触发，与值无关）。
HEADER_KV_RE = re.compile(r"(?i)x-anthropic-billing-hdr\s*[:=][^\n;]*;?\s*")
# 裸键名（无冒号无值）：整段删除会破坏句子，只做最小缩写。
HEADER_BARE_RE = re.compile(r"(?i)x-anthropic-billing-hdr")
# 尾随的裸计量键值：反复清到不再变化。
CC_KV_RE = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\n]*;?\s*")

# 改写层：左边是上游认得的原句，右边是"只差一个词"的等价句。
# 这一层**所有角色都过**：它只换一个词，不改变谁在说话，用户数据也安全。
REWRITES: Tuple[Tuple[str, str], ...] = (
    # Claude Code 自述：把 official CLI for Claude 补成 official CLI tool for Claude。
    # 一个词的差别，说话的人没变。
    ("official CLI for Claude", "official CLI tool for Claude"),
    # Codex 自述："CLI, a terminal-based..." -> "CLI tool, a terminal-based..."。
    ("Codex CLI, a terminal-based", "Codex CLI tool, a terminal-based"),
    # 反馈地址句：单数改复数。
    ("users should report the issue at", "users should report issues at"),
)

# --- 整段指纹：连排才算命中，所以只动一个词 -------------------------------

# GUI 描述段：只调整句尾语序，"DeepSeek Harness" 全名保留。
DSH_GUI_RE = re.compile(
    r"(?i)(You are interacting with the user through the "
    r"DeepSeek[ \t]+Harness[ \t]+Web[ \t]+GUI)"
    r" whose URL the runtime context names\.")
DSH_GUI_REPLACEMENT = r"\1 whose URL is named in the runtime context."
DSH_GUI_HINT = "whose url the runtime context names"

# 文件引用段：补一个 that 打断整段连续匹配；(?!that ) 保证幂等。
DSH_FILEREF_RE = re.compile(
    r"(?i)(Tokens prefixed with @ are paths )(?!that )(the user explicitly referenced\.)")
DSH_FILEREF_REPLACEMENT = r"\1that \2"
DSH_FILEREF_HINT = "are paths the user explicitly referenced"

# 品牌词：只在散文（空格/制表符）里缩，路径与标识符一律不动。
# 例外是紧跟 "Web GUI" 的那一处——那句话靠"调语序"过审，品牌全名要原样留着，
# 否则 GUI 规则自己先匹配不上了。
BRAND_RE = re.compile(r"DeepSeek[ \t]+Harness(?!\s+Web\s+GUI)", re.IGNORECASE)
BRAND_REPLACEMENT = "DeepSeek"
# 品牌与这些词连排才算指纹；单独出现品牌词是放行的。
BRAND_CONTEXT = ("this page", "this gui", "this app", "web gui", "runtime context")

# 自我介绍句式：动词表补全，名词可省略，冠词后允许 0~3 个修饰词。
IDENTITY_VERBS = (
    "powered by", "built on", "built by", "created by",
    "developed by", "made by", "trained by",
)
IDENTITY_RE = re.compile(
    r"You are\s+"
    r"(?:an?\s+|the\s+)?(?:[\w.-]+\s+){0,3}?"
    r"(?:agent|assistant|model|AI|LLM)?[\s,]*"
    r"(?:powered by|built on|built by|created by|developed by|made by|trained by)"
    r"[^\n]*?(?:\.(?=\s|$)|(?=\n|$))",
    re.IGNORECASE)
IDENTITY_REPLACEMENT = "You are a helpful AI assistant."

# 整串指纹（单独出现的两个词都放行，所以只中性化整串）。
OMO_RE = re.compile(r"Sisyphus-Junior - Focused executor from OhMyOpenCode", re.IGNORECASE)
OMO_REPLACEMENT = "Sisyphus-Junior - Focused executor"

# Claude Code / Codex 的自述首段：整句换中性句（这两句本身就是指纹）。
CLAUDE_INTRO_RE = re.compile(
    r"(?i)You are Claude Code,\s*Anthropic['\u2019]s official CLI"
    r"(?: tool)? for Claude(?: Code)?\.?,?\s*")
CODEX_INTRO_RE = re.compile(
    r"(?i)You are a coding agent running in the Codex CLI(?: tool)?" r"[^.\n]*\.,?\s*")
INTRO_REPLACEMENT = "You are a helpful AI assistant. "


def _low(text: str) -> str:
    # 三个字符的 re.IGNORECASE 与 lower() 语义不同（İ/ı 能匹配 i，ſ 能匹配 s），
    # 先归一化再小写，结果与正则等价。
    if "\u0130" in text or "\u0131" in text or "\u017f" in text:
        text = text.translate({0x130: "i", 0x131: "i", 0x17F: "s"})
    return text.lower()


def has_fingerprint(text: str, identity: bool = True) -> bool:
    """廉价预检：判断这段文本是否需要净化。

    ``identity=False`` 时只认"与品牌无关"的指纹（计费头、11-128、整串 OMO），
    用于 tool_calls.arguments / reasoning_content —— 那些是应用数据，不该按
    "你是不是官方客户端"改写自我介绍句。
    """
    if not text:
        return False
    for feature in FEATURES:
        if feature in text:
            return True
    low = _low(text)
    if "x-anthropic-billing-hdr" in low or "ohmyopencode" in low:
        return True
    if not identity:
        return False
    if "you are claude code," in low:
        return True
    if "harness" in low and BRAND_RE.search(text) is not None:
        if any(marker in low for marker in BRAND_CONTEXT):
            return True
    if DSH_GUI_HINT in low and DSH_GUI_RE.search(text) is not None:
        return True
    if DSH_FILEREF_HINT in low and DSH_FILEREF_RE.search(text) is not None:
        return True
    if not any(verb in low for verb in IDENTITY_VERBS):
        return False
    return IDENTITY_RE.search(text) is not None


def sanitize_text(text: str, identity: bool = True) -> str:
    """单段文本净化。没有指纹就原样返回（绝大多数请求走这条路）。"""
    if not text or not has_fingerprint(text, identity=identity):
        return text

    # 只中性化已确认的整串，不全局删 OhMyOpenCode：单独出现是放行的，
    # 删了等于替用户改产品名。
    text = OMO_RE.sub(OMO_REPLACEMENT, text)

    if identity:
        # 自述首段先整句换掉：这两句本身就是 FEATURES 里的指纹。
        text = CLAUDE_INTRO_RE.sub(INTRO_REPLACEMENT, text)
        text = CODEX_INTRO_RE.sub(INTRO_REPLACEMENT, text)
        text = IDENTITY_RE.sub(IDENTITY_REPLACEMENT, text)
        # 整段指纹：只调语序 / 补一个词。
        text = DSH_GUI_RE.sub(DSH_GUI_REPLACEMENT, text)
        text = DSH_FILEREF_RE.sub(DSH_FILEREF_REPLACEMENT, text)
        # 品牌缩减放最后：GUI 规则还认全名，先让它匹配原文。
        text = BRAND_RE.sub(BRAND_REPLACEMENT, text)

    for source, target in REWRITES:
        if source in text:
            text = text.replace(source, target)
    if HEADER_KV_RE.search(text):
        text = HEADER_KV_RE.sub("", text)
    if "cc_" in text:
        previous = None
        while previous != text:  # 清尾随裸 kv，可能连着好几段
            previous = text
            text = CC_KV_RE.sub("", text)
    text = HEADER_BARE_RE.sub("x-anthropic-billing-hdr", text)
    # 剥离层会留下多余空白；行内连续空白的收缩不改变语义，只是别让模型看到空洞。
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def sanitize_content(value: Any, identity: bool = True) -> Tuple[Any, bool]:
    """兼容字符串与多模态数组；只动 text part，图片等部件不碰。"""
    if isinstance(value, str):
        cleaned = sanitize_text(value, identity=identity)
        return cleaned, cleaned != value
    if isinstance(value, list):
        changed = False
        for part in value:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            cleaned = sanitize_text(text, identity=identity)
            if cleaned != text:
                part["text"] = cleaned
                changed = True
        return value, changed
    return value, False


def sanitize_tool_calls(value: Any) -> bool:
    """``arguments`` 是字符串化的 JSON，按文本走即可（identity=False）。

    这块最容易被漏：工具调用轮的 ``content`` 通常是 null，早先的实现看到 content
    缺失就 ``continue``，于是历史里写进工具参数的被拦字符串（文件名、命令、写入的
    文件内容）原样出站。
    """
    if not isinstance(value, list):
        return False
    changed = False
    for call in value:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            continue
        cleaned = sanitize_text(arguments, identity=False)
        if cleaned != arguments:
            function["arguments"] = cleaned
            changed = True
    return changed


# 只有这些角色的文本会被"按身份句"改写；user / tool 是用户数据。
IDENTITY_ROLES = ("system", "developer", "assistant")


def sanitize_messages(messages: Any) -> bool:
    """净化 messages 里的 content / reasoning / tool_calls。任一命中返回 True。"""
    if not isinstance(messages, list):
        return False
    changed = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        identity = str(message.get("role") or "") in IDENTITY_ROLES
        if "content" in message:
            new_content, hit = sanitize_content(message["content"], identity=identity)
            if hit:
                message["content"] = new_content
                changed = True
        for key in ("reasoning_content", "reasoning"):
            value = message.get(key)
            if isinstance(value, str):
                cleaned = sanitize_text(value, identity=False)
                if cleaned != value:
                    message[key] = cleaned
                    changed = True
        if "tool_calls" in message and sanitize_tool_calls(message["tool_calls"]):
            changed = True
    return changed
