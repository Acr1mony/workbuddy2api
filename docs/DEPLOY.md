# 部署

目标：**一个目录 + 一个进程**。没有数据库、没有 Redis、没有编译步骤。

## 前置

- Python 3.9+（Windows 上装 3.9-3.12 都行；3.13 未测试）
- 能访问 `copilot.tencent.com`（cn 域）或 `www.workbuddy.ai`（global 域）
- 想暴露到公网的话，再准备一个 nginx / caddy

从源码跑：

```bash
git clone <this repo> workbuddy2api && cd workbuddy2api
python -m wb2api serve
```

或者直接打包带走（零依赖，目录即产物）：

```bash
python -m zipfile -c wb2api.zip wb2api pyproject.toml config.example.json README.md docs
# 目标机器解压后：python -m wb2api serve
```

> 想用 `pip install .` 也可以（`pyproject.toml` 里 `dependencies = []`），但没有额外收益。

---

## 第一次部署的顺序

1. 启动：`python -m wb2api serve`——首启会生成 `config.json`、`data/`、`logs/`。
2. 立刻改面板密码：面板 →「设置」→ 面板密码（默认 `admin`）。或用环境变量 `WB_PANEL_PASSWORD`。
3. 加号：面板 →「账号」→ 添加账号 → 登录链接。
4. 建客户端密钥：面板 →「设置」→ 客户端密钥 → 新建（记下 `sk-...`，只显示一次）。
5. 自检：`curl -H "Authorization: Bearer sk-..." http://127.0.0.1:8788/v1/models`。
6. 再考虑往外暴露（见最后一节）。

---

## Windows

### 后台常驻（推荐 NSSM）

```powershell
# 装好 nssm 后
nssm install workbuddy2api "C:\Python312\python.exe" "-m wb2api serve"
nssm set workbuddy2api AppDirectory "D:\workbuddy2api"
nssm set workbuddy2api AppStdout "D:\workbuddy2api\logs\stdout.log"
nssm set workbuddy2api AppStderr "D:\workbuddy2api\logs\stderr.log"
nssm set workbuddy2api Start SERVICE_AUTO_START
nssm start workbuddy2api
```

### 不想装 NSSM：任务计划程序

新建任务 → 触发器「登录时」或「开机时」→ 操作：

```
程序:     C:\Python312\pythonw.exe
参数:     -m wb2api serve
起始于:   D:\workbuddy2api
```

用 `pythonw.exe`（无控制台窗口），勾掉「如果任务运行超过 72 小时则停止」。

### 防火墙

只在本机用不需要动防火墙（`listen` 默认 `127.0.0.1:8788`）。要让局域网其他机器用，改成 `0.0.0.0:8788` 并放行端口——**同时务必设置客户端密钥**，否则整个局域网都能花你的额度。

---

## Linux（systemd）

`/etc/systemd/system/workbuddy2api.service`：

```ini
[Unit]
Description=workbuddy2api gateway
After=network-online.target

[Service]
Type=simple
User=wb2a
WorkingDirectory=/opt/workbuddy2api
ExecStart=/usr/bin/python3 -m wb2api serve
Restart=always
RestartSec=5
Environment=WB_DATA_DIR=/var/lib/workbuddy2api
# 数据目录里有 token，权限要收紧
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd -r -s /usr/sbin/nologin wb2a
sudo mkdir -p /var/lib/workbuddy2api && sudo chown wb2a: /var/lib/workbuddy2api
sudo systemctl daemon-reload && sudo systemctl enable --now workbuddy2api
sudo journalctl -u workbuddy2api -f
```

---

## Docker

`Dockerfile`：

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY wb2api ./wb2api
COPY pyproject.toml config.example.json README.md ./

ENV WB_LISTEN=0.0.0.0:8788 \
    WB_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8788/healthz',timeout=3)"

CMD ["python", "-m", "wb2api", "serve"]
```

```bash
docker build -t workbuddy2api .
docker run -d --name wb2a -p 127.0.0.1:8788:8788 \
  -v wb2a-data:/data \
  -e WB_PANEL_PASSWORD='换成强密码' \
  --restart unless-stopped workbuddy2api
```

数据卷 `/data` 里有账号 token，别用匿名卷了事——出问题时你得能备份它。

---

## 反代到公网

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name wb.example.com;

    ssl_certificate     /etc/letsencrypt/live/wb.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/wb.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8788;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE 必需：不缓冲，且读超时要够长
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        chunked_transfer_encoding on;
    }
}
```

`proxy_buffering off` 是硬要求：开着的话 nginx 会把 SSE 攒成一坨再发，流式体验全废（表现为"等很久，然后一次性吐出全部内容"）。

### caddy

```
wb.example.com {
    reverse_proxy 127.0.0.1:8788 {
        flush_interval -1
    }
}
```

`flush_interval -1` 即"每帧立刻刷"。

---

## 安全清单

- [ ] 面板密码改掉（默认 `admin` 会在 `/panel/status` 里被标成 `is_default_password: true`）
- [ ] `listen` 保持 `127.0.0.1`，或用前置反代 + TLS；**不要把裸端口暴露到公网**
- [ ] 至少建一个客户端密钥（否则是开放模式）
- [ ] 数据目录权限：Linux `chmod 700`，Windows 去掉 Everyone/Users 的读权限
- [ ] 定期备份 `data/accounts.json`（丢了等于所有账号要重新授权）
- [ ] 日志保留：`usage.retention_days`（默认 7 天，台账按天分文件自动清理）
- [ ] 怀疑密钥泄露时：面板删掉该密钥即可立即失效（无需重启）

---

## 升级

```bash
git pull
python -m wb2api doctor        # 先检查配置是否兼容
python -m unittest discover -s tests -t .   # 132 个单测
# 重启服务
```

配置键兼容策略：域键 `domestic/international` 仍被接受（等价于 `cn/global`），所以老配置文件不需要手改。`config_version` 目前是 1，未来有破坏性变更会 +1。

---

## 真机自检清单

单测覆盖不到真实上游，部署完请依次确认（面板里点、或 curl）：

1. `GET /healthz` → `{"ok":true}`
2. 面板登录 → 能看到面板
3. 账号「测试」按钮 → `ok: true`（这步会真打一次上游，失败时它回的错误就是真实原因）
4. 账号「查额度」→ 有数字（`credits: null` 说明接口拒了，通常是令牌或域不对）
5. 非流式 chat → 有内容、`usage.total_tokens > 0`
6. 流式 chat → 首字延迟正常（< 3s），结尾有 `[DONE]`
7. 面板「概览」→ 请求数 +1、TTFT 有值、账号列有记录
8. 拔掉一个号（禁用）→ 再请求，自动换号成功
9. 故意用错密钥 → 401 `invalid_api_key`；把所有号禁用 → 503 `no_account`

第 3、4 步失败时把面板「日志」页最后几十行贴出来，里面按错误码分了类（`pool` 相关日志会写明是哪个时间窗被触发）。
