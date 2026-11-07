"""S5c 总览 dashboard API + 四页聚合装配（§10.2/§10.3；任务书 Stage5 S5c）。

总览页数据源（全部只读，§10.1-4 GET 无副作用）：

- ``GET /api/status``：**S5a 已建**（``console/api/status.py``，引擎状态 +
  last_command ack + 队列深度），本模块**扩展**其响应——dry_run /
  放量期 / 风控暂停 / 平台限额日（见 ``console/api/status.py`` 的
  ``_s5c_dashboard_fields``；保持确定性，GET 幂等可重复）；
- ``GET /api/alerts``：告警横幅（M5 风控暂停含 EVICTED·SECURITY_CHECK
  信号 / 登录失效 / 适配器失效 / 平台限额红条 / 登录模式互斥提示）+
  恢复指引 [FR11.3]；
- ``GET /api/prereq``：§4 五项前置清单（缺项亮红 + 指引 [B14]）；
- ``GET /api/dryrun/planned``：试运行拟发送清单（"正式运行将发出什么"
  [FR3.9]）。

控制端点（§5.5 I6，一律经 ``console.commands.issue_command`` 入队、立即
返回 req_id）：``POST /api/engine/{start,stop,pause,resume}``、
``POST /api/engine/clear-risk-pause``（M5 唯一用户恢复通道，问题清单 v16
"S5c：恢复入口"注记——引擎侧消费体在 ``orchestrator._execute_command``，
本 SubStage 增补进 ``COMMANDS``）、``POST /api/browser/relaunch``、
``POST /api/browser/login-mode``（payload ``{"closed": true}`` = 登录完成
通知，S4d 契约）。

装配（S5a ``create_app`` / S5d ``__main__``，与 S5b 工厂同款）::

    from job_agent.console.api.ops import register_ops_routes

    register_ops_routes(app, db=db, command_queue=orch.command_queue(),
                        config_loader=lambda: orch.config)

或仅取 router 自行 include（路由自带 ``/api`` 前缀，勿重复加）。
"""

from __future__ import annotations

import queue
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, FastAPI

from job_agent.config import Config, load_config
from job_agent.core import dryrun, rampup, runstate
from job_agent.core.orchestrator import KEY_PLATFORM_LIMIT_DATE, EngineCommand
from job_agent.db import Database
from job_agent.timeutil import beijing_date_str, utc_str

from .context import LoginModeBody, OpsContext, QueuedResponse, issue
from .notifications import attach_notifications_routes
from .outbox import attach_outbox_routes
from .records import attach_records_routes

__all__ = [
    "OpsContext",
    "alerts_view",
    "create_ops_router",
    "dry_run_view",
    "planned_view",
    "prereq_view",
    "rampup_view",
    "register_ops_routes",
]


# ---- run_state 只读视图（GET 无副作用：不播种、不触引擎惰性初始化）----


def dry_run_view(db: Database, config: Config) -> bool:
    """当前试运行模式（直读 run_state.dry_run；键缺失回退配置，不播种）。"""
    stored = runstate.get_state(db, dryrun.KEY_DRY_RUN)
    if stored is None:
        return bool(config.delivery.dry_run)
    return bool(stored)


def rampup_view(db: Database) -> dict[str, Any]:
    """放量期快照（直读 run_state.ramp_up；不触发 _ensure_rampup 写入）。"""
    stored = runstate.get_state(db, rampup.KEY_RAMPUP)
    if not isinstance(stored, dict) or stored.get("state") not in ("inactive", "active", "done"):
        return {"state": "unknown", "days_left": None}
    state = str(stored["state"])
    days_left: int | None = None
    if state == "active":
        effective = stored.get("effective_days")
        enabled = stored.get("enabled_days")
        if isinstance(effective, list) and isinstance(enabled, int):
            days_left = max(0, enabled - len(effective))
    return {"state": state, "days_left": days_left}


