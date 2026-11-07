"""GET /api/status：引擎状态只读快照（总览 dashboard 的主数据源，S5c 扩展）。

数据源是 DB（``run_state.engine_state`` / ``run_state.last_command``，
由 orchestrator 每个持久化 tick 写入）+ 指令队列深度——控制台与引擎仅经
「队列 + DB」通信（§5.5 I6），本端点不触碰引擎对象、不产生任何写。

S5c 扩展字段（``_s5c_dashboard_fields``，总览 dashboard 其余横幅/标识依据）：
``dry_run``（试运行显著标识 [FR3.9]）、``rampup``（放量期）、``risk_pause``
（M5 风控暂停详情）、``platform_limit_today``（平台限额红条 [B6]）。
全部直读 run_state（键缺失回退配置/空值，不播种——保持 GET 幂等与
响应确定性）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from job_agent.console.context import get_context
from job_agent.console.snapshot import ReadOnlyDatabase, read_state
from job_agent.core import dryrun, rampup
from job_agent.core.orchestrator import (
    KEY_ENGINE_STATE,
    KEY_LAST_COMMAND,
    KEY_PLATFORM_LIMIT_DATE,
)
from job_agent.timeutil import beijing_date_str

router = APIRouter(prefix="/api", tags=["console"])

#: 引擎尚未写入 run_state 时的占位快照（首次启动/未装配引擎线程）
_ENGINE_UNKNOWN: dict[str, str] = {
    "state": "unknown",
    "detail": "引擎尚未启动或未写入 run_state（S5d 装配后自动出现）",
}


def _s5c_dashboard_fields(view: ReadOnlyDatabase, config_dry_run_default: bool) -> dict[str, Any]:
    """S5c 总览扩展字段（只读、确定性：无时间戳，GET 幂等可重复）。"""
    stored = read_state(view, dryrun.KEY_DRY_RUN)
    dry = config_dry_run_default if stored is None else bool(stored)
    ramp = read_state(view, rampup.KEY_RAMPUP)
    rampup_view: dict[str, Any] = {"state": "unknown", "days_left": None}
    if isinstance(ramp, dict) and ramp.get("state") in ("inactive", "active", "done"):
        state = str(ramp["state"])
        days_left: int | None = None
        if state == "active":
            effective = ramp.get("effective_days")
            enabled = ramp.get("enabled_days")
            if isinstance(effective, list) and isinstance(enabled, int):
                days_left = max(0, enabled - len(effective))
        rampup_view = {"state": state, "days_left": days_left}
    info = read_state(view, rampup.KEY_RISK_PAUSE)
    limit_date = read_state(view, KEY_PLATFORM_LIMIT_DATE)
    return {
        "dry_run": dry,
        "rampup": rampup_view,
        "risk_pause": info if isinstance(info, dict) else None,
        "platform_limit_today": limit_date == beijing_date_str(datetime.now(UTC)),
    }


@router.get("/status")
def engine_status(request: Request) -> dict[str, Any]:
    """引擎状态快照：engine / last_command / 队列深度 + S5c 总览扩展字段。"""
    ctx = get_context(request)
    engine: Any = None
    last_command: Any = None
    extra: dict[str, Any] = {
        "dry_run": bool(ctx.config_loader().delivery.dry_run),
        "rampup": {"state": "unknown", "days_left": None},
        "risk_pause": None,
        "platform_limit_today": False,
    }
    if ctx.db is not None:
        engine = read_state(ctx.db, KEY_ENGINE_STATE)
        last_command = read_state(ctx.db, KEY_LAST_COMMAND)
        extra = _s5c_dashboard_fields(ctx.db, bool(ctx.config_loader().delivery.dry_run))
    if not isinstance(engine, dict):
        engine = dict(_ENGINE_UNKNOWN)
    if not isinstance(last_command, dict):
        last_command = None
    depth: int | None = None
    if ctx.command_queue is not None:
        depth = ctx.command_queue.qsize()  # 只读计数，不消费队列
    return {
        "engine": engine,
        "last_command": last_command,
        "command_queue_depth": depth,
        **extra,
    }
