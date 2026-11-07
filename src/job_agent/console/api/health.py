"""GET /api/health：控制台存活探针（只读、无副作用，不触 DB/引擎）。"""

from __future__ import annotations

from fastapi import APIRouter

from job_agent import __version__

router = APIRouter(prefix="/api", tags=["console"])


@router.get("/health")
def health() -> dict[str, str]:
    """存活探针（§10.1 第 4 项：GET 无副作用——仅返回进程内常量）。"""
    return {"status": "ok", "version": __version__}