def prereq_view(ctx: OpsContext) -> dict[str, Any]:
    """§4 启动前置条件清单（B14）：DB 可判项权威计算，运行时项读引擎详情。

    运行时项（AI 连通性 / 登录态）由引擎侧检测——本视图只读
    ``run_state[ctx.prereq_detail_key]``（S4d/S5d 写入 ``{"ai": bool,
    "login": bool}``）；键缺失返回 ``ok=null``（未检测，不亮红，但
    ``all_met`` 保守为 False）。dry_run 下登录态缓冲为运行时检测（§4-⑤）。
    """
    detail = runstate.get_state(ctx.db, ctx.prereq_detail_key)
    runtime: dict[str, Any] = detail if isinstance(detail, dict) else {}
    row = ctx.db.query_one("SELECT 1 AS x FROM resume_snapshots WHERE is_current = 1")
    directions = ctx.db.query_one("SELECT COUNT(*) AS n FROM job_directions WHERE is_confirmed = 1")
    greeting = ctx.db.query_one(
        "SELECT 1 AS x FROM greeting_templates WHERE kind = 'active' AND is_current = 1"
    )
    dry = dry_run_view(ctx.db, ctx.config_loader())
    items = [
        {
            "key": "ai_connectivity",
            "ok": runtime.get("ai") if isinstance(runtime.get("ai"), bool) else None,
            "required_in_dry_run": True,
            "guide": "AI 连通性未通过/未检测：前往「AI 配置」页填写端点与密钥并测试连通",
        },
        {
            "key": "resume_parsed",
            "ok": row is not None,
            "required_in_dry_run": True,
            "guide": "简历未解析：前往「简历与方向」页上传 PDF 并完成结构化",
        },
        {
            "key": "directions_confirmed",
            "ok": int(directions["n"]) > 0 if directions is not None else False,
            "required_in_dry_run": True,
            "guide": "缺少已确认的职位方向：前往「简历与方向」页生成并确认 ≥1 组方向",
        },
        {
            "key": "greeting_finalized",
            "ok": greeting is not None,
            "required_in_dry_run": True,
            "guide": "主动招呼语未定稿：前往「招呼语工作台」从候选中定稿 1 条",
        },
        {
            "key": "login_valid",
            "ok": runtime.get("login") if isinstance(runtime.get("login"), bool) else None,
            "required_in_dry_run": False,  # §4-⑤：dry_run 下缓冲为运行时检测
            "guide": "浏览器会话登录态无效：停止引擎后打开「登录模式」重新扫码登录",
        },
    ]
    all_met = all(item["ok"] is True for item in items if item["required_in_dry_run"] or not dry)
    return {"items": items, "all_met": all_met, "dry_run": dry}


