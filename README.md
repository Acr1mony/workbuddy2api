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

### 一条命令部署到 Linux 服务器

```bash
curl -fsSL https://raw.githubusercontent.com/Acr1mony/workbuddy2api/main/scripts/install.sh | bash
```

它会：检查/安装 Python 3.9+ → 取源码到 `/opt/workbuddy2api`（非 root 则 `~/.wb2api`）→ 在独立 venv 里安装（**不动系统 Python**）→ 建好数据目录 → 有 systemd 就注册 `wb2api.service` 并启动。

常用参数（写在命令前面即可）：

```bash
# 换端口、对外监听、跳过 systemd
curl -fsSL .../install.sh | WB2A_PORT=9000 WB2A_BIND=0.0.0.0 WB2A_SERVICE=0 bash

# 自定义安装目录与数据目录
curl -fsSL .../install.sh | WB2A_DIR=/srv/wb2a WB2A_DATA_DIR=/var/lib/wb2a bash

# 装指定版本（tag / 分支）
curl -fsSL .../install.sh | WB2A_REF=v1.2.0 bash
```

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WB2A_DIR` | root 为 `/opt/workbuddy2api`，否则 `~/.wb2api` | 代码与 venv 的位置 |
| `WB2A_DATA_DIR` | `<WB2A_DIR>/data` | 账号、密钥、台账、日志 |
| `WB2A_PORT` / `WB2A_BIND` | `8788` / `127.0.0.1` | 监听端口与地址 |
| `WB2A_SERVICE` | `auto` | `1`/`0` 强制装/不装 systemd |
| `WB2A_REF` | `main` | 分支或 tag |
| `WB2A_TOKEN` | 空 | **仓库是私有的**时给一个 `contents:read` 的 PAT |
| `WB2A_SRC` | 空 | 本地源码目录（离线安装/自测，给了就不下载） |

装完它会把面板地址、`wb2a login` 的用法和下一步提示直接打出来。升级＝重跑同一条命令（`data/` 不会被碰）。

> 仓库是公开的，上面那行 `curl` 直接可用。如果你 fork 到了私有仓库，下载时给个 PAT 即可：`WB2A_TOKEN=ghp_xxx`。

### 安装为命令（可选）

```powershell
pip install .
wb2a serve          # 或 workbuddy2api serve
```

- 构建只需要 setuptools（有网时 pip 会自动拉进隔离环境，不用手工装）。
- 安装后 `wb2a` 与 `workbuddy2api` 两个命令等价；数据目录默认取当前工作目录下的 `data/`，可用 `WB_DATA_DIR` 改。
- 不想装也行：`python -m wb2api serve` 与本机源码目录完全等价，功能上没有区别。

### 在终端里加账号（服务器上没有浏览器时）

```powershell
wb2a login                  # 打印授权链接，等你在任意设备上点完
wb2a login --realm global   # 另一个域
wb2a login --no-wait --json # 只要链接（脚本/远程会话用）
```

网关正在跑时，`login` 会把这次登录**交给面板进程**去完成——同一份 `accounts.json` 只能有一个写者，CLI 自己落盘会被面板下一次保存覆盖。面板没跑时它才自己起 auth 流程，令牌照样写回同一个数据目录。

---

## 它具体做了什么

上游是腾讯 CodeBuddy / WorkBuddy 的私有 API：只能流式、请求头有一整套会话指纹、`tool_choice` 只认字符串、`stop` 只认数组、`max_completion_tokens` 会被拒……直接拿 OpenAI SDK 打过去基本必失败。网关做的事情是：

| 层   | 处理                                                                                                                                                                                                                                |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 请求体 | `stream` 强制为真、`max_completion_tokens` 翻译、GPT 系 `max_tokens` 抬到 ≥16、`tool_choice` 对象→字符串、`stop` 字符串→数组、工具名/schema 修复、`developer→system`、图片部件归一、工具消息配对（合并/重排/剔孤儿）、推理模型 `thinking` + `reasoning_effort` 双发、历史 `reasoning_content` 回填、glm 类模型缺档位时补最省的一档（否则 11150） |
| 请求头 | 完整会话头族（`X-Conversation-ID` 等 5 个 id、`X-Machine-ID`/`X-Session-ID` 由 uid 派生、`X-CodeBuddy-Request: 1`）、按域切换 UA / Origin / `X-Domain`                                                                                                |
| 响应  | SSE → OpenAI 流式（含 `delta.reasoning_content`、`tool_calls` 增量）；非流式在本地聚合；正文里漏出来的 DSML 工具标记被抠成真正的 `tool_calls`；每个响应都带 `X-Request-Id`（台账里能按它反查这一条） |
| 账号  | 令牌自动刷新（单飞，防一次性 refresh token 被并发烧掉）、6 小时前置保活（刷越勤越危险）、余额查询、每日签到                                                                                                                                                                       |
| 池   | 加权随机选号、会话粘滞（同一对话尽量打同一账号，否则上游 prefix 缓存全废；一个会话最多钉 3 轮就换页，见 `session_sticky.page_size`）、并发槽、软冷却/熔断/降级/风控连坐五类时间窗、失败转移最多 3 次                                                                                                                                                      |
| 防封  | 同账号最小间隔 200ms、定时任务随机错峰、403 分三路处置（11140 风控 / 边缘 WAF / 凭证失效，风控绝不换号）、**一分钟内两个号被 WAF 拦就整体停手一分钟（一枪都不打上游）**、内容审核打回时摘掉系统提示词重试一次、媒体请求不绑会话、连接池按凭据分池、半读连接不回池、链路抖动记在出口上而不是账号上、每天一次活跃上报（点连登，企业号跳过）、配额到期用 16 token 的探针验证而不是让真实流量去撞（`upstream.send_stable_ids=false` 可关掉 chat 上的机器 id 头族、`models.min_refresh_seconds` 给目录刷新节流）                                                        |
| 观测  | 每请求一行 JSONL 台账（tokens、TTFT、命中缓存、账套、账号、`request_id`、`attempts`）、面板实时 KPI |
| 稳   | 并发槽在 `finally` 里释放（异常、客户端半路断开都不会漏）、状态文件坏了就拒绝启动而不是静默清空、连接读写有超时（慢客户端拖不死网关）、面板页面带 CSP/X-Frame-DENY 且不带 CORS |

---

## API Key 能看见什么

GET /v1/models 会按这把 key 的 models（白名单，支持 prefix*）和 excluded_models 过滤：**看得见的就等于调得动的**。直接点名一个没开的模型会回 403 model_not_allowed（不是 404——模型是好的，是你的 key 没开），客户端不用靠猜。

列表里每条还带 `max_output_tokens`（上游给的输出预算）和 `context_length` / `context_window`（可配，见下）——客户端靠这两个数决定什么时候该自己截断历史，缺了它只能瞎猜。

## 模型别名

客户端里写死的模型名往往不是你账号能用的那个（`gpt-4o`、`claude-sonnet-4` 之类）。在配置里登记一行就能把名字接过来，不用改客户端：

```json
{
  "models": {
    "aliases":       { "gpt-4o": "deepseek-v4-flash", "sonnet": "default-1.2" },
    "context_lengths": { "deepseek-v4-flash": 128000 }
  }
}
```

- 别名会出现在 `/v1/models` 里（带 `alias_of` 指向真名），也继承真名的能力与限制。
- 出站请求的 `model` 会被换成真名；响应头回一句 `X-Model-Alias: gpt-4o`，让你知道自己写的名字被解析成了什么。
- key 的白名单里写别名或写真名都放行——否则"配了别名却调不动"。
- 别名指向一个不存在的模型时，它不会出现在列表里（宁可看不见，也不要调过去报 11102）。

## 面板

`http://127.0.0.1:8788/panel/`，五个页面，全部零构建（原生 JS + 一份 CSS），断网也能用：

