# 贡献指南

## 环境

```bash
python --version          # 需要 3.9+
python -m unittest discover -s tests -t .   # 全部单测，约 3 秒
python -m wb2api serve --no-scheduler        # 本地跑起来（关掉后台任务便于观察）
python -m wb2api doctor                      # 配置自检
```

不需要虚拟环境，也不需要安装任何依赖——**项目本体零第三方依赖，请保持这一点**。新增 `import` 只能用标准库；如果某个功能非第三方不可，先开 issue 讨论，而不是直接提 PR。

## 代码约定

- **中文注释，讲"为什么"**。`# 这里必须单飞，否则第二个请求拿着已消费的 refresh token` 有用；`# 刷新令牌` 没用。
- **单文件不超过约 700 行**。超过就说明该拆模块了。
- 公开函数写类型注解，返回 `Dict[str, Any]` 这类结构时在 docstring 里写清形状。
- 错误用 `ServiceError(message, status, code, kind)`；`code` 是稳定的字符串符号（`no_account`、`fingerprint_rejected`），不要用数字状态码当 `code`。
- 日志用 `get_logger("模块名")`，**永远不要打印 token**（用 `util.mask_secret`）。
- 改中文源码**只用编辑器或 `edit`/`write` 工具**。绝对不要用 PowerShell 的 `Get-Content | Set-Content` 批量正则替换：它按 GB18030 解码 UTF-8 再写回，会把整份中文注释变成乱码并让文件语法失效（本仓库真的踩过这个坑）。

## 测试

```bash
python -m unittest discover -s tests -t .            # 全量
python -m unittest tests.test_pool -v                # 单个文件
python -m unittest tests.test_chat.TestX.test_y      # 单个用例
```

写新测试时注意三件容易踩的事：

1. **SSE 假数据必须在 `data:` 行之间留空行**。`sse.iter_sse` 只在空行处 dispatch，否则所有 `data:` 会被拼成一条非法 JSON，症状是"内容为空、usage 为 0"。测试里用 `sse_lines(*lines)` 构造。
2. **`Account` 没有 `enabled` 构造参数**。`enabled` 是 property（写回 `disabled`），构造时用 `disabled=False`。
3. **`Request.client_ip` 是 property**，不是方法。

测试不联网：上游一律用 `FakeClient`（见 `tests/test_service.py`）脚本化响应。

## 模块边界

新增能力前先决定它属于哪一层（详细表格见 `docs/ARCHITECTURE.md`）：

| 要改的东西 | 去哪个文件 |
| --- | --- |
| 请求/响应字段改写 | `wb2api/adapters/chat.py` |
| 上游请求头、会话 id | `wb2api/identity.py` |
| 新的上游端点调用 | 对应模块（`billing.py` / `models.py` / `authflow.py`）+ `service.py` 里包一层 |
| 错误码 → 冷却动作 | `service.py::_absorb` + `pool.py::note_*` |
| 新管理接口 | `routes_admin.py`（面板前端在 `web/static/app.js`） |
| 新客户端接口 | `routes_openai.py` |
| 配置项 | `config.py::DEFAULTS`，然后跑 `python tools/gen_example_config.py` 重新生成示例 |

**配置项必须真的被读取**：加键的同时确认代码里 `cfg.get("你.的.键")` 存在，并在 `config.example.json` 里出现。本仓库曾经有一批"写进默认值但没人读"的键，改了配置等于没改——有单测之前只能靠人眼，所以改完请顺手 grep 一次。

## 加一个端点（示例）

```python
# wb2api/routes_admin.py
router.add("GET", r"/api/foo", lambda req: _foo(panel, req))

def _foo(panel: Panel, req: Any) -> Response:
    panel.require(req)          # 第一件事永远是鉴权
    return json_response({"ok": True})
```

然后在 `docs/API.md` 的表格里补一行——文档和代码不同步的话，下一个人会以为接口不存在。

## 加一个上游错误码

1. `service.py` 顶部加常量：`CODE_FOO = 12345`；
2. `_absorb()` 里加分支：这笔失败该记到哪个窗口（`note_soft_rate` / `note_hard_credit` / `note_restricted` / `block_model` / `note_unknown_failure`），以及**要不要换号**；
3. `_client_error()` 里加映射：回给客户端什么状态码与 code；
4. `tests/test_service.py` 加用例：断言"换号次数"和"账号窗口状态"两个事实；
5. `docs/PROTOCOL.md` 的错误码段补一行。

换号与否的判断依据只有一条：**这个问题换账号能解决吗**。限额、单号额度、账号级风控 → 能；指纹、内容违规、请求体形状 → 不能，换号只是多封几个。

## 提交

- 一个 PR 做一件事。重构和新功能不要混在一起。
- 提交信息用中文写清"改了什么 + 为什么"，例如：
  `修复 status 端点匿名可读：它包含余额与账号数量`
- 改完跑一遍全量单测与 `python -m wb2api doctor`，把结果贴进 PR 描述。
- **不要**提交 `data/`、`logs/`、`config.json`（`.gitignore` 已覆盖）。

## 常见坑（踩过的）

| 症状 | 原因 |
| --- | --- |
| `unittest discover` 报 `Start directory is not importable` | 缺 `tests/__init__.py` |
| 测试里 chat 内容为空、usage 全 0 | 假 SSE 缺空行分隔 |
| 每个 chat 请求都 500 `'str' object is not callable` | 把 `request.client_ip` 写成了 `request.client_ip()` |
| 面板改了配置却不生效 | 键名对不上，见上面「配置项必须真的被读取」 |
| PowerShell 里中文日志乱码 | 控制台编码是 cp936，不影响功能；看日志请用面板或编辑器 |
| 改了 `models.py`/`routes_admin.py` 后语法报 `invalid non-printable character` | 被 PowerShell 管道写过，见「代码约定」最后一条 |