def alerts_view(ctx: OpsContext) -> dict[str, Any]:
    """总览告警横幅（§10.3 [FR11.3]）：熔断红条 + 恢复指引。

    数据源全部只读：引擎 fatal（run_state.engine_state）／M5 风控暂停
    （run_state.risk_pause，含 EVICTED·SECURITY_CHECK·CAPTCHA·RISK_PAGE
    信号）／平台限额日（run_state.platform_limit_date=当日）／登录模式
    互斥提示（info 级）。
    """
    from job_agent.core.orchestrator import KEY_ENGINE_STATE

    engine = runstate.get_state(ctx.db, KEY_ENGINE_STATE)
    engine = engine if isinstance(engine, dict) else {}
    now = ctx.clock()
    alerts: list[dict[str, Any]] = []
    # 风控暂停以 run_state.risk_pause（awaiting_user）为权威数据源——引擎
    # 快照的 risk_paused 只是同源镜像（下一 tick 才持久化），横幅须即时呈现
    if rampup.is_risk_paused(ctx.db):
        info = rampup.risk_pause_info(ctx.db) or {}
        signal = str(info.get("signal", "UNKNOWN"))
        m5 = bool(info.get("m5"))
        alerts.append(
            {
                "type": "risk_pause",
                "level": "high",
                "signal": signal,
                "at": info.get("at"),
                "message": (
                    f"风控信号（{signal}）：已暂停全部任务并等待你的决策"
                    + ("（M5：无自动恢复，需人工确认后恢复）" if m5 else "")
                ),
                "guide": (
                    "确认环境安全后，点击「恢复运行（清除风控暂停）」；恢复后引擎自动补一轮对账"
                ),
                "action": "clear_risk_pause",
            }
        )
    fatal = engine.get("fatal_reason")
    if fatal == "login_expired":
        alerts.append(
            {
                "type": "login_expired",
                "level": "high",
                "signal": None,
                "at": engine.get("updated_at"),
                "message": "登录失效：已停止全部任务（不会执行任何登录动作）",
                "guide": "先停止引擎 → 打开「登录模式」扫码登录 → 关闭登录窗口 → 启动引擎",
                "action": "login_mode",
            }
        )
    elif fatal == "adapter_broken":
        alerts.append(
            {
                "type": "adapter_broken",
                "level": "high",
                "signal": None,
                "at": engine.get("updated_at"),
                "message": "适配器疑似失效（关键元素连续定位失败）",
                "guide": "点击「重新拉起浏览器」重建会话；仍失败请检查网络与页面改版",
                "action": "relaunch_browser",
            }
        )
    limit_date = runstate.get_state(ctx.db, KEY_PLATFORM_LIMIT_DATE)
    if isinstance(limit_date, str) and limit_date == beijing_date_str(now):
        alerts.append(
            {
                "type": "platform_limit",
                "level": "high",
                "signal": None,
                "at": None,
                "message": "平台今日沟通达上限：当日已停止投递（消息处理照常）",
                "guide": "次日自动恢复投递；无需处理 [B6]",
                "action": None,
            }
        )
    if engine.get("login_mode"):
        alerts.append(
            {
                "type": "login_mode",
                "level": "info",
                "signal": None,
                "at": None,
                "message": "登录窗口打开中：引擎启动被互斥（同一浏览器会话不能双进程占用）[M3/G5]",
                "guide": "在登录窗口完成扫码后关闭它，再点「登录完成」通知引擎",
                "action": "login_mode_closed",
            }
        )
    return {"alerts": alerts}


def planned_view(ctx: OpsContext, limit: int) -> dict[str, Any]:
    """试运行拟发送清单（§10.3 [FR3.9]："正式运行将发出什么"）。

    只读 ``send_intents.state='skipped_dryrun'``（dryrun.planned_send_list
    纯 SELECT）；greeting 按岗位缓存补标题，reply/resume 补会话 HR 名。
    """
    rows = dryrun.planned_send_list(ctx.db, limit=limit)
    job_ids = [str(r["target_ref"]) for r in rows if r["kind"] == "greeting"]
    conv_ids = [str(r["target_ref"]) for r in rows if r["kind"] in ("reply", "resume")]
    titles = _batch_lookup(ctx.db, "SELECT job_id, title FROM jobs WHERE job_id IN", job_ids)
    peers = _batch_lookup(
        ctx.db, "SELECT conv_id, peer_name FROM conversations WHERE conv_id IN", conv_ids
    )
    items: list[dict[str, Any]] = []
    for r in rows:
        ref = str(r["target_ref"])
        items.append(
            {
                "id": int(r["id"]),
                "kind": str(r["kind"]),
                "target_ref": ref,
                "target_label": titles.get(ref) or peers.get(ref) or ref,
                "created_at": str(r["created_at"]),
            }
        )
    total = ctx.db.query_one(
        "SELECT COUNT(*) AS n FROM send_intents WHERE state = 'skipped_dryrun'"
    )
    return {"items": items, "total": int(total["n"]) if total is not None else 0}


