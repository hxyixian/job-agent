"""/api 路由注册（S5a：health/status 只读端点；业务端点归 S5b/S5c）。

S5b 追加方式（已按此合并）：``content.create_content_router(ContentContext)``
聚合「配置与内容类 5 页」路由——``app.create_app(db=...)`` 在提供 db 时自动
构造并经 ``register_api(app, content=...)`` 挂载；S5c 的 ``ops.create_ops_router``
（需 OpsContext 注入）由 S5d 装配时以同方式 include。六项加固与异常统一
JSON 由 ``app.create_app`` 全局生效，路由模块无需重复装配。
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from job_agent.console.api import health, status

__all__ = ["register_api"]


def register_api(app: FastAPI, content: APIRouter | None = None) -> None:
    """注册全部 ``/api`` 路由（S5b/S5c 在此追加各自页面组）。"""
    app.include_router(health.router)
    app.include_router(status.router)
    if content is not None:
        app.include_router(content)