| 页面 | 能做什么 |
| --- | --- |
| 概览 | KPI（请求/失败/token/缓存命中/积分/平均耗时）、账号池条形图与风控连坐剩余时间、WAF 波次提示（出口被盯上时告诉你停手多久）、被多个号同时锁住的模型、调度任务表（可「立即执行」）、最近 20 条请求（含错误列与请求 id，可整表复制） |
| 账号 | 搜索、按状态筛选（可用/冷却/暂停/停用）、按余额/昵称/最近使用排序、分页；每行直接说清**为什么现在不接单**（冷却/熔断/降权/风控/连败各自倒计时）；点「详情」看这个号的完整因果——五类窗口各自剩多久、哪些模型被单独封了、它一共花掉多少积分、最近 20 次请求；备注、分组、权重**行内改完即存**；勾选后批量测试/续期/查额度/签到/上报/启停/**暂停**；**一键导出/导入备份** |
| 用量 | 按模型 / 账号 / API key / 天四个维度，含缓存命中率与积分 |
| 日志 | 尾部日志，级别关键字过滤（支持正则写法）、自动刷新、一键复制 |
| 设置 | 监听地址、默认 API key、面板密码、客户端密钥增删（**明文只显示一次**，可给每把 key 设并发上限）、提示词模式，以及 17 个可在线改的调优参数（并发、冷却、熔断阈值、`credit_floor`、请求节奏…），改完立刻生效 |

「暂停」和「停用」不是一回事：停用是"这个号不要了"，暂停是"先按住它、令牌和余额都留着"，排查问题或留备份时用它。

几条刻意的设计：

- **批量操作是串行的**：40 个账号同时打上游等于自己制造一个风口，第二个之后每个都随机间隔 0.8–2 秒。
- **状态徽章按窗口分开**：只写"冷却中"会让人以为在等 429，其实可能是熔断或 11140 风控。
- **危险操作把后果写进确认文案**：删除会连本地令牌一起删、不可恢复。
- **备份文件不脱敏**：导出的 JSON 里有明文 refresh token——脱敏的备份 restore 不回来等于假备份，所以宁可老实导出 + 在文件里写警告。命令行同样有：`wb2a accounts export -o backup.json` / `wb2a accounts import backup.json`。
- **刷新不丢 tab**：地址是 `#accounts` 这种，收藏或刷新都停在原页。
- 面板令牌存 `localStorage`，401 自动回登录页；页面 3 秒/5 秒/15 秒分档轮询，标签页隐藏时停轮询。

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

配置文件的查找顺序（`-c` / `WB_CONFIG` 优先）：

1. 显式指定的路径；
2. 工作目录下的 `config.json`（如果本来就有）；
3. **数据目录下的 `config.json`**（`<data_dir>/config.json`，默认 `./data/config.json`）。

前两种之外不会往工作目录里写东西：以前默认路径写死成 `./config.json`，用 `python -m wb2api` 从仓库根启动时，面板改一次设置就会在仓库里凭空生成一个配置文件——现在这些改动落到数据目录里，重启后依然生效。字段与默认值见 [config.example.json](config.example.json)。

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
| `403 content_blocked`                 | 内容审核 / 边缘 WAF 拦的（不是 `11140`）                       | 跟账号无关，换号一样被拦：改写触发内容或稍后再来                 |
| `503 upstream_timeout`                | 上游 120 秒没响应                                        | 稍后重试；网关**不会**替你换号重发（重发可能重复计费）             |
| `429 rate_limit_error`                | 账号软限流                                              | 网关已按上游给的 reset 时钟冷却；加号或换域                 |
| `402 quota`                           | 额度耗尽                                               | 换号 / 充值；次日 04:00 自动解冻                     |
| `400 model_unavailable`               | 上游目录里列了但**没接入**的模型（`11102 service info not found`） | 换一个模型；等或换号都没用                             |
| 面板打不开                                 | 面板被关（`panel.enabled=false`）或端口不对                   | `python -m wb2api doctor`                 |
| 面板点了「登录」没反应 / 登录后还看到登录框              | 浏览器缓存了旧版 `app.js`（页面脚本没跑起来，或被拦）                     | **Ctrl+F5** 强制刷新；页面 2 秒后会自己提示这一句          |

面板脚本没跑起来时页面是"死"的：登录按钮不响应，人只会以为密码错了。所以：

- `/panel/` 一律 `Cache-Control: no-store`，静态资源带 `?v=<版本号>` 指纹，升级后浏览器自然会拿到新文件；
- 页面加载 2 秒后检查 `window.__WB2A_READY`，没置位就直接显示「页面脚本没有正常运行，请 Ctrl+F5」而不是干等；
- 版本号（`data-version` 属性）和 `/panel/status` 返回的 `version` 不一致时会弹提示，说明前端文件是旧的。

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
