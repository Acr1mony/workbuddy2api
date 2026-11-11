"""workbuddy2api - 把 WorkBuddy / CodeBuddy 账号变成 OpenAI 兼容 API 的网关。

模块地图（想改哪块看哪块）：

    config.py       配置默认值 / 文件 / 环境变量 / 校验
    store.py        账号、网关 API Key、运行时状态的持久化
    identity.py     上游要求的 UA / 请求头 / 设备与会话指纹
    authflow.py     OAuth 登录（无 PKCE，state 轮询）与一次性 refresh token
    upstream.py     上游 HTTP 客户端、连接复用、错误码归一化
    egress.py       出口（host+proxy）级退避：链路抖动不记在账号头上
    proxies.py      出口代理池：按域/权重挑出口，坏了自动冷却
    diagnostics.py  失败报文留存（脱敏、限大小、按天封顶）
    reqlog.py       逐请求明细（可按状态/模型/账号/关键字检索）
    billing.py      额度查询 / 签到 / 用量上报（宽容解析）
    probe.py        账号体检（真打一次最小 chat，只读不记账）
    sse.py          SSE 解析与 OpenAI 分块编码
    markup.py       DSML 内联工具调用的增量提取
    sanitize.py     出站请求体反指纹净化（可选）
    models.py       模型目录（/v3/config 优先，TTL + 负缓存 + 磁盘缓存 + 刷新节流）
    adapters/       协议转换（chat.completions ↔ Anthropic messages）
    pool.py         选号、五类冷却窗口、会话粘滞、并发槽
    sticky.py       会话 → 账号亲和表（按页换号，防单号被对话绑死）
    service.py      请求编排：鉴权、换号、流式边界、记账
    scheduler.py    令牌保活（提前 6h）、余额刷新、签到、活跃上报、配额探针、落盘
    usage.py        用量台账与面板聚合
    httpserver.py   极简 HTTP 框架（路由 / 请求 / 响应 / 面板会话）
    routes_openai.py  客户端面：/v1/chat/completions、/v1/messages、/v1/responses、/v1/models、/healthz、/status
    routes_admin.py   面板 API：账号（含备份）、设置、用量、日志、调度、请求明细、出口池
    web/            面板静态页（零构建）
    service.py      编排：鉴权 → 选号 → 出站 → 转发 → 记账（Relay/Failover 两个 mixin 在此组装）
    relay.py        流式转发与记账（不选号、不换号）
    failover.py     错误处置：谁该换号、谁绝对不能换
    errors.py       错误翻译成客户端信封（纯函数）
    endpoint_responses.py  /v1/responses 的编排（ResponsesEndpoint，与 messages 同构）
    replay.py       失败报文回放：净化前后对照 + 用存下来的体重发一次
    app.py          装配：把上面这些拼成一个应用
    cli.py          命令行入口（serve / doctor / version / accounts）
"""

VERSION = "1.10.2"
__version__ = VERSION
#: 面板上"跳到仓库"的地址；只在这里写一次，页面用 ``{{REPO}}`` 填。
PROJECT_URL = "https://github.com/Acr1mony/workbuddy2api"
