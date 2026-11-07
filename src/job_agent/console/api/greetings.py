"""招呼语工作台路由（§10.2「招呼语工作台」行；FR5.1/5.3，F1）。

对接 ``ai/greeting.py`` 的写库路径（save_greeting_template /
current_greeting / confirm_platform_synced / is_platform_synced）与
``resume/service.py`` 的当前简历/已确认方向读取（生成输入），路由层
只做请求校验与用户可见错误转 HTTP 状态码：

- ``GET  /api/greetings/current``：双模板当前态（内容/版本/F1 同步态）；
- ``POST /api/greetings/generate?kind=active|passive``：恒 3 条候选 [FR5.1]；
- ``PUT  /api/greetings/current``：定稿保存（新版本行，F1 同步确认随之失效）；
- ``POST /api/greetings/platform-synced``：**"已同步到平台设置"确认勾选
  [F1]**——与定稿时的内容哈希绑定（``confirm_platform_synced`` 写入当前
  哈希）；此后任何重新定稿 → 新行哈希为 NULL → 勾选自动失效（FR5.3），
  前端按 ``platform_synced`` 布尔呈现。

R12 背景：主动招呼语由平台发送（用户粘贴到平台设置），被动招呼语由
软件直接发送；同步确认勾选只对主动模板呈现（页面层约束，API 对两种
kind 通用）。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException

from job_agent.ai import greeting
from job_agent.ai.errors import AIError
from job_agent.db import Database
from job_agent.resume import service as resume_service

from ._helpers import ContentContext, SectionModel

__all__ = ["attach_greetings_routes"]


class GreetingSavePayload(SectionModel):
    """PUT /greetings/current 请求体：kind + 定稿内容（非空由服务层校验）。"""

    kind: Literal["active", "passive"]
    content: str


class PlatformSyncPayload(SectionModel):
    """F1 同步确认勾选请求体（与当前定稿内容哈希绑定）。"""

    kind: Literal["active", "passive"]


def _kind_payload(db: Database, kind: Literal["active", "passive"]) -> dict[str, Any] | None:
    """单 kind 当前模板 → 响应体（含 F1 同步态判定）；未定稿返回 None。"""
    row = greeting.current_greeting(db, kind)
    if row is None:
        return None
    synced_hash = row["platform_synced_hash"]
    return {
        "kind": kind,
        "content": row["content"],
        "version": row["version"],
        "created_at": row["created_at"],
        "platform_synced": greeting.is_platform_synced(row["content"], synced_hash),
        "platform_synced_at": row["platform_synced_at"],
    }


def attach_greetings_routes(router: APIRouter, ctx: ContentContext) -> None:
    """注册招呼语工作台路由（由 ``content.create_content_router`` 调用）。"""
    db = ctx.db

    @router.get("/greetings/current")
    def get_current() -> dict[str, Any]:
        """双模板当前态（active/passive 各一，未定稿为 null → 页面 FR5.3 提示）。"""
        return {"active": _kind_payload(db, "active"), "passive": _kind_payload(db, "passive")}

    @router.post("/greetings/generate")
    def post_generate(kind: Literal["active", "passive"]) -> dict[str, Any]:
        """生成 3 条候选 [FR5.1]：简历为生成输入、已确认方向为求职目标输入；
        未上传简历 / AI 层失败（解析/预算/超时）→ 400（文案不静默降级）。"""
        snapshot = resume_service.get_current_snapshot(db)
        if snapshot is None:
            raise HTTPException(
                status_code=400,
                detail="尚未上传简历：请先在「简历与方向」页上传并解析简历，再生成招呼语候选。",
            )
        directions = resume_service.confirmed_directions(db)
        try:
            candidates = greeting.generate_greeting_candidates(kind, snapshot, directions)
        except AIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"kind": kind, "candidates": candidates}

    @router.put("/greetings/current")
    def put_current(payload: GreetingSavePayload) -> dict[str, Any]:
        """定稿保存（清同 kind 旧 current → 新版本行；F1 同步列为 NULL [FR5.3]）。
        空白内容 → 422。"""
        try:
            greeting.save_greeting_template(db, payload.kind, payload.content)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        fresh = _kind_payload(db, payload.kind)
        assert fresh is not None, "定稿后必有 current 行"
        return fresh

    @router.post("/greetings/platform-synced")
    def post_platform_synced(payload: PlatformSyncPayload) -> dict[str, Any]:
        """F1 确认勾选：写入当前内容哈希 + 时间；此后内容任何修改（重新定稿）
        → 哈希不匹配 → 勾选自动失效。未定稿 → 400。"""
        row = greeting.current_greeting(db, payload.kind)
        if row is None:
            raise HTTPException(
                status_code=400,
                detail=f"{'主动' if payload.kind == 'active' else '被动'}招呼语尚未定稿，"
                "请先生成候选并定稿，再确认「已同步到平台设置」。",
            )
        greeting.confirm_platform_synced(db, int(row["id"]))
        fresh = _kind_payload(db, payload.kind)
        assert fresh is not None, "确认同步后必有 current 行"
        return fresh
