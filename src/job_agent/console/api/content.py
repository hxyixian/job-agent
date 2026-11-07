"""S5b「配置与内容类 5 页」聚合装配（§10.2 对应 5 行的 /api 路由）。

装配协议（与 S5c ``ops.create_ops_router(OpsContext)`` 同构；S5a 骨架 /
S5d ``__main__`` 注入依赖）::

    from job_agent.console.api._helpers import ContentContext
    from job_agent.console.api.content import create_content_router

    app.include_router(create_content_router(ContentContext(
        db=db,                                    # 引擎共享 Database（S4e：db 第一参数）
        config_path=default_config_path(),        # 配置文件（GET/PUT /api/config*）
        command_queue=orch.command_queue(),       # 配置保存后 reload_config 提示
    )))  # 路由自带 /api 前缀，include 时勿重复加

``console.app.create_app(db=...)`` 已按上述协议自动装配（S5a 合并点），
S5d 无需重复挂载；仅独立装配（如脱离 create_app 的定制 app）时才需要
显式 include。子模块明细：

- ``resume_directions``：简历与方向（上传/行内编辑/方向生成确认编辑）；
- ``greetings``：招呼语工作台（双模板候选→定稿 + F1 同步确认勾选）；
- ``config_routes``：任务配置 + 分节（notify/ai）读写（全量重写 [B14]）；
- ``diagnostics``：通知测试发送（假端点默认）+ AI 连通性测试。
"""

from __future__ import annotations

from fastapi import APIRouter

from ._helpers import ContentContext
from .config_routes import attach_config_routes
from .diagnostics import attach_diagnostics_routes
from .greetings import attach_greetings_routes
from .resume_directions import attach_resume_directions_routes

__all__ = ["create_content_router"]


def create_content_router(ctx: ContentContext) -> APIRouter:
    """构造 S5b 五页聚合 router（前缀 ``/api``；include 时无需再加前缀）。"""
    router = APIRouter(prefix="/api", tags=["s5b-content"])
    attach_resume_directions_routes(router, ctx)
    attach_greetings_routes(router, ctx)
    attach_config_routes(router, ctx)
    attach_diagnostics_routes(router, ctx)
    return router
