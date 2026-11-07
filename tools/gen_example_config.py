"""把 ``wb2api.config.DEFAULTS`` 落成 ``config.example.json``。

示例配置必须是**从代码生成**的，不能手写：手写的示例迟早和默认值分叉，
而"照示例配了一遍却发现行为不对"是这类项目最常见的求助来源。

用法（在仓库根目录）::

    python tools/gen_example_config.py
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wb2api.config import DEFAULTS  # noqa: E402


def main() -> int:
    path = os.path.join(ROOT, "config.example.json")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(DEFAULTS, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print("wrote %s (%d bytes)" % (path, os.path.getsize(path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
