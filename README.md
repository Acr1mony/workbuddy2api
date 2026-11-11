# workbuddy2api

把 WorkBuddy / CodeBuddy 的账号池，变成一个**任何 OpenAI 客户端都能用**的本地网关。

- 一个 `POST /v1/chat/completions`，兼容 OpenAI 的请求、响应与流式（含 `tool_calls`、`reasoning_content`）；另有一个 `POST /v1/messages`，给只会说 Anthropic 协议的客户端。
- 多个账号自动轮换、失败转移、会话粘滞、余额与冷却管理。
- 自带中文面板：加号、测号、看余额、看用量、改设置，不用开命令行。
- **零依赖**：只用 Python 标准库（3.9+），不需要装任何包，也不需要 node / Go 运行时。

> ⚠️ 仅供个人学习研究使用。账号来自你自己的 WorkBuddy / CodeBuddy 订阅；本项目不提供账号、不绕过付费，风控与频率限制仍由上游判定。
>
> 📌 本项目仅供本人自用，不承诺任何可用性：没有固定维护时间，也没有 issue 响应承诺；上游接口一变就可能直接失效。作者不对使用后果、账号风险、数据丢失或任何直接/间接损失负责。

---

## 快速开始

在仓库根目录直接跑，不需要先安装：

```powershell
# 1) 启动（首次会在当前目录生成 data/ 与 logs/）
python -m wb2api serve
```

启动后会打印面板地址与 base_url。然后：

```powershell
# 2) 打开面板，用初始密码 admin 登录，第一件事是改掉它
start http://127.0.0.1:8788/panel/

# 3) 「账号」页 → 添加账号 → 点「登录链接」→ 浏览器里登录 → 自动回填
# 4) 「设置」页 → 客户端密钥 → 新建，拿到 sk-xxxx
```

```powershell
# 5) 用起来
curl http://127.0.0.1:8788/v1/chat/completions ^
  -H "Authorization: Bearer sk-xxxx" ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"deepseek-v4-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

Python：

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8788/v1", api_key="sk-xxxx")
print(client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[{"role": "user", "content": "用三句话说明什么是反向代理"}],
).choices[0].message.content)
```

任何支持「自定义 OpenAI 兼容 endpoint」的客户端（Cherry Studio、NextChat、Cline、Continue、IDE 插件…）都能接：填 base_url 与 key 即可。

### 装成命令（可选）

```powershell
pip install .
wb2a serve            # 或 workbuddy2api serve
```

`wb2a` 与 `workbuddy2api` 等价；数据目录默认是当前工作目录下的 `data/`，可用 `WB_DATA_DIR` 改。不装也完全一样——`python -m wb2api serve` 就是它的全部。

### 部署到 Linux 服务器

```bash
curl -fsSL https://raw.githubusercontent.com/Acr1mony/workbuddy2api/main/scripts/install.sh | bash
```

它会检查/安装 Python 3.9+、把代码放到 `/opt/workbuddy2api`（非 root 则 `~/.wb2api`）、在独立 venv 里安装（不动系统 Python）、建好数据目录，有 systemd 就注册服务并启动。升级＝重跑同一条命令，`data/` 不会被碰。

常用参数写在命令前面：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WB2A_PORT` / `WB2A_BIND` | `8788` / `127.0.0.1` | 监听端口与地址（对外服务常用 `0.0.0.0`） |
| `WB2A_DATA_DIR` | `<安装目录>/data` | 账号、密钥、台账、日志 |
| `WB2A_DIR` | root 为 `/opt/workbuddy2api`，否则 `~/.wb2api` | 代码与 venv 的位置 |
| `WB2A_SERVICE` | `auto` | `1`/`0` 强制装/不装 systemd |
| `WB2A_REF` | `main` | 分支或 tag，如 `v1.9.3` |

```bash
# 换端口、对外监听、跳过 systemd
curl -fsSL .../install.sh | WB2A_PORT=9000 WB2A_BIND=0.0.0.0 WB2A_SERVICE=0 bash
```

服务器上没有浏览器时，用命令行加账号：

```powershell
wb2a login                  # 打印授权链接，在任意设备上点完即可
wb2a login --realm global   # 另一个域
```

同一条命令还有 `wb2a accounts list / test / enable / disable / remove / balance`，以及 `export` / `import` 做备份。

---

## 三种协议

| 入口 | 给谁用 |
| --- | --- |
| `POST /v1/chat/completions` | OpenAI SDK、各种兼容 OpenAI 的客户端 |
| `POST /v1/messages`（+ `/v1/messages/count_tokens`） | Claude Code 这类只会说 Anthropic 协议的客户端 |
| `POST /v1/responses` | Codex CLI、新版 OpenAI SDK（只认 Responses 协议） |

三个入口走同一套选号、重试、冷却、台账：

```bash
curl -s http://127.0.0.1:8788/v1/messages \
  -H "x-api-key: sk-..." -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
  -d '{"model":"deepseek-v4-flash","max_tokens":256,"messages":[{"role":"user","content":"hi"}]}'
