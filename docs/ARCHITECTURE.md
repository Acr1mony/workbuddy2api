# 架构与设计取舍

这份文档解释「为什么长这样」。想改代码之前读一遍，能省掉几个已知的坑。

## 一句话结构

```
客户端 ──/v1/chat/completions──> routes_openai ──> service ──> pool ──> upstream ──> 腾讯上游
                                                    │        └──> sticky（会话亲和）
                                                    ├──> adapters.chat（请求/响应改写）
                                                    ├──> authflow（令牌）
                                                    └──> usage（台账）
面板浏览器 ──/api/*──> routes_admin ──> 上面那些模块的只读/管理视图
```

`service.py` 是唯一的编排者：它拿 `Request`、选出账号、改写请求、打上游、把 SSE 重写成 OpenAI 格式、记账。HTTP 层（`httpserver.py`）不认识业务，业务层不认识 HTTP。

## 为什么是「零依赖的标准库」

两个参考实现里，Python 那个是 7000 行单文件、Go 那个要编译工具链。零依赖的收益很具体：

- 部署 = 复制目录 + `python -m wb2api serve`，没有 `pip install`、没有虚拟环境、没有 wheel 兼容问题；
- 目标机器可能根本装不上 pip（内网、旧 Python、受限权限）；
- 依赖少 = 供应链面小，这类"反代"项目要处理别人的 token，越少第三方代码越好。

代价是自己写了 300 行 HTTP 框架（`httpserver.py`）：只支持 HTTP/1.1、不支持 keep-alive 之外的复用细节、SSL 交给前置 nginx/caddy。够用，而且出问题能读得懂。

## 一次 chat 请求的完整路径

1. **鉴权**：`Authorization: Bearer <key>` 或 `X-API-Key`。没有配置任何密钥时进入"开放模式"（只建议在 `127.0.0.1` 上这么用）。
2. **定域**：`cn:` / `global:` 模型名前缀 → 密钥绑定 → `X-Realm` → `?realm=` → 默认 `cn`。
3. **改写请求**：`chat.prepare_request()`，顺序固定（见 `docs/PROTOCOL.md` §2）。顺序不能随便动：比如工具配对必须在 `thinking` 注入之前，否则回填 `reasoning_content` 会打断配对判定。
4. **选号**：会话亲和优先 → 池策略（加权随机/轮询/顺序耗尽）→ 成本层过滤 → 并发槽。
5. **打上游**：`identity.chat_headers()` 造一整套会话头，`upstream.post(raw_body=...)` 直接发预编码字节（不能重新 `json.dumps`，紧凑分隔符本身就是指纹的一部分）。
6. **失败处理**：`_absorb()` 把错误归类到池的记账动作，返回"是否换号"。**流式一旦开始写响应头就不能换号。**
7. **回写**：流式把上游 SSE 逐帧重写成 OpenAI chunk；非流式在本地聚合成一个 `chat.completion`。
8. **记账**：一行 JSONL，`finally` 里写（客户端中断也写，上游已经计费了）。

## 六个关键决定

### 1. 冷却窗口持久化，失败叙述不持久化

账号的 `cooldown_until`、`breaker_until`、`restricted_until` 等**时间窗**写盘；`last_error` 这类**文字**只留在内存。

原因很实际：如果一个 429 早已恢复，但它的错误文字被持久化，重启后面板仍然显示"限流中"，人会去查一个不存在的问题。窗口是事实，叙述是当时的解释。

### 2. 风控（403 / `11140`）绝不换号

参考实现的实测结论：同一份请求在一轮里轮换号码，把 7 个账号**连着**封了。风控是按内容/指纹判定的，换号只是在扩大伤害面。所以：

- `11140` → 该号冷却 2 小时，**不换号**，直接把 403 返回客户端；
- 900 秒内有 2 个不同账号被风控 → 整个域暂停 30 分钟（`pool.restriction_hold_seconds`）。

