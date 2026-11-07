"""S5c 待发箱 API（§10.2 待发箱行；§8.3/B10 规则；任务书 S5c 第 2 条）。

- ``GET /api/outbox``：列表（来源 ai/manual、会话 HR、创建时间、状态与
  **stale 标记**——会话有新入站消息后既有 pending 草稿被引擎标记 stale，
  确认页提示重新生成）+ 各状态计数；
- ``POST /api/outbox/{id}/approve``：经队列 ``approve_outbox``（I6；引擎
  执行 Messenger.approve_outbox_entry——**人工确认不计自动数** [B10]，
  dry_run 下只记拟发送 [N2]）；二次确认由页面 confirm 承担；
- ``POST /api/outbox/{id}/skip``：经队列 ``skip_outbox``；
- ``POST /api/outbox/{id}/edit``：直接改草稿文本（pending/stale 限定；
  纯数据操作不涉站点，approve 时引擎按最新文本执行）；
- ``POST /api/outbox/{id}/regenerate``：控制台线程重组会话上下文
  （近期消息/简历/方向，§5.5 I4 同构）→ 分类 → AI 重拟 → stale 归位
  pending；AI 失败 503 且条目保持原状（不吞错、不写空串）。

预检语义：``404=not_found``、``409=invalid_state``（快路径反馈；引擎
消费时仍按最新状态复检，竞态以引擎 ack 为准）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from job_agent.ai.classify import ClassifyResult

from .context import OpsContext, QueuedResponse, conversation_context, issue

__all__ = ["attach_outbox_routes"]

#: 待发箱可操作状态（§8.3：pending/stale 可确认、跳过、编辑、重生成）
_ACTIONABLE = ("pending", "stale")
#: 状态展示顺序（可操作在前，最新在前）
_STATE_ORDER = (
    "CASE state WHEN 'pending' THEN 0 WHEN 'stale' THEN 1"
    " WHEN 'approved' THEN 2 WHEN 'skipped' THEN 3 ELSE 4 END"
)
#: regenerate 的合法草拟类别（§8.2 AI 四分类；no_reply/异常值保守回落 key）
_DRAFT_CATEGORIES = frozenset({"low_risk", "key", "positive_response"})


class OutboxEditBody(BaseModel):
    """POST /outbox/{id}/edit 请求体。"""

    draft_text: str = Field(min_length=1, max_length=2000)


class OutboxMutationResponse(BaseModel):
    """edit / regenerate 的同步结果（approve/skip 为异步指令，走 QueuedResponse）。"""

    id: int
    conv_id: str
    draft_text: str
    source: str
    state: str
    category: str | None = None
    guard_warnings: list[str] = Field(default_factory=list)


def _outbox_row(ctx: OpsContext, entry_id: int) -> Any:
    row = ctx.db.query_one(
        "SELECT id, conv_id, draft_text, source, state FROM outbox WHERE id = ?", (entry_id,)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row


def _require_actionable(ctx: OpsContext, entry_id: int) -> Any:
    row = _outbox_row(ctx, entry_id)
    if str(row["state"]) not in _ACTIONABLE:
        raise HTTPException(status_code=409, detail=f"invalid_state:{row['state']}")
    return row


def attach_outbox_routes(router: APIRouter, ctx: OpsContext) -> None:
    """注册待发箱路由（由 ops.create_ops_router 调用）。"""

    @router.get("/outbox")
    def get_outbox(state: str | None = None, limit: int = 200) -> dict[str, Any]:
        """待发箱列表（state 过滤可选；可操作状态在前、时间倒序）。"""
        params: list[Any] = []
        where = ""
        if state is not None:
            where = "WHERE o.state = ?"
            params.append(state)
        rows = ctx.db.query(
            f"SELECT o.id, o.conv_id, c.peer_name, o.draft_text, o.source, o.state,"
            f" o.created_at, o.reminded_at FROM outbox o"
            f" LEFT JOIN conversations c ON c.conv_id = o.conv_id {where}"
            f" ORDER BY {_STATE_ORDER}, o.created_at DESC, o.id DESC LIMIT ?",
            (*params, max(1, min(limit, 500))),
        )
        items = [
            {
                "id": int(r["id"]),
                "conv_id": str(r["conv_id"]),
                "peer_name": str(r["peer_name"] or ""),
                "draft_text": str(r["draft_text"]),
                "source": str(r["source"]),
                "state": str(r["state"]),
                "stale": str(r["state"]) == "stale",
                "created_at": str(r["created_at"]),
                "reminded_at": str(r["reminded_at"]) if r["reminded_at"] else None,
            }
            for r in rows
        ]
        counts: dict[str, int] = {}
        for r in ctx.db.query("SELECT state, COUNT(*) AS n FROM outbox GROUP BY state"):
            counts[str(r["state"])] = int(r["n"])
        return {"items": items, "counts": counts}

    @router.post("/outbox/{entry_id}/approve")
    def post_approve(entry_id: int) -> QueuedResponse:
        """用户确认发送（二次确认在页面；引擎执行不计自动数 [B10]）。"""
        _require_actionable(ctx, entry_id)
        req_id = issue(ctx, "approve_outbox", {"outbox_id": entry_id})
        return QueuedResponse(req_id=req_id, cmd="approve_outbox")

    @router.post("/outbox/{entry_id}/skip")
    def post_skip(entry_id: int) -> QueuedResponse:
        _require_actionable(ctx, entry_id)
        req_id = issue(ctx, "skip_outbox", {"outbox_id": entry_id})
        return QueuedResponse(req_id=req_id, cmd="skip_outbox")

    @router.post("/outbox/{entry_id}/edit")
    def post_edit(entry_id: int, body: OutboxEditBody) -> OutboxMutationResponse:
        row = _require_actionable(ctx, entry_id)
        text = body.draft_text.strip()
        if not text:
            raise HTTPException(status_code=422, detail="draft_text_empty")
        ctx.db.execute("UPDATE outbox SET draft_text = ? WHERE id = ?", (text, entry_id))
        return OutboxMutationResponse(
            id=entry_id,
            conv_id=str(row["conv_id"]),
            draft_text=text,
            source=str(row["source"]),
            state=str(row["state"]),
        )

    @router.post("/outbox/{entry_id}/regenerate")
    def post_regenerate(entry_id: int) -> OutboxMutationResponse:
        """AI 重拟草稿（分类 → 草拟 → 护栏提示；stale → pending 归位）。

        AI 异常（传输/预算/解析）→ 503，条目保持原状，由用户决定重试或
        人工编辑（与 §8.2"AI 失败降级人工"一致）。
        """
        row = _require_actionable(ctx, entry_id)
        conv = conversation_context(ctx, str(row["conv_id"]))
        result: ClassifyResult = ctx.classify_fn(list(conv.recent_messages))
        category = result.category if result.category in _DRAFT_CATEGORIES else "key"
        try:
            draft = ctx.draft_fn(conv, category)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"ai_error:{type(exc).__name__}") from exc
        draft = draft.strip()
        if not draft:
            raise HTTPException(status_code=503, detail="ai_error:empty_draft")
        guard = ctx.guard_fn(draft)
        warnings = [v.reason for v in guard.reasons] if not guard.ok else []
        ctx.db.execute(
            "UPDATE outbox SET draft_text = ?, source = 'ai', state = 'pending' WHERE id = ?",
            (draft, entry_id),
        )
        return OutboxMutationResponse(
            id=entry_id,
            conv_id=str(row["conv_id"]),
            draft_text=draft,
            source="ai",
            state="pending",
            category=category,
            guard_warnings=warnings,
        )
