"""S5c 通知中心 API（§10.2 通知中心行；§11"全量落库可回溯"）。

``GET /api/notifications``：全量事件流（notification_events 只读）+
级别过滤（high/medium/low，I5 权威表）+ 事件类型过滤 + 分页，时间倒序。
中文文案由前端映射（I5 约定），API 只回英文成员与结构化 payload。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter

from .context import OpsContext

__all__ = ["attach_notifications_routes"]


def attach_notifications_routes(router: APIRouter, ctx: OpsContext) -> None:
    """注册通知中心路由（由 ops.create_ops_router 调用）。"""

    @router.get("/notifications")
    def get_notifications(
        level: Literal["high", "medium", "low"] | None = None,
        event_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """事件流查询（级别/类型过滤 + 分页；created_at 倒序，id 破平）。"""
        conditions: list[str] = []
        params: list[Any] = []
        if level is not None:
            conditions.append("level = ?")
            params.append(level)
        if event_type is not None:
            conditions.append("event_type = ?")
            params.append(event_type)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        total = ctx.db.query_one(f"SELECT COUNT(*) AS n FROM notification_events {where}", params)
        rows = ctx.db.query(
            f"SELECT id, event_type, level, payload_json, created_at, delivered_json"
            f" FROM notification_events {where}"
            f" ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, max(1, min(limit, 500)), max(0, offset)),
        )
        items = []
        for r in rows:
            try:
                payload = json.loads(str(r["payload_json"]))
            except ValueError:
                payload = {"raw": str(r["payload_json"])}
            delivered: Any = None
            if r["delivered_json"] is not None:
                try:
                    delivered = json.loads(str(r["delivered_json"]))
                except ValueError:
                    delivered = {"raw": str(r["delivered_json"])}
            items.append(
                {
                    "id": int(r["id"]),
                    "event_type": str(r["event_type"]),
                    "level": str(r["level"]),
                    "payload": payload,
                    "created_at": str(r["created_at"]),
                    "delivered": delivered,
                }
            )
        counts: dict[str, int] = {}
        level_rows = ctx.db.query(
            "SELECT level, COUNT(*) AS n FROM notification_events GROUP BY level"
        )
        for r in level_rows:
            counts[str(r["level"])] = int(r["n"])
        return {
            "items": items,
            "total": int(total["n"]) if total is not None else 0,
            "counts": counts,
            "limit": limit,
            "offset": offset,
        }
