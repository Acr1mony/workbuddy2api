"""面板静态页。

面板是三个页面（账号 / 概览 / 设置）的纯前端单页，没有构建步骤——一份 HTML、
一份 CSS、一份 JS，直接由网关自己发。理由和零依赖一样：反代跑在 NAS 上，不该
为了改一个按钮再装一遍 node。

静态文件只从本目录的 ``static/`` 里取，文件名不允许包含路径分隔符，所以不存在
目录穿越；``/panel`` 前缀在面板关闭时根本没被注册。
"""

from __future__ import annotations

import os
from typing import Any, Dict

from ..httpserver import Response, Router, error_response

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


def register(router: Router, panel: Any) -> None:
    router.add("GET", r"/", lambda req: _redirect("/panel/"))
    router.add("GET", r"/panel", lambda req: _redirect("/panel/"))
    router.add("GET", r"/panel/", lambda req: _file("index.html"))
    router.add("GET", r"/panel/(?P<name>[^/]+)", lambda req, name: _file(name))


def _redirect(location: str) -> Response:
    return Response(status=302, body=b"", content_type="text/plain; charset=utf-8", headers={"Location": location})


def _file(name: str) -> Response:
    if "/" in name or "\\" in name or name.startswith("."):
        return error_response(404, "not found", "not_found")
    path = os.path.join(STATIC_DIR, name)
    if not os.path.isfile(path):
        return error_response(404, f"panel asset {name} not found", "not_found")
    try:
        with open(path, "rb") as handle:
            body = handle.read()
    except OSError:
        return error_response(500, "cannot read panel asset", "io_error")
    extension = os.path.splitext(name)[1].lower()
    if extension == ".html":
        # 页面本身绝不缓存，且把版本号填进静态资源 URL —— 只缓存一份 app.js
        # 的浏览器会在升级后继续用旧脚本，登录按钮看着没反应（它绑的是旧 DOM）。
        body = body.replace(b"{{VERSION}}", __version().encode("utf-8"))
        body = body.replace(b"{{REPO}}", __repo().encode("utf-8"))
        headers: Dict[str, str] = {"Cache-Control": "no-store"}
    else:
        headers = {"Cache-Control": "public, max-age=300"}
    return Response(status=200, body=body, content_type=CONTENT_TYPES.get(extension, "application/octet-stream"), headers=headers)


def asset_path(name: str) -> str:
    return os.path.join(STATIC_DIR, name)


def __version() -> str:
    """面板页面里填的版本号，用来给静态资源做缓存指纹。"""
    from .. import VERSION

    return VERSION


def __repo() -> str:
    """面板里那个"跳到仓库"的地址：只在 ``wb2api/__init__.py`` 里写一次。"""
    from .. import PROJECT_URL

    return PROJECT_URL
