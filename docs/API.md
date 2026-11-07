# HTTP API 参考

所有接口都在同一个监听端口上（默认 `127.0.0.1:8788`），两套前缀：

- `/v1/*`、`/healthz`、`/status` —— **客户端面**（OpenAI 兼容）
- `/panel/*`、`/api/*` —— **管理面**（面板与脚本）

错误统一是 OpenAI 信封：

```json
{"error": {"message": "人类可读的说明", "type": "invalid_request_error", "code": "invalid_api_key"}}
```

`type` 取值：`invalid_request_error`（请求本身有问题）、`api_error`（上游/网关）、`rate_limit_error`（限流）、`authentication_error`。`code` 是稳定的字符串符号（`invalid_api_key` / `no_account` / `fingerprint_rejected` …），**程序判断请用 `code` 而不是 `message`**。

常见 `code` 对照（都是真机打出来过的，不是设想）：

| HTTP | `code` | 含义 | 该怎么做 |
|---|---|---|---|
| 401 | `invalid_api_key` | 密钥不认识 | 换密钥 |
| 400 | `missing_model` | 没给 `model` | 补上 |
| 400 | `model_not_allowed` | 密钥的模型白名单不允许 | 换模型或改密钥 |
| 400 | `model_unavailable` | 上游确实没有这个模型（`11102 service info not found`） | **换模型**；等多久都没用 |
| 400 | `fingerprint_rejected` | 请求体里有指纹（`11-128`） | 开 `prompt.mode=sanitize` 或改提示词；换号无效 |
| 400 | `prompt_too_long` | `11115` | 缩短上下文 |
| 400 | `tool_call_mismatch` / `reasoning_effort_unsupported` / `user_agent_rejected` | `11148` / `11150` / `12403` | 修请求 |
| 402 | `insufficient_quota` | 账号额度耗尽（`6004`/`402`） | 等次日 04:00 重置或换号 |
| 403 | `restricted` | 账号被上游风控（`11140`） | **停止重试**，换号会连坐 |
| 403 | `content_blocked` | 内容审核 / 边缘 WAF 拦的（HTML 错误页或审核文案，**不是** `11140`） | 改写触发内容或稍后再来；换号无效 |
| 429 | `rate_limited` | 真限流 | 看响应头 `Retry-After` 退避重试 |
| 503 | `no_account` | 池里没有可用账号（冷却/禁用/槽满） | 等冷却结束或去面板加号 |
| 503 | `upstream_unavailable` | 上游 5xx | 稍后重试（有 `Retry-After` 时按它来） |
| 503 | `upstream_timeout` | 上游超时 | 稍后重试；网关**不会**替你换号重发 |
| 503 | `account_auth_failed` | 账号令牌失效需要重新登录 | 面板里重新授权 |

> `restricted`（风控封号）和 `content_blocked`（内容审核）以前是同一个 403，现在分开了：
> 前者只能停手，后者换号一点用都没有——搞混的代价是"一次内容拦截把整池账号都打一遍"。
> 同理超时单独是个 `code`：网关不会换号重发，因为重发很可能让上游再生成一遍（重复计费）。
>
> `content_blocked` 在返回给你之前，网关已经悄悄试过一次"把系统提示词换成中性最小版再发"
> （`prompt.content_retry`，默认开）：如果你的 `system` 里写了项目名、身份设定之类容易被
> 审核盯上的内容，摘掉它们常常就能过。用户消息一个字都不改——改写用户的话等于替用户撒谎。
> 两次都被拦才会真的返回 403。

---

## 客户端面

### 鉴权

```http
Authorization: Bearer sk-xxxx
# 或
X-API-Key: sk-xxxx
```

密钥在面板「设置 → 客户端密钥」里创建，可绑定 `realm` 与模型白名单（支持 `prefix*` 通配）。**没配置任何密钥时是开放模式**（谁都能用，包括余额消耗），只建议在本机回环地址上这样跑。

