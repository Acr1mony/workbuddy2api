"""命令行入口：``python -m wb2api`` / ``wb2api``。

只有三个子命令，因为运维只需要三件事：跑起来、检查配置、看版本。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from typing import List, Optional

from . import VERSION
from .app import banner, build
from .config import Config
from .log import get_logger, setup as setup_logging


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="wb2api", description="WorkBuddy / CodeBuddy → OpenAI 兼容网关")
    parser.add_argument("-c", "--config", default=os.environ.get("WB_CONFIG", "config.json"), help="配置文件路径")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动网关（默认）")
    serve.add_argument("--host", default="", help="覆盖监听地址")
    serve.add_argument("--port", type=int, default=0, help="覆盖监听端口")
    serve.add_argument("--no-panel", action="store_true", help="本次不启用面板")
    serve.add_argument("--no-scheduler", action="store_true", help="本次不启动后台调度")

    sub.add_parser("doctor", help="检查配置与数据目录")
    sub.add_parser("version", help="打印版本")

    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "version":
        print(f"workbuddy2api {VERSION}")
        return 0

    cfg = Config.load(args.config)
    cfg.apply_env(dict(os.environ))
    setup_logging(
        str(cfg.get("logging.level") or cfg.get("log_level") or "INFO"),
        str(cfg.get("logging.dir") or "./logs"),
    )

    if command == "doctor":
        return _doctor(cfg)
    return _serve(cfg, args)


def _doctor(cfg: Config) -> int:
    errors, warnings = cfg.validate_split()
    print(f"config : {cfg.path}")
    print(f"data   : {cfg.data_dir}")
    print(f"listen : {cfg.listen_address()}")
    print(f"panel  : {cfg.get('panel.enabled')}")
    for warning in warnings:
        print(f"提示：{warning}")
    if not errors:
        print("检查通过。")
        return 0
    print("问题：")
    for problem in errors:
        print(f"  - {problem}")
    return 1


def _serve(cfg: Config, args: argparse.Namespace) -> int:
    if getattr(args, "no_panel", False):
        cfg.set("panel.enabled", False)
    host, port = cfg.listen_address()
    if getattr(args, "host", ""):
        host = args.host
    if getattr(args, "port", 0):
        port = args.port

    from .httpserver import serve as serve_http

    app, service = build(cfg)
    server = serve_http(app, host, port)
    print(banner(cfg))

    scheduler = getattr(app, "scheduler", None)
    if scheduler is not None and not getattr(args, "no_scheduler", False):
        scheduler.start()

    stopping = threading.Event()

    def shutdown(signum: int = 0, frame: object = None) -> None:
        if stopping.is_set():
            return
        stopping.set()
        get_logger("cli").info("收到退出信号，正在关闭…")
        if scheduler is not None:
            scheduler.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig):
            try:
                signal.signal(getattr(signal, sig), shutdown)
            except (ValueError, OSError):  # 非主线程/受限环境
                pass

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        shutdown()
    finally:
        try:
            server.server_close()
        finally:
            service.close()
        get_logger("cli").info("已退出")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
