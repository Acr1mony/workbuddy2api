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


def _config(args: argparse.Namespace) -> Config:
    """决定读哪个配置文件，以及"以后写回哪里"。

    以前默认路径就是工作目录下的 ``config.json``，于是"跑一次测试"或"在面板里
    改一次设置"都会在当前目录凭空生成/覆盖一个配置文件——用 ``python -m wb2api``
    从仓库根启动时，这等于悄悄改掉使用者的配置。

    现在的规则：显式给了 ``-c``/``WB_CONFIG`` 就用它；工作目录里本来就有
    ``config.json`` 就用它；否则配置落在数据目录（``<data_dir>/config.json``），
    既不会污染工作目录，面板里的改动也能在重启后留下来。
    """
    explicit = (getattr(args, "config", "") or "").strip()
    if explicit:
        return Config.load(explicit)
    if os.path.exists("config.json"):
        return Config.load("config.json")
    base = Config.load(None)
    base.apply_env(dict(os.environ))
    data_dir = os.path.abspath(str(base.get("data_dir") or "./data"))
    stored = os.path.join(data_dir, "config.json")
    if os.path.exists(stored):
        return Config.load(stored)
    base.path = stored
    return base


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="wb2api", description="WorkBuddy / CodeBuddy → OpenAI 兼容网关")
    parser.add_argument(
        "-c", "--config", default=os.environ.get("WB_CONFIG", ""),
        help="配置文件路径（默认：./config.json，没有就落到 <data_dir>/config.json）",
    )
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
        choices=("list", "test", "enable", "disable", "remove", "balance", "export", "import"),
        help="list=列表 test=体检 enable/disable=启停 remove=删除 balance=查余额 export/import=备份与恢复",
    )
    accounts.add_argument("target", nargs="?", default="all", help="uid / uid 前缀 / all；import 时是备份文件路径")
    accounts.add_argument("-o", "--output", default="", help="export 的输出文件（不给就打标准输出）")
    accounts.add_argument("--realm", default="", help="只处理这个域（cn/global）")
    accounts.add_argument("--model", default="", help="test 用的模型，默认取目录第一个")
    accounts.add_argument("--json", action="store_true", help="输出 JSON（给脚本用）")

    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "version":
        print(f"workbuddy2api {VERSION}")
        return 0

    cfg = _config(args)
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
    # unavailable_reason 返回的是 dict（reason/kind/remaining），不是字符串——
    # 直接拿它做布尔判断会把每个可用账号都显示成"冷却"。
    info = unavailable_reason(account, "")
    reason = str(info.get("reason") or "")
    return f"冷却({reason})" if reason else "可用"


def _export_accounts(service: Any, output: str = "") -> int:
    """把账号（含 refresh token）导出成 JSON——备份用，不是"导出成可分享的东西"。"""
    accounts = service.store.list_accounts("")
    if not accounts:
        print("没有账号可导出。")
        return 1
    payload = {
        "version": 1,
        "exported_at": time.time(),
        "warning": "本文件含 refresh token，等同于账号密码。别提交到仓库，别贴到聊天软件里。",
        "accounts": [account.to_dict() for account in accounts],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if output:
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"已导出 {len(accounts)} 个账号 → {output}（含 refresh token，注意保管）")
    else:
        print(text)
    return 0


def _import_accounts(service: Any, path: str) -> int:
    """从备份文件恢复账号。同 uid 覆盖，其余保留——导入不做删除。"""
    if not path or path in ("all", "*") or not os.path.exists(path):
        print(f"找不到备份文件：{path or '(未指定)'}")
        return 1
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    items = data.get("accounts") if isinstance(data, dict) else data
    if not isinstance(items, list):
        print("备份格式不对：需要 {\"accounts\": [...]} 或直接给数组。")
        return 1
    from .store import Account

    added = updated = skipped = 0
    for raw in items[:500]:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        try:
            account = Account.from_dict(raw)
        except Exception:
            skipped += 1
            continue
        if not (account.uid or account.access_token or account.refresh_token):
            skipped += 1
            continue
        account.ensure_ids()
        existed = bool(account.uid) and service.store.get_account(account.uid) is not None
        service.store.upsert_account(account)
        if existed:
            updated += 1
        else:
            added += 1
    print(f"导入完成：新增 {added}，更新 {updated}，跳过 {skipped}")
    return 0


def _accounts(cfg: Config, args: argparse.Namespace) -> int:
    from .probe import probe_account

    service = _service(cfg)

    if args.action == "export":
        return _export_accounts(service, getattr(args, "output", ""))
    if args.action == "import":
        return _import_accounts(service, args.target)

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