### 定域

优先级：模型名前缀 `cn:` / `global:` → 密钥绑定的 realm → 请求头 `X-Realm` → query `?realm=` → 默认 `cn`。

```json
{"model": "global:gpt-4o-mini", "...": "..."}
```

### `POST /v1/chat/completions`

请求体就是 OpenAI 的形状。网关会做必要的改写（见 `docs/PROTOCOL.md` §2），你**不需要**也不应该手动构造上游字段。

```json
{
  "model": "deepseek-v3",
  "messages": [{"role": "user", "content": "你好"}],
  "stream": true,
  "max_tokens": 512,
  "temperature": 0.7,
  "tools": [{"type": "function", "function": {"name": "get_time", "parameters": {"type": "object", "properties": {}}}}]
}
```

字段支持情况：

| 字段 | 处理 |
| --- | --- |
| `stream` | 上游只支持流式；`false` 时由网关在本地聚合（对外仍是普通 JSON） |
| `max_tokens` / `max_completion_tokens` | 后者会被翻译；GPT 系模型自动抬到 ≥16 |
| `temperature` / `top_p` / `presence_penalty` / `frequency_penalty` | 原样透传 |
| `stop` | 字符串自动包成数组（上游只认数组） |
| `tools` / `tool_choice` | 工具名与 schema 会被修复；`tool_choice` 对象形式自动降级成字符串 |
| `response_format` | 透传；上游支持有限，不支持时会体现在错误里 |
| `reasoning_effort` / `thinking` | 推理模型按目录能力自动降级到最接近的支持档（`off/minimal/low/medium/high/xhigh/max`） |
| `stream_options` | 缺省补 `{include_usage: true}`，这样流式也能拿到 usage |
| 其它未知字段 | 透传（以 `_` 开头的键会被丢弃） |

**非流式响应**（标准 `chat.completion`）：

```json
{
  "id": "chatcmpl-3f2a...",
  "object": "chat.completion",
  "created": 1762500000,
  "model": "deepseek-v3",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "你好！", "reasoning_content": "（思考内容，若有）"},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 0}}
}
```

工具调用在 `message.tool_calls` 里，`finish_reason` 为 `tool_calls`。思考内容始终单独放在 `reasoning_content`，**不会**混进 `content`。

**流式响应**（`text/event-stream`）：

```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"你"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"reasoning_content":"先想一下"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":8,"total_tokens":20}}

data: [DONE]
```

流中可能出现的异常帧（客户端应作为错误提示并停止渲染）：

```json
{"error": {"message": "上游连接中断", "type": "upstream_stream_error"}}
```

发生这种情况时**不会**再补 `[DONE]`——收到 error 帧即终止，这是刻意与 OpenAI 保持一致的行为约定。

### `GET /v1/models`

```json
{"object": "list", "data": [{"id": "deepseek-v3", "object": "model", "created": 0, "owned_by": "workbuddy", "context_length": 65536, "max_output_tokens": 8192}]}
```

列表来自上游 `/v3/config`（三层回落，缓存 1 小时），已过滤掉补全类/图像/视频模型。`models.hidden` 里配置的模型（支持 `prefix*`）不会出现。

### `GET /v1/models/{id}`

单个模型对象；不存在返回 404 `model_not_found`。

### `GET /healthz`

```json
{"ok": true}
```

**不需要鉴权、不含任何账号信息**——存活探针该有的样子。

### `GET /status`

需要鉴权（或面板关闭时也要求 API key）。给人和监控看的摘要：

```json
{
  "ok": true,
  "listening": "127.0.0.1:8788",
  "accounts": {"cn": 3, "global": 1},
  "pool": {"total": 4, "ready": 3, "cooling": 1, "in_flight": 0, "credits": 812.5, "...": "..."},
  "usage": {"total": {"requests": 128, "ok": 126, "errors": 2, "prompt_tokens": 91234}, "...": "..."},
  "models": {"cn": 22, "global": 22}
}
```

