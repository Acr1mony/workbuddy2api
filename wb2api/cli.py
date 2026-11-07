"""命令行入口：``python -m wb2api`` / ``wb2api``。

运维只需要几件事：跑起来、检查配置、看版本、管账号。账号相关的子命令不是
"额外的界面"，而是让没有面板（或面板被关掉）的机器也能完成任务。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from typing import Any, List, Optional

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

    accounts = sub.add_parser("accounts", help="账号管理（不需要面板）")
    accounts.add_argument(
        "action",
        choices=("list", "test", "enable", "disable", "remove", "balance"),
        help="list=列表 test=体检 enable/disable=启停 remove=删除 balance=查余额",
    )
    accounts.add_argument("target", nargs="?", default="all", help="uid / uid 前缀 / all")
    accounts.add_argument("--realm", default="", help="只处理这个域（cn/global）")
    accounts.add_argument("--model", default="", help="test 用的模型，默认取目录第一个")
    accounts.add_argument("--json", action="store_true", help="输出 JSON（给脚本用）")

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
    if command == "accounts":
        return _accounts(cfg, args)
    return _serve(cfg, args)



def _service(cfg: Config) -> Any:
    """进程内建一个 service：不开 socket，也不启调度。"""
    from .service import GatewayService
    from .store import Store

    store = Store(cfg.data_dir)
    return GatewayService(cfg, store)


def _resolve(service: Any, target: str, realm: str = "") -> List[Any]:
    accounts = service.store.list_accounts(realm)
    if target in ("", "all", "*"):
        return accounts
    exact = service.store.get_account(target)
    if exact is not None:
        return [exact]
    # 允许用 uid 前缀（终端里复制 36 位 uid 太费劲）
    return [a for a in accounts if (a.uid or "").startswith(target) or (a.id or "").startswith(target)]


def _state_of(service: Any, account: Any) -> str:
    from .pool import unavailable_reason

    if account.disabled:
        return "已禁用"
    reason = unavailable_reason(account, "")
    return f"冷却({reason})" if reason else "可用"


def _accounts(cfg: Config, args: argparse.Namespace) -> int:
    from .probe import probe_account

    service = _service(cfg)
    accounts = _resolve(service, args.target, args.realm)
    if not accounts:
        print("没有匹配的账号。" if args.target in ("all", "", "*") else f"找不到账号：{args.target}")
        return 1

    if args.action == "list":
        rows = [
            {
                "uid": a.uid,
                "realm": a.realm,
                "nickname": a.nickname,
                "credit": a.credit,
                "state": _state_of(service, a),
                "last_error": (a.last_error or "")[:60],
            }
            for a in accounts
        ]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        print(f"{'UID':<38}{'域':<8}{'昵称':<14}{'余额':>10}  状态")
        for row in rows:
            credit = "未知" if row["credit"] is None else f"{row['credit']:.2f}"
            print(f"{row['uid']:<38}{row['realm']:<8}{(row['nickname'] or '-')[:12]:<14}{credit:>10}  {row['state']}")
        return 0

    if args.action == "test":
        failures = 0
        for account in accounts:
            ok, detail = probe_account(service, account, args.model)
            if ok:
                print(f"[ok]   {account.label} → {detail.get('model')}  {detail.get('sample', '')[:60]}")
            else:
                failures += 1
                print(f"[fail] {account.label} → {detail.get('error', '')}")
        return 1 if failures else 0

    if args.action in ("enable", "disable"):
        for account in accounts:
            account.disabled = args.action == "disable"
        service.store.save_accounts()
        print(f"{'已启用' if args.action == 'enable' else '已禁用'} {len(accounts)} 个账号")
        return 0

    if args.action == "remove":
        if args.target in ("all", "", "*"):
            print("拒绝删除全部账号：请指定 uid。")
            return 1
        removed = 0
        for account in accounts:
            if service.store.remove_account(account.key):
                removed += 1
        print(f"已删除 {removed} 个账号")
        return 0

    if args.action == "balance":
        from .billing import Billing

        billing = Billing(service.client, cfg)
        for account in accounts:
            credits = billing.credits(account, account.realm)
            if credits is None:
                print(f"{account.label} → 读不到余额（可能是不支持该接口的账号类型）")
            else:
                service.store.update_credit(account, credits)
                print(f"{account.label} → {credits:.2f}")
        return 0
    return 1


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
