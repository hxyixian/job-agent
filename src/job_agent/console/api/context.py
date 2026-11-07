"""S5c 路由共享依赖与视图助手（对齐 S5a 骨架约定，避免循环导入的公共层）。

内容：

- :class:`OpsContext`：S5c 四页的依赖注入集（可写 ``db``（引擎共享实例，
  S5b ``create_*_router`` 同款注入）/ 指令队列 / 配置加载器 / AI 桩 / 时钟）；
- :func:`issue`：经 S5a ``console.commands.issue_command`` 投递 I6 指令并
  返回 ``req_id``（执行 ack 由引擎线程落 ``run_state.last_command``，页面
  经 ``GET /api/status`` 轮询——S5a 已建该端点，S5c 扩展其响应）；
- :class:`QueuedResponse` / :class:`LoginModeBody`：子路由复用的模型；
- ``row_to_message`` / ``company_of_detail`` / ``confirmed_keywords`` /
  ``conversation_context``：待发箱 regenerate 与记录页共用的只读视图助手。
"""

from __future__ import annotations

import json
import queue
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from job_agent.ai.classify import ClassifyResult, classify_messages
from job_agent.ai.guard import GuardResult, guard_outgoing
from job_agent.ai.reply import ConversationContext, draft_reply
from job_agent.config import Config
from job_agent.console.commands import issue_command
from job_agent.core.scheduler import utc_clock
from job_agent.db import Database
from job_agent.models import Message, MsgType, ResumeSnapshot
from job_agent.resume.parser import current_resume_snapshot

__all__ = [
    "LoginModeBody",
    "OpsContext",
    "QueuedResponse",
    "company_of_detail",
    "confirmed_keywords",
    "conversation_context",
    "issue",
    "row_to_message",
]


@dataclass
class OpsContext:
    """S5c 路由的依赖集（装配方注入；测试可替换 AI 桩与时钟）。

    - ``db``：与引擎线程共享的线程安全 :class:`Database`（S5b 同款注入；
      GET 呈现仅用 query/query_one，写操作限于本模块明确的数据面：
      待发箱 edit/regenerate、投递情况手动标记、标注导入——均不触站点）；
    - ``command_queue``：I6 指令队列（S5d 传 ``orch.command_queue()``）；
    - ``config_loader``：配置快照读取；
    - ``draft_fn`` / ``classify_fn`` / ``guard_fn``：regenerate 的 AI 注入点
      （默认绑定 S3b 实现，离线测试注入桩）；
    - ``clock``：时间源（统计窗口/当日口径；测试注入固定时钟）。
    """

    db: Database
    command_queue: queue.Queue[Any]
    config_loader: Callable[[], Config]
    draft_fn: Callable[[ConversationContext, str], str] = draft_reply
    classify_fn: Callable[[list[Message]], ClassifyResult] = classify_messages
    guard_fn: Callable[[str], GuardResult] = guard_outgoing
    clock: Callable[[], datetime] = utc_clock
    #: 引擎写入的逐项前置详情（S4d/S5d 可选写入；键 → {"ai": bool, "login": bool}）
    prereq_detail_key: str = "prereq_detail"


def issue(ctx: OpsContext, cmd: str, payload: dict[str, Any] | None = None) -> str:
    """投递 I6 引擎指令（经 S5a ``commands.issue_command``）并返回 req_id。

    未知指令（``ValueError``）转 404；指令执行是异步的——引擎线程每 tick
    消费，ack 落 ``run_state.last_command``。
    """
    try:
        return issue_command(ctx.command_queue, cmd, payload).req_id
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class QueuedResponse(BaseModel):
    """指令入队成功的统一响应（req_id 供页面与引擎 ack 对账）。"""

    req_id: str
    cmd: str
    queued: bool = True


class LoginModeBody(BaseModel):
    """POST /browser/login-mode 请求体（缺省 = 打开登录窗口）。"""

    closed: bool | None = Field(default=None, description="true=登录完成窗口已关闭通知")


def row_to_message(row: Any) -> Message:
    """messages 行 → Message（与 core.messenger._row_to_message 同构的只读副本）。"""
    from datetime import UTC

    try:
        sent_at = datetime.fromisoformat(str(row["sent_at"]).replace("Z", "+00:00"))
    except ValueError:
        sent_at = datetime.min.replace(tzinfo=UTC)
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    return Message(
        platform_msg_id=str(row["platform_msg_id"]),
        conv_id=str(row["conv_id"]),
        direction=str(row["direction"]),  # type: ignore[arg-type]
        sender=str(row["sender"]),  # type: ignore[arg-type]
        msg_type=MsgType(str(row["msg_type"])),
        text=str(row["text"]),
        sent_at=sent_at,
        is_ours_auto=None if row["is_ours_auto"] is None else bool(row["is_ours_auto"]),
    )


def company_of_detail(detail_json: str) -> str:
    """jobs.detail_json → company_name（解析失败返回空串）。"""
    try:
        data = json.loads(detail_json)
    except ValueError:
        return ""
    value = data.get("company_name") if isinstance(data, dict) else None
    return str(value) if isinstance(value, str) else ""


def confirmed_keywords(db: Database) -> tuple[str, ...]:
    """已确认职位方向关键词（扁平；与 messenger._confirmed_directions 同构）。"""
    rows = db.query("SELECT keywords_json FROM job_directions WHERE is_confirmed = 1")
    keywords: list[str] = []
    for row in rows:
        try:
            data = json.loads(str(row["keywords_json"]))
        except ValueError:
            continue
        if isinstance(data, list):
            keywords.extend(str(k) for k in data)
    return tuple(keywords)


def conversation_context(ctx: OpsContext, conv_id: str) -> ConversationContext:
    """regenerate 的 AI 上下文（§5.5 I4 同构：简历/方向/城市/关联岗位/近期消息）。"""
    conv = ctx.db.query_one(
        "SELECT conv_id, peer_name, job_id FROM conversations WHERE conv_id = ?", (conv_id,)
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation_not_found")
    job_title, company_name = "", ""
    if conv["job_id"] is not None:
        job = ctx.db.query_one(
            "SELECT title, detail_json FROM jobs WHERE job_id = ?", (str(conv["job_id"]),)
        )
        if job is not None:
            job_title = str(job["title"])
            company_name = company_of_detail(str(job["detail_json"]))
    rows = ctx.db.query(
        "SELECT platform_msg_id, conv_id, direction, sender, msg_type, text, sent_at,"
        " is_ours_auto FROM messages WHERE conv_id = ? ORDER BY sent_at DESC LIMIT 10",
        (conv_id,),
    )
    recent = [row_to_message(r) for r in rows]
    recent.reverse()
    config = ctx.config_loader()
    return ConversationContext(
        conv_id=conv_id,
        peer_name=str(conv["peer_name"]),
        job_title=job_title,
        company_name=company_name,
        target_city=config.target.city,
        directions=confirmed_keywords(ctx.db),
        resume=current_resume_snapshot(ctx.db) or ResumeSnapshot(),
        recent_messages=tuple(recent),
    )