同理，`11-128`（指纹/渠道）也是"改内容"能过、"换账号"没用的问题，所以不换号。

### 3. "没有可用账号" 返回 503，不是 429

429 的语义是"你太快了，等会儿再来"，客户端会按退避重试；而池空是"现在没有资源"，重试没意义。混用这两个码会让客户端在半分钟内打几百次空请求。所以本项目里：429 只用于真的限流，池空/上游超时/账号认证失败都用 503，并且**从不返回 504**。

### 4. 会话亲和优先于负载均衡

上游有 prefix 缓存。实测数据（来自参考实现）：没有粘滞时首字延迟 1.5s → 5-6s，且 59% 的 prompt token 白烧。所以选号顺序是"先看这个对话上次用哪个号"，命中就继续，只在失败时 `demote` 掉那一个（其余候选保留——它们的缓存里可能还有这个对话）。

### 5. 令牌刷新必须单飞

refresh token 是**一次性**的：两个线程同时刷新，第二个拿着已消费的 token 去换，返回 `12153`，整条链失效、账号废掉。`AuthFlow` 用 `util.KeyedLock` 按账号串行化，并且刷新成功后立刻落盘。

### 6. 面板关闭时路径 404，而不是 401

`panel.enabled=false` 时 `/panel` 与 `/api` **完全不注册**。返回 401 等于告诉扫描器"这里有个需要密码的后台"，404 什么都不说。同理，面板返回的账号行永远不含 token（`Account.masked()`）。

## 数据目录

```
data/
  accounts.json     账号（含 token —— 这是本项目的最高机密，注意文件权限）
  api_keys.json     客户端密钥（含 models / realm 绑定）
  runtime.json      运行态：面板签名密钥、池游标
  usage/
    usage-YYYY-MM-DD.jsonl   每请求一行，保留 N 天（默认 7）
logs/
  wb2api.log        滚动 8MB × 5
```

写盘全部走 `util.atomic_write_json`（同目录临时文件 + `os.replace`），断电不会留下半截 JSON。台账用单次 `os.write` 追加，因为读方是 `except: continue`，**丢一行比写坏一行更糟**。

## 模块边界

| 模块 | 可以依赖 | 不该做的事 |
| --- | --- | --- |
| `httpserver.py` | 标准库 | 不认识业务名词 |
| `identity.py` | `util`, `config` | 不发请求 |
| `upstream.py` | `util`, `config` | 不重试、不选号（重试属于 service） |
| `adapters/chat.py` | `util`, `markup`, `sanitize` | 不做 IO |
| `pool.py` | `store`, `sticky`, `util` | 不发请求（只记账） |
| `service.py` | 以上全部 | 不碰 `Request` 以外的 HTTP 细节 |
| `routes_*.py`, `web/` | `httpserver`, `service` | 不直接打上游 |
| `scheduler.py` | `service` | 不起第二个 HTTP 服务器 |

新增能力时先问"它属于哪一层"：改写请求体 → `adapters/chat.py`；新的上游端点 → 各自模块 + `service` 里的方法；新的管理操作 → `routes_admin.py`。

## 已知取舍与未尽事项

- **面板会话是 `X-Panel-Token` 令牌**，不是 cookie 优先；cookie 也支持（`wb2a_panel`），为了兼容 `curl` 调试。令牌是 HMAC 签名，进程重启后仍有效（密钥在 `runtime.json`），改密码后旧令牌失效需重新登录——这一条依赖密钥轮换，当前实现不轮换，属已知弱点。
- **没有多进程共享状态**。池游标、亲和表都是进程内内存。要跑多实例请让它们各自持有不同的账号子集，或用前置负载均衡做会话粘滞。
- **模型目录缓存 1 小时**，负缓存 5 分钟。换号后目录可能短暂滞后。
- **真机端到端未自动化**：单测覆盖到"没有账号 → 503"为止；带真实 token 的端到端需要人工跑（见 `docs/DEPLOY.md` 末尾的自检清单）。
