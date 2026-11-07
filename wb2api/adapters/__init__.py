"""协议适配器。每个子模块负责一种对外协议 ↔ 上游协议的转换。"""

from . import chat  # noqa: F401

__all__ = ["chat"]