---

## 管理面

仅在 `panel.enabled` 为真时存在；关闭时这些路径**完全 404**。

### 登录

```http
POST /panel/login
Content-Type: application/json

{"password": "admin"}
```

```json
{"ok": true, "token": "1762500000.9f3c...", "expires_at": 1762543200}
```

之后所有 `/api/*` 请求带上：

```http
X-Panel-Token: 1762500000.9f3c...
```

（也接受 query `?token=` 与 cookie `wb2a_panel`，后者是给浏览器 `curl` 调试用的。）密码错误返回 401 `bad_password`；同一 IP 60 秒内失败 5 次后返回 429 `too_many_attempts`。

### `GET /panel/status`（免鉴权）

面板自己用它判断该显示登录框还是主界面：

```json
{"enabled": true, "password_required": true, "is_default_password": true,
 "authenticated": false, "api_key_set": false, "version": "1.0.0"}
```

### 账号

| 方法与路径 | 说明 |
| --- | --- |
| `GET /api/accounts?realm=cn` | 账号列表（`realm` 省略则全部）。每行附加 `throttle_for`（秒）、`throttle_reason`（哪种窗口）、`in_flight` |
| `POST /api/accounts/add` | `{"realm":"cn","refresh_token":"...","note":"","proxy":""}`；只有 `access_token` 也能加 |
| `POST /api/accounts/login/start` | `{"realm":"cn"}` → `{"state":"...","url":"https://copilot.tencent.com/login?..."}` |
| `GET /api/accounts/login/poll?state=...` | `{"state":"pending"\|"ok"\|"expired"\|"error","account":{...}}`；`ok` 时账号已入库 |
| `POST /api/accounts/set` | `{"uid":"...","weight":2,"note":"...","group":"...","proxy":"...","enabled":true}` |
| `POST /api/accounts/{uid}/enable` / `disable` / `delete` | 启停 / 删除 |
| `POST /api/accounts/{uid}/test` | 真打一次最小 chat（`max_tokens: 16`）确认能用，返回 `{"ok":true,"model":"...","sample":"..."}` |
| `POST /api/accounts/{uid}/refresh` | 强制刷新令牌 |
| `POST /api/accounts/{uid}/checkin` | 每日签到（CN 域） |
| `POST /api/accounts/{uid}/balance` | 查余额并写回账号 |
| `POST /api/accounts/bulk` | `{"action":"test","uids":["..."]}` 或 `{"action":"disable","all":true,"realm":"cn"}`。`action` ∈ `enable`/`disable`/`test`/`refresh`/`balance`；单次最多 200 个；一个号失败不影响其余，返回 `{"ok":true,"total":N,"ok_count":M,"results":[{"uid","ok","detail"}]}` |
| `GET /api/accounts/export` | 下载账号备份（`Content-Disposition: attachment`，`Cache-Control: no-store`）。**含明文 refresh token**——不做脱敏，脱敏的备份 restore 不回来等于假备份 |
| `POST /api/accounts/import` | `{"accounts":[...]}`（也接受裸数组）。merge 语义：同 uid 覆盖，其余保留，**从不删除**；每行独立判定，坏行只计入 `skipped`。返回 `{"ok":true,"added":N,"updated":M,"skipped":K}` |

账号行形状（**永不含 token**）：

```json
{
  "uid": "u_123", "id": "u_123", "realm": "cn", "nickname": "小明", "email": "", "avatar": "",
  "group": "", "disabled": false, "note": "", "added_at": 1762400000, "last_used_at": 1762499000,
  "last_error": "", "state": "", "failures": 0,
  "credit": 812.5, "credit_at": 1762500000,
  "cooldown_until": 0, "cooldown_reason": "", "cooldown_level": 0,
  "balance_until": 0, "breaker_until": 0, "degrade_until": 0,
  "soft_streak": 0, "unscoped_streak": 0, "consecutive_fails": 0, "model_cooldowns": {},
  "in_flight": 0, "throttle_for": 0, "throttle_reason": "",
  "has_access_token": true, "has_refresh_token": true,
  "token_expires_at": 1765092000, "token_expired": false,
  "proxy": "", "device_token": "set", "enterprise_id": "abcd…", "enterprise": true,
  "domain": "", "product": "CodeBuddy", "source": "login", "static_key": "",
  "weight": 1.0, "tier": 0, "restricted_until": 0
}
```