def _batch_lookup(db: Database, sql_head: str, keys: list[str]) -> dict[str, str]:
    """IN 批量取两列映射（键去重；空键集合/空结果 → 空映射；按列序位置取值）。"""
    unique = sorted(set(keys))
    if not unique:
        return {}
    placeholders = ", ".join("?" for _ in unique)
    rows = db.query(f"{sql_head} ({placeholders})", unique)
    return {str(r[0]): str(r[1]) for r in rows}


def window_start(now: datetime, days: int) -> str:
    """统计窗口起点（ISO-8601 UTC 文本；与库内时间戳可直接比较）。"""
    return utc_str(now - timedelta(days=days))


# ---- 装配入口 ----


def create_ops_router(ctx: OpsContext) -> APIRouter:
    """构造 S5c 四页聚合 router（前缀 ``/api``；include 时无需再加前缀）。"""
    router = APIRouter(prefix="/api", tags=["s5c-ops"])

    # ---- 总览：告警 / 前置条件 / 试运行清单（/status 归 S5a，本模块扩展）----
    @router.get("/alerts")
    def get_alerts() -> dict[str, Any]:
        return alerts_view(ctx)

    @router.get("/prereq")
    def get_prereq() -> dict[str, Any]:
        return prereq_view(ctx)

    @router.get("/dryrun/planned")
    def get_planned(limit: int = 100) -> dict[str, Any]:
        return planned_view(ctx, max(1, min(limit, 500)))

    # ---- 控制：start/stop/pause/resume + 登录模式 + 重新拉起（全部经队列 I6）----
    # 注意：字面量路径必须先于 /engine/{action} 注册，否则会被动态段吞掉。

    @router.post("/engine/clear-risk-pause")
    def post_clear_risk_pause() -> QueuedResponse:
        """M5 风控暂停恢复（唯一用户通道；clear_risk_pause 经队列指令）。"""
        req_id = issue(ctx, "clear_risk_pause", {"decided_by": "console"})
        return QueuedResponse(req_id=req_id, cmd="clear_risk_pause")

    @router.post("/engine/{action}")
    def post_engine(action: Literal["start", "stop", "pause", "resume"]) -> QueuedResponse:
        req_id = issue(ctx, action)
        return QueuedResponse(req_id=req_id, cmd=action)

    @router.post("/browser/relaunch")
    def post_relaunch() -> QueuedResponse:
        req_id = issue(ctx, "relaunch_browser")
        return QueuedResponse(req_id=req_id, cmd="relaunch_browser")

    @router.post("/browser/login-mode")
    def post_login_mode(body: LoginModeBody | None = None) -> QueuedResponse:
        """登录模式 [M3/G5]：无体/``closed!=true`` = 打开登录窗口（引擎互斥）；
        ``{"closed": true}`` = 登录完成窗口已关闭通知（S4d payload 契约）。"""
        payload = {"closed": True} if body is not None and body.closed else {}
        req_id = issue(ctx, "login_mode", payload)
        return QueuedResponse(req_id=req_id, cmd="login_mode")

    attach_outbox_routes(router, ctx)
    attach_records_routes(router, ctx, window_start)
    attach_notifications_routes(router, ctx)
    return router


def register_ops_routes(
    app: FastAPI,
    *,
    db: Database,
    command_queue: queue.Queue[EngineCommand],
    config_loader: Callable[[], Config] = load_config,
    **kwargs: Any,
) -> APIRouter:
    """S5a ``create_app`` / S5d 装配入口：include S5c 四页路由。

    与 S5b ``register_content_routes`` 同款显式依赖注入；多余的关键字参数
    原样传给 :class:`OpsContext`（测试注 AI 桩/时钟用）。
    """
    ctx = OpsContext(db=db, command_queue=command_queue, config_loader=config_loader, **kwargs)
    router = create_ops_router(ctx)
    app.include_router(router)
    return router