```

Claude Code 里当自定义端点填：`ANTHROPIC_BASE_URL=http://127.0.0.1:8788`、`ANTHROPIC_API_KEY=sk-...`；
Codex CLI 填：`OPENAI_BASE_URL=http://127.0.0.1:8788/v1`、`OPENAI_API_KEY=sk-...`，`wire_api` 选 `responses`。

## 模型

`GET /v1/models` 只列出**这把 key 能调**的模型（按 key 的白名单与排除项过滤），每条带 `max_output_tokens` 与可配的 `context_length`。点名一个没开的模型会回 `403 model_not_allowed`——模型是好的，是 key 没开。

客户端里写死的模型名往往不是账号能用的那个，可以在配置里登记别名，不用改客户端：

```json
{
  "models": {
    "aliases":         { "gpt-4o": "deepseek-v4-flash", "sonnet": "default-1.2" },
    "context_lengths": { "deepseek-v4-flash": 128000 }
  }
}
```

别名会出现在模型列表里（带 `alias_of`），出站时换成真名，响应头回一句 `X-Model-Alias`。

## 面板

`http://127.0.0.1:8788/panel/`，六个页面，零构建、断网可用：

| 页面 | 能做什么 |
| --- | --- |
| 概览 | 请求/失败/token/积分、延迟分位与错误率、账号池状态、调度任务、最近请求 |
| 账号 | 搜索筛选排序分页；每行说清**为什么现在不接单**（冷却/熔断/风控/余额各自倒计时）；点「详情」看这个号的全部因果（窗口剩多久、哪些模型被单独封、余额什么时候过期、花掉多少积分、最近请求）；备注/分组/权重行内改；批量测试/续期/查额度/签到/启停/暂停；导出导入备份 |
| 用量 | 按模型 / 账号 / key / 天四个维度 |
| 日志 | 尾部日志，关键字过滤 |
| 请求 | 逐请求明细，以及**失败报文**：上游拒绝时的原话，连我们发出去的请求体一起留（脱敏后）。每条报文可以「看看净化会改成什么」（不发上游）和「用净化后的体重发一次」（真发、花积分、结果进台账） |
| 设置 | 监听、面板密码、客户端密钥（明文只显示一次，可设并发上限与每分钟速率、点选限定模型）、出口代理池、提示词模式、17 个可在线调的参数 |

「暂停」和「停用」不是一回事：停用是"这个号不要了"，暂停是"先按住它、令牌和余额都留着"。

---

## 账号是怎么来的

网关自己**没有**账号，三种添加方式：

1. **登录链接（推荐）**：点「登录链接」→ 浏览器打开、登录你自己的账号 → 面板自动落库，不用手抄 token。
2. **粘贴 refresh token**：贴进来即可，网关会立刻换回 access token 并算出到期时间。
3. **粘贴 access token**：能跑，但过期要手动点「续期」。

加成功后会自动拉余额、认 uid，CN 域还会顺手签到。定时任务会在令牌过期前保活，不需要你管。

## 选 cn 还是 global

两套上游互不相通，账号属于哪个域就只能用哪个域：

| 域 | 上游 | 适合 |
| --- | --- | --- |
| `cn` | `copilot.tencent.com` | 国内版 CodeBuddy / 企业账号 |
| `global` | `www.workbuddy.ai` | 国际版 WorkBuddy |

指定方式优先级：模型名前缀 `cn:deepseek-v4-flash` → 请求头 `X-Realm` → query `?realm=` → 密钥上绑定的 realm → 默认 `cn`。

---

## 配置

