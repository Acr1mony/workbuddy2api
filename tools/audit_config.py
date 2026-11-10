#!/usr/bin/env python3
"""找出 ``config.DEFAULTS`` 里"写了但没人读"的配置键。

为什么需要这个工具：这个仓库出过好几次"改了配置等于没改"——键名对不上、
拼写不同、或者干脆忘了接。配置项是最容易腐烂的地方，因为它不报错：
用户把 ``stream_heartbeat_seconds`` 改成 5，什么都不会发生，也不会有人知道。

判定方式（静态，不启动服务）：

1. 收集 ``DEFAULTS`` 的所有叶子键；
2. 在 ``wb2api/`` 的其它模块里找"用过"的痕迹：
   * 带点的字符串字面量：``cfg.get("upstream.timeout_seconds")``
   * ``_pool_cfg("pool", "max_in_flight")`` 这类分段写法
   * 前缀写法：``realm_value(cfg, "pool.strategy", realm)`` 会读出
     ``pool.strategy.cn``，所以字面量是叶子键的前缀也算用过
3. 剩下的就是死键。

用法::

    python tools/audit_config.py          # 有死键退出码 1（CI 用）
    python tools/audit_config.py --list    # 只打印，退出码恒 0
"""

from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wb2api.config import DEFAULTS  # noqa: E402

SKIP_FILES = {"config.py"}  # DEFAULTS 自己不算"被读取"


def leaves(node, prefix=""):
    if isinstance(node, dict) and node:
        for key, value in node.items():
            yield from leaves(value, f"{prefix}.{key}" if prefix else str(key))
    else:
        yield prefix


DOTTED = re.compile(r'"([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)"')
# 分段读取：`_pool_cfg("pool", "max_in_flight")`。参数列表要整段抓下来——
# 只抓到第一个逗号前的部分会让 used 里只剩段名 "pool"，于是一整段配置全被
# 当成"已使用"，死键就被掩盖了。
POOL_CFG = re.compile(r'_pool_cfg\(\s*([^)\n]+)\)', re.IGNORECASE)
QUOTED = re.compile(r'"([a-z0-9_]+)"')
# 分段读取：`_duration(("pool", "quota_probe_grace"), 60.0)` 这种元组写法
TUPLE_PATH = re.compile(r'\(\s*"([a-z0-9_]+)"\s*,\s*"([a-z0-9_]+)"(?:\s*,\s*"([a-z0-9_]+)")?\s*\)')
# 整段读取：`schedule.get("report_hour")` 只出现叶子名，段名在另一个变量里
SECTION_GET = re.compile(r'\.get\(\s*"([a-z0-9_]+)"')


def used_keys(root=ROOT):
    used = set()
    for base, _dirs, files in os.walk(os.path.join(root, "wb2api")):
        for name in files:
            if not name.endswith(".py") or name in SKIP_FILES:
                continue
            with open(os.path.join(base, name), encoding="utf-8") as handle:
                text = handle.read()
            used.update(DOTTED.findall(text))
            for group in POOL_CFG.findall(text):
                parts = QUOTED.findall(group)
                if parts:
                    used.add(".".join(parts))
            for first, second, third in TUPLE_PATH.findall(text):
                used.add(".".join(part for part in (first, second, third) if part))
            # 整段读取只能看到叶子名。宁可漏报也别误报——这个工具的职责是抓
            # "拼错了/忘了接"，不是证明每个键都真的被读到。
            used.update(SECTION_GET.findall(text))
    return used


def dead_keys(root=ROOT):
    used = used_keys(root)
    dead = []
    for key in leaves(DEFAULTS):
        if not key:
            continue
        hit = key in used
        if not hit:
            for candidate in used:
                if key.startswith(candidate + ".") or candidate.startswith(key + "."):
                    hit = True
                    break
        if not hit:
            dead.append(key)
    return sorted(dead)


def main(argv=None):
    parser = argparse.ArgumentParser(description="列出没人读取的配置键")
    parser.add_argument("--list", action="store_true", help="只打印，不返回失败")
    parser.add_argument("--root", default=ROOT, help="仓库根目录（默认当前仓库）")
    args = parser.parse_args(argv)

    total = len(list(leaves(DEFAULTS)))
    dead = dead_keys(args.root)
    if not dead:
        print(f"配置键检查通过：{total} 个键都有读取方")
        return 0
    print(f"发现 {len(dead)} 个没人读的配置键（共 {total} 个）：")
    for key in dead:
        print(f"  - {key}")
    print("\n处理办法：接上它（在代码里真正读一次），或者从 DEFAULTS 里删掉。")
    return 0 if args.list else 1


if __name__ == "__main__":
    sys.exit(main())
