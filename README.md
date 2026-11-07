# workbuddy2api

把 WorkBuddy / CodeBuddy 的账号池，变成一个**任何 OpenAI 客户端都能用**的本地网关。

- 一个 `POST /v1/chat/completions`，兼容 OpenAI 请求/响应/流式（含 `tool_calls`、`reasoning_content`）。
- 多个账号自动轮换、失败转移、会话粘滞、余额与冷却管理。
- 自带中文 Web 面板：加号、测号、看余额、看用量、改设置，不用开命令行。
- **零依赖**：只用 Python 标准库（3.9+），不需要 `pip install` 任何东西，也不需要 node / Go 运行时。

这个项目是对两个已停更项目的重写融合：[workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)（Python 单文件 7000 行）与 [workbuddy2api-panel](https://github.com/linguo2625469/workbuddy2api-panel)（Go 实现）。协议细节、错误码表、冷却策略大部分来自这两个仓库的实测结论，代码是重新组织的模块化实现。

> ⚠️ 仅供个人学习研究使用。账号来自你自己的 WorkBuddy / CodeBuddy 订阅；本项目不提供账号、不绕过付费，风控与频率限制仍由上游判定。
>
> 📌 **本项目仅供本人自用，不承诺任何可用性**：没有固定维护时间，没有 issue 响应承诺，上游接口一变就可能直接失效；作者不对使用后果、账号风险、数据丢失或任何直接/间接损失负责。请自己评估风险，不要用于生产环境。

---

## 30 秒跑起来

不需要 clone 后安装：在仓库根目录直接跑即可。想装成命令，见下面的「安装为命令」。

```powershell
# 1) 启动（首次会在当前目录生成 data/ 与日志）
python -m wb2api serve

# 输出示例
# workbuddy2api 1.0.0
# listen : http://127.0.0.1:8788
# panel  : http://127.0.0.1:8788/panel/  (默认密码 admin)
# openai : base_url=http://127.0.0.1:8788/v1  api_key=<面板里创建>
```

```powershell
# 2) 打开面板，登录（初始密码 admin，登录后请立刻改掉）
start http://127.0.0.1:8788/panel/

# 3) 「账号」页 → 添加账号 → 点「登录链接」→ 浏览器完成授权 → 自动回填
# 4) 「设置」页 → 客户端密钥 → 新建，拿到 sk-xxxx
```

```powershell
# 5) 使用
curl http://127.0.0.1:8788/v1/chat/completions ^
  -H "Authorization: Bearer sk-xxxx" ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"deepseek-v3\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

Python：

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8788/v1", api_key="sk-xxxx")
print(client.chat.completions.create(
    model="deepseek-v3",
    messages=[{"role": "user", "content": "用三句话说明什么是反向代理"}],
).choices[0].message.content)
```

任何支持「自定义 OpenAI 兼容 endpoint」的客户端（Cherry Studio、NextChat、Cline、Continue、IDE 插件…）都能接：填 base_url 与 key 即可。

### 安装为命令（可选）

```powershell
pip install .
wb2a serve          # 或 workbuddy2api serve
```

- 构建只需要 setuptools（有网时 pip 会自动拉进隔离环境，不用手工装）。
- 安装后 `wb2a` 与 `workbuddy2api` 两个命令等价；数据目录默认取当前工作目录下的 `data/`，可用 `WB_DATA_DIR` 改。
- 不想装也行：`python -m wb2api serve` 与本机源码目录完全等价，功能上没有区别。

---

## 它具体做了什么

上游是腾讯 CodeBuddy / WorkBuddy 的私有 API：只能流式、请求头有一整套会话指纹、`tool_choice` 只认字符串、`stop` 只认数组、`max_completion_tokens` 会被拒……直接拿 OpenAI SDK 打过去基本必失败。网关做的事情是：

| 层   | 处理                                                                                                                                                                                                                                |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 请求体 | `stream` 强制为真、`max_completion_tokens` 翻译、GPT 系 `max_tokens` 抬到 ≥16、`tool_choice` 对象→字符串、`stop` 字符串→数组、工具名/schema 修复、`developer→system`、图片部件归一、工具消息配对（合并/重排/剔孤儿）、推理模型 `thinking` + `reasoning_effort` 双发、历史 `reasoning_content` 回填 |
| 请求头 | 完整会话头族（`X-Conversation-ID` 等 5 个 id、`X-Machine-ID`/`X-Session-ID` 由 uid 派生、`X-CodeBuddy-Request: 1`）、按域切换 UA / Origin / `X-Domain`                                                                                                |
| 响应  | SSE → OpenAI 流式（含 `delta.reasoning_content`、`tool_calls` 增量）；非流式在本地聚合；正文里漏出来的 DSML 工具标记被抠成真正的 `tool_calls`                                                                                                                        |
| 账号  | 令牌自动刷新（单飞，防一次性 refresh token 被并发烧掉）、48 小时前置保活、余额查询、每日签到                                                                                                                                                                           |
| 池   | 加权随机选号、会话粘滞（同一对话尽量打同一账号，否则上游 prefix 缓存全废）、并发槽、软冷却/熔断/降级/风控连坐五类时间窗、失败转移最多 3 次                                                                                                                                                      |
| 观测  | 每请求一行 JSONL 台账（tokens、TTFT、命中缓存、账套、账号）、面板实时 KPI                                                                                                                                                                                   |

---

## 目录结构

```
wb2api/                 网关本体（全部零依赖）
  httpserver.py         HTTP/1.1 + 路由 + chunked SSE + 面板会话（自己写的 300 行框架）
  identity.py           上游请求头与会话指纹
  authflow.py           登录（state 轮询）与令牌刷新
  upstream.py           连接池 / 超时 / 错误分类
  adapters/chat.py      OpenAI ⇄ 上游 的请求与响应改写
  markup.py             正文里漏出的 DSML 工具调用提取（流式增量）
  sanitize.py           出站请求体的指纹净化（可选）
  models.py             模型目录（/v3/config 三层回落 + 缓存）
  billing.py            余额 / 签到 / 用量明细
  pool.py               账号池、五类时间窗、选号策略
  sticky.py             会话亲和
  service.py            一次请求的完整编排（重试、故障转移、SSE 中继、记账）
  scheduler.py          后台保活/余额/签到/清理
  routes_openai.py      /v1/* 客户端面
  routes_admin.py       /api/* 面板面
  web/static/           面板前端（原生 JS，无构建）
  app.py / cli.py       装配与命令行
tests/                  132 个单测（`python -m unittest discover -s tests -t .`）
docs/                   架构、协议、API、部署、贡献指南
```

---

## 配置

配置文件默认是 `./config.json`（可用 `-c` 或 `WB_CONFIG` 指定）。**首次运行自动生成**，字段与默认值见 [config.example.json](config.example.json)。

改配置的三种方式，优先级从高到低：

1. 环境变量（见下表）
2. `config.json`
3. 内置默认值

| 环境变量                       | 配置键                             | 说明                                  |
| -------------------------- | ------------------------------- | ----------------------------------- |
| `WB_LISTEN`                | `listen`                        | 监听地址，如 `0.0.0.0:8788`               |
| `WB_DATA_DIR`              | `data_dir`                      | 数据目录（账号、密钥、台账、运行态）                  |
| `WB_API_KEY`               | `api_key`                       | 初始客户端密钥（面板建过密钥后由面板接管）               |
| `WB_PANEL_PASSWORD`        | `panel.password`                | 面板密码                                |
| `WB_PANEL_ENABLED`         | `panel.enabled`                 | `0` 则完全关闭面板（`/panel`、`/api` 直接 404） |
| `WB_MAX_IN_FLIGHT`         | `pool.max_in_flight`            | 单账号并发上限                             |
| `WB_CREDIT_FLOOR`          | `pool.credit_floor`             | 余额低于它不再派收费模型（0 = 关闭）                |
| `WB_UPSTREAM_TIMEOUT`      | `upstream.timeout_seconds`      | 建连/首字节超时                            |
| `WB_UPSTREAM_IDLE_TIMEOUT` | `upstream.idle_timeout_seconds` | 流式读空闲超时                             |
| `WB_DEVICE_TOKEN`          | `upstream.device_token`         | 上游设备令牌（一般留空，自动从账号派生/文件读取）           |
| `WB_CLIENT_VERSION` 等      | `upstream.*`                    | 伪装的上游客户端版本号                         |

Docker / 服务化部署见 [docs/DEPLOY.md](docs/DEPLOY.md)。

---

## 账号是怎么来的

网关自己**没有**账号。三种添加方式（面板「账号」页或 `POST /api/accounts/add`）：

1. **登录链接（推荐）**：点「登录链接」→ 拿到一串 `https://copilot.tencent.com/login?...` 或 `https://www.workbuddy.ai/login?...` → 浏览器打开、登录你自己的账号 → 面板轮询到 `state` 完成后自动落库。整个过程不需要手抄 token。
2. **粘贴 refresh token**：如果你已经从别处拿到 `refreshToken`，直接贴进来，网关会立刻用一次性刷新换回 access token 并算出到期时间。
3. **粘贴 access token**：能跑，但过期需要手动续（面板有「续期」按钮）。

添加成功后网关会自动：拉余额、认 uid/企业 ID、（CN 域）签到。

---

## 选域：cn 还是 global

两套上游互不相通，账号属于哪个域就只能用哪个域：

| 域        | 上游                    | 适合                   |
| -------- | --------------------- | -------------------- |
| `cn`     | `copilot.tencent.com` | 国内版 CodeBuddy / 企业账号 |
| `global` | `www.workbuddy.ai`    | 国际版 WorkBuddy        |

三种指定方式（优先级从高到低）：模型名前缀 `cn:deepseek-v3` → 请求头 `X-Realm: global` → query `?realm=global` → 客户端密钥上绑定的 `realm` → 默认 `cn`。

---

## 出问题时先看这几处

| 现象                                    | 大概率原因                                              | 处置                                        |
| ------------------------------------- | -------------------------------------------------- | ----------------------------------------- |
| `503 no_account`                      | 没有可用账号（全在冷却/被禁/余额为空）                               | 面板「概览」看池状态；「账号」页看冷却原因                     |
| `400 fingerprint_rejected` (`11-128`) | 请求体里带了被逐字匹配的特征（Claude Code / Codex CLI 的系统提示等）     | 把 `prompt.mode` 设为 `sanitize`，或换掉客户端自带提示词 |
| `403 restricted` (`11140`)            | 触发风控                                               | **换号没用，会连坐封号**。停一到两小时，检查客户端/UA 是否被识别      |
| `429 rate_limit_error`                | 账号软限流                                              | 网关已按上游给的 reset 时钟冷却；加号或换域                 |
| `402 quota`                           | 额度耗尽                                               | 换号 / 充值；次日 04:00 自动解冻                     |
| `400 model_unavailable`               | 上游目录里列了但**没接入**的模型（`11102 service info not found`） | 换一个模型；等或换号都没用                             |
| 面板打不开                                 | 面板被关（`panel.enabled=false`）或端口不对                   | `python -m wb2api doctor`                 |

`python -m wb2api doctor` 会检查配置文件、数据目录、监听地址并把问题逐条列出来。

面板「账号」页的**余额**来自上游计费接口（`POST /v2/billing/meter/get-user-resource`），会把一个账号下所有资源包  
（赠送包、每日签到包、套餐包）加起来；「概览」里的**消耗积分**来自每次请求的 usage（上游按请求扣分），  
所以两者应当对得上，对不上就说明有请求绕过了网关。

---

## 开发

```powershell
python -m unittest discover -s tests -t .   # 132 个单测，约 3 秒
python -m wb2api serve --no-scheduler        # 调试时关掉后台调度
```

代码风格、模块边界、如何加一个新端点见 [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)，设计取舍见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，上游协议细节见 [docs/PROTOCOL.md](docs/PROTOCOL.md)。