配置文件按这个顺序找：`-c` / `WB_CONFIG` 指定 → 工作目录下已有的 `config.json` → **数据目录下的 `config.json`**（默认 `./data/config.json`）。面板里改的设置写进数据目录那份，不会污染工作目录。全部字段与默认值见 [config.example.json](config.example.json)。

优先级：环境变量 > `config.json` > 内置默认值。

| 环境变量 | 配置键 | 说明 |
| --- | --- | --- |
| `WB_LISTEN` | `listen` | 监听地址，如 `0.0.0.0:8788` |
| `WB_DATA_DIR` | `data_dir` | 数据目录 |
| `WB_API_KEY` | `api_key` | 初始客户端密钥（面板建过密钥后由面板接管） |
| `WB_PANEL_PASSWORD` | `panel.password` | 面板密码 |
| `WB_PANEL_ENABLED` | `panel.enabled` | `0` 则完全关闭面板（`/panel`、`/api` 直接 404） |
| `WB_MAX_IN_FLIGHT` | `pool.max_in_flight` | 单账号并发上限 |
| `WB_CREDIT_FLOOR` | `pool.credit_floor` | 余额低于它不派收费模型（0 = 关闭） |
| `WB_UPSTREAM_TIMEOUT` | `upstream.timeout_seconds` | 建连/首字节超时 |
| `WB_DEVICE_TOKEN` | `upstream.device_token` | 上游设备令牌（一般留空，自动派生） |

其余常用开关在面板「设置」里都能点，改完立刻生效，包括：冷却与熔断阈值、请求节奏、并发上限、提示词模式、网关追加提示词（接在 system 末尾，不覆盖客户端那段）。

### 出口代理池

多个出口能显著降低"同一台机器高频调用"的观感。账号可以单独绑代理（一个号一个出口最稳），没绑的走池子：

```json
{
  "pool": {
    "proxies": [
      "http://user:pass@10.0.0.11:3128",
      {"url": "socks5://10.0.0.12:1080", "realm": "global", "weight": 2}
    ]
  }
}
```

坏出口会自己冷却；**全部冷却时不会退回直连**（退了就等于把真实 IP 交出去），而是等最早解禁的那个。面板里可以随时增删、停用、清冷却。

---

## 出问题时先看这里

| 现象 | 大概率原因 | 处置 |
| --- | --- | --- |
| `503 no_account` | 没有可用账号（冷却/禁用/暂停/并发槽满） | 面板「概览」看池状态，「账号」页看原因 |
| `400 fingerprint_rejected`（`11-128`） | 请求体里带了被逐字匹配的特征（`DeepSeek Harness` 的描述句、Claude Code / Codex CLI 的系统提示等） | 默认已开 `prompt.mode=sanitize` 自动改写；还不行就看「请求 → 失败报文」里我们发出去的内容 |
| `403 restricted`（`11140`） | 触发风控 | **换号没用，会连坐封号**。停一到两小时，检查客户端与 UA |
| `403 content_blocked` | 内容审核 / 边缘 WAF 拦的（不是 `11140`） | 与账号无关，改写触发内容或稍后再来 |
| `429 rate_limit_error` | 账号被限流 | 网关已按上游给的重置时钟冷却；加号或换域 |
| `402 insufficient_quota` | 额度耗尽 | 换号或充值；次日 04:00 自动恢复 |
| `400 model_unavailable` | 目录里列了但上游其实没接入的模型 | 换一个模型，等多久都没用 |
| `503 upstream_timeout` | 上游超时 | 稍后重试（网关不会替你换号重发，重发可能重复计费） |
| 面板「登录」点了没反应 / 登录后还看到登录框 | 浏览器缓存了旧版脚本 | **Ctrl+F5**；页面 2 秒后会自己提示这一句 |
| 面板打不开 | 面板被关或端口不对 | `python -m wb2api doctor` |

面板上的**余额**来自上游计费接口（把所有资源包加起来），**消耗积分**来自每次请求的 usage——两者对不上说明有请求绕过了网关。

---

## 更多文档

- [docs/API.md](docs/API.md)：完整接口、错误码、响应头
- [docs/DEPLOY.md](docs/DEPLOY.md)：systemd / Docker / nginx（SSE 必须关 buffering）
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：设计取舍
- [docs/PROTOCOL.md](docs/PROTOCOL.md)：上游协议实测细节
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)：开发与提交约定
