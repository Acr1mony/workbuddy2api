"""装配：把配置、存储、服务、路由、调度器拼成一个可运行的应用。

``build()`` 是唯一知道"谁依赖谁"的地方，``__main__`` 只负责参数解析和信号处理。
面板关闭时不注册面板路由——路径根本不存在，比"存在但 401"少一个攻击面。
"""

from __future__ import annotations

from typing import Any, Tuple

from .httpserver import Application, Router
from .log import get_logger
from .routes_admin import Panel
from .routes_admin import register as register_admin
from .routes_openai import register as register_openai
from .scheduler import Scheduler
from .service import GatewayService
from .store import Store
from .web import register as register_web

log = get_logger("app")


def build(cfg: Any, *, store: Store = None) -> Tuple[Application, GatewayService]:
    store = store or Store(str(cfg.get("data_dir") or "./data"))
    service = GatewayService(cfg, store)
    router = Router()
    register_openai(router, service)

    panel = None
    if cfg.get("panel.enabled"):
        panel = Panel(service, cfg)
        register_admin(router, panel)
        register_web(router, panel)
        log.info("面板已启用：/panel/")
    else:
        log.info("面板未启用（panel.enabled=false）")

    scheduler = Scheduler(service, cfg)
    service.scheduler = scheduler

    app = Application(router, service=service)
    app.panel = panel
    app.scheduler = scheduler
    app.store = store
    return app, service


def banner(cfg: Any) -> str:
    from . import VERSION

    host, port = "127.0.0.1", 8788
    if hasattr(cfg, "listen_address"):
        host, port = cfg.listen_address()
    else:
        listen = str(cfg.get("listen") or "")
        if ":" in listen:
            host, _, port_text = listen.rpartition(":")
            port = int(port_text or 8788)
    lines = [
        f"workbuddy2api {VERSION}",
        f"  listen : http://{host}:{port}",
        f"  openai : base_url=http://{host}:{port}/v1",
        f"  panel  : {'http://%s:%s/panel/' % (host, port) if cfg.get('panel.enabled') else 'disabled'}",
        f"  keys   : {'configured' if cfg.get('api_key') else 'none (local open mode)'}",
    ]
    if cfg.get("panel.enabled") and str(cfg.get("panel.password") or "admin") in ("admin", ""):
        lines.append("  ! panel 仍在用默认密码 admin，请登录后立刻修改")
    return "\n".join(lines)