注意几个字段的语义：`disabled`（不是 `enabled`，启用状态是它的反）、`token_expired` 由网关按当前时间算、`model_cooldowns` 是"模型 → 解禁时间戳"的映射、`throttle_for`/`throttle_reason` 是**所有**时间窗里最长的那个（只报冷却会让人误以为在看 429，实际可能是熔断）。

### 设置

| 方法与路径 | 说明 |
| --- | --- |
| `GET /api/settings` | 当前设置（**密码与 device token 不回明文**）；`tunables` 数组给出可在线修改的调优项（`path`/`label`/`kind`/`restart`/`value`） |
| `POST /api/settings` | `panel_password`（≥6 位）、`prompt_mode`（`passthrough`\|`sanitize`）、`api_key`、`listen`、`keys_add:[{key,name,realm,models}]`、`keys_delete:[name]`、`tunables:{配置路径: 值}` |

`tunables` 的写入是**逐项校验**的：`duration` 接受 `"30m"`/`"200ms"` 或纯数字（按秒），`int`/`float` 拒绝负数，`schedule.checkin_hour` 不超过 23，`pool.strategy.*` 只接受 `weighted_random`/`round_robin`/`sequential_exhaust`。任何一项非法整单返回 `400 bad_value`、一个都不写；不认识的路径静默忽略（前向兼容）。`server.max_payload_mb` 标了 `restart: true`，改完要重启进程。

### 观测

| 方法与路径 | 说明 |
| --- | --- |
| `GET /api/usage` | 台账汇总：总量、按天/按密钥/按账号/按模型、最近 50 条 |
| `GET /api/usage/recent?limit=50` | 最近请求明细 |
| `GET /api/logs?lines=200` | 日志尾部，返回 `{"lines":["...", "..."]}`（只读最后 256KB，不会把大文件读进内存；`lines` 上限 2000） |
| `GET /api/pool` | 池状态：可用/冷却/禁用、余额合计、各域策略、风控连坐剩余时间 |
| `GET /api/scheduler` | 后台任务清单与上次/下次运行时间 |
| `POST /api/scheduler/trigger` | `{"job":"keepalive"}` 立刻跑一次；不传 `job` 则跑全部到期任务 |

### 面板静态资源

`GET /`、`GET /panel`（→ 302 `/panel/`）、`GET /panel/`、`GET /panel/{index.html|style.css|app.js}`。文件名里的 `..`、`/`、`\` 一律拒绝（无目录穿越），HTML 带 `Cache-Control: no-cache`。

---

## 一个完整的脚本示例

```bash
BASE=http://127.0.0.1:8788

# 登录拿令牌
TOKEN=$(curl -s -X POST $BASE/panel/login -H 'Content-Type: application/json' \
        -d '{"password":"admin"}' | python -c 'import json,sys;print(json.load(sys.stdin)["token"])')

# 开始一次登录，把 url 交给浏览器
curl -s -X POST $BASE/api/accounts/login/start -H "X-Panel-Token: $TOKEN" \
     -H 'Content-Type: application/json' -d '{"realm":"cn"}'

# 轮询直到 state 完成（浏览器里授权完）
curl -s "$BASE/api/accounts/login/poll?state=<state>" -H "X-Panel-Token: $TOKEN"

# 看池状态
curl -s $BASE/api/pool -H "X-Panel-Token: $TOKEN"
```
