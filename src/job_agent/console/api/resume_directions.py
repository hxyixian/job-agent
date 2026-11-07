"""简历与方向页路由（§10.2「简历与方向」行；FR2.1/2.2/2.3/2.5）。

对接 S4e 装配层 ``resume/service.py`` 的 API 级接口（db 第一参数，直接调用）：
``upload_and_parse`` / ``get_current_version`` / ``apply_manual_edit`` /
``directions_generate`` / ``directions_confirm`` / ``directions_edit`` /
``get_directions``。本层不重复实现任何解析/版本化细节，只做：

- 请求模型校验（Pydantic 422）与用户可见错误转 HTTP 400
  （``ResumeError`` 族 message 即控制台文案，S3c/S4e 契约）；
- 换简历联动提示文案（``regenerate_hint`` [FR2.3]，提示不强制失效）。

路由清单（§10.2 + 页面加载所需 GET）：

- ``POST /api/resume/upload``：multipart 上传（仅 .pdf）→ S4e 上传链；
- ``GET  /api/resume/structured``：当前结构化简历（无版本时各字段为 null）；
- ``PUT  /api/resume/structured``：行内编辑保存 → 新版本 [FR2.5]；
- ``GET  /api/directions``：全部方向行（候选 + 已确认）；
- ``POST /api/directions/generate``：AI 候选生成（替换未确认候选）；
- ``PUT  /api/directions``：确认（source=confirm）/手动编辑（source=edit）
  两路接线，同一"整体替换已确认集"语义。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import Field

from job_agent.models import JobDirection, ResumeSnapshot
from job_agent.resume import parser, service
from job_agent.resume.service import ResumeVersion

from ._helpers import ContentContext, SectionModel

__all__ = ["REGENERATE_HINT_TEXT", "attach_resume_directions_routes"]

# 换简历联动提示文案（FR2.3 s4 决策：提示不强制失效，仅建议重新生成）
REGENERATE_HINT_TEXT = (
    "已更换简历：检测到存在基于旧简历的产物（已确认方向或已定稿招呼语），"
    "建议重新生成职位方向与招呼语候选；旧内容在你重新确认/定稿前持续有效。"
)


class StructuredPayload(SectionModel):
    """PUT /resume/structured 请求体：四字段各为字符串数组 [FR2.5]。"""

    education: list[str] = Field(default_factory=list)
    experience: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)


class DirectionItemPayload(SectionModel):
    """单组方向：关键词 ≥1（空组无法作为搜索条件）+ 依据说明。"""

    keywords: list[str] = Field(min_length=1)
    rationale: str = ""


class DirectionsPayload(SectionModel):
    """PUT /directions 请求体：``source`` 区分"确认 AI 候选"与"直接手动编辑"
    两路接线（S4e 提供同名异径入口，语义一致），终选列表整体替换已确认集。"""

    source: Literal["confirm", "edit"] = "confirm"
    directions: list[DirectionItemPayload] = Field(default_factory=list)


def _snapshot_payload(snapshot: ResumeSnapshot) -> dict[str, Any]:
    return {
        "education": list(snapshot.education),
        "experience": list(snapshot.experience),
        "skills": list(snapshot.skills),
        "projects": list(snapshot.projects),
    }


def _version_payload(version: ResumeVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "created_at": version.created_at,
        "source_pdf_hash": version.source_pdf_hash,
    }


def _direction_payload(direction: JobDirection) -> dict[str, Any]:
    return {
        "keywords": list(direction.keywords),
        "rationale": direction.rationale,
        "is_confirmed": direction.is_confirmed,
    }


def attach_resume_directions_routes(router: APIRouter, ctx: ContentContext) -> None:
    """注册简历与方向路由（由 ``content.create_content_router`` 调用）。

    依赖注入：装配层共享的线程安全 ``Database``（S4e 契约「db 第一参数，
    直接调用」）；六项加固与异常统一 JSON 由 ``console.app.create_app``
    全局生效，本模块无需重复装配。
    """
    db = ctx.db

    @router.post("/resume/upload")
    async def upload_resume(file: UploadFile) -> dict[str, Any]:
        """上传 .pdf → S4e 上传链（解析→结构化→版本入库）；失败不落库。

        非 .pdf（按原始文件名判定）直接 400；``ResumeError`` 族
        （扫描件/AI 结构化失败等）message 即用户可见文案，转 400。
        """
        name = file.filename or ""
        if not name.lower().endswith(".pdf"):
            raise HTTPException(
                status_code=400,
                detail="仅支持 PDF 格式简历（.pdf）：请导出文字版 PDF 后重新上传。",
            )
        data = await file.read()
        # 临时文件承载字节（upload_and_parse 按路径读取；后缀固定 .pdf）
        fd, tmp_name = tempfile.mkstemp(suffix=".pdf", prefix="job_agent_upload_")
        os.close(fd)
        try:
            Path(tmp_name).write_bytes(data)
            try:
                result = service.upload_and_parse(db, tmp_name)
            except parser.ResumeError as exc:  # 含 ScannedPdf/AIStructure/AIDirection
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            os.unlink(tmp_name)
        body: dict[str, Any] = {
            "version": _version_payload(result.version),
            "structured": _snapshot_payload(result.version.snapshot),
            "replaced_previous": result.replaced_previous,
            "regenerate_hint": result.regenerate_hint,
        }
        if result.regenerate_hint:
            body["regenerate_hint_text"] = REGENERATE_HINT_TEXT
        return body

    @router.get("/resume/structured")
    def get_structured() -> dict[str, Any]:
        """当前结构化简历（AI 上下文事实来源 [FR2.4]）；从未上传时各字段 null。"""
        version = service.get_current_version(db)
        if version is None:
            return {"version": None, "structured": None}
        return {
            "version": _version_payload(version),
            "structured": _snapshot_payload(version.snapshot),
        }

    @router.put("/resume/structured")
    def put_structured(payload: StructuredPayload) -> dict[str, Any]:
        """行内编辑保存 → 新版本 [FR2.5]；联系方式条目由服务端整条剔除 [B12]，
        剔除结果即回显结果（用户可见）。"""
        snapshot = ResumeSnapshot(
            education=payload.education,
            experience=payload.experience,
            skills=payload.skills,
            projects=payload.projects,
        )
        try:
            version = service.apply_manual_edit(db, snapshot)
        except (ValueError, parser.ResumeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "version": _version_payload(version),
            "structured": _snapshot_payload(version.snapshot),
        }

    @router.get("/directions")
    def get_directions() -> dict[str, Any]:
        """全部方向行（候选 is_confirmed=0 + 已确认=1，插入序）。"""
        return {"directions": [_direction_payload(d) for d in service.get_directions(db)]}

    @router.post("/directions/generate")
    def post_directions_generate() -> dict[str, Any]:
        """AI 生成方向候选 [FR2.2]：未上传简历 / AI 失败 → 400（文案含出路指引）。"""
        try:
            candidates = service.directions_generate(db)
        except parser.ResumeError as exc:  # 含 AIDirectionError（子类）
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"directions": [_direction_payload(d) for d in candidates]}

    @router.put("/directions")
    def put_directions(payload: DirectionsPayload) -> dict[str, Any]:
        """确认（source=confirm）/ 手动编辑（source=edit）[FR2.2/FR2.3]：
        终选列表整体替换已确认集；语义校验失败（空关键词组等）→ 422。"""
        directions = [
            JobDirection(keywords=item.keywords, rationale=item.rationale, is_confirmed=True)
            for item in payload.directions
        ]
        replace = (
            service.directions_confirm if payload.source == "confirm" else service.directions_edit
        )
        try:
            confirmed = replace(db, directions)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"directions": [_direction_payload(d) for d in confirmed]}
