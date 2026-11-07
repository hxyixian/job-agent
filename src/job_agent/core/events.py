"""I5 事件本地占位协议（S2d 过渡层，S3d 完成后由 MainAgent 对接替换）。

背景：I5（notify 层 ``emit``）由 S3d 交付，S2d（session/rampup/dryrun）
先按任务书约定使用本占位协议：

1. ``emit`` 把事件**全量写入 ``notification_events`` 表**（与 §11 路管线
   第一跳一致：事件先落库，级别路由/免打扰/推送由 S3d 的 Notifier 负责）；
2. 同时**返回事件元组** :class:`EventRecord`，供调用方（S4c/S4d 编排与
   单元测试）断言事件内容，不依赖通知中心；
3. 级别默认表 ``EVENT_LEVELS`` 逐条抄自 §5.5 I5 的枚举分组（I5 注明
   "英文成员 ↔ §11 级别（权威对应）"），``level_override`` 参数与 I5 的
   ``emit(event, payload, level_override)`` 签名同构。

对接方式（S3d 完成后）：本模块仅被 core/session.py、core/rampup.py、
core/dryrun.py 引用——把三处 ``events.emit(...)`` 替换为 notify 层的
``emit``（或在本模块内转发到 notify 层）即可；``notification_events``
表结构不变，占位期间落库的事件可被通知中心直接回溯读取。

已知文档分歧（留 MainAgent 裁决）：RAMPUP_DONE 在 §5.5 I5/§11 归
medium，而 §13/需求文档 FR3.9/验收 A-CORE-17 写"低级通知/低级事件"。
本模块按 I5 的"权威对应"取 medium；如裁决为 low，改
``EVENT_LEVELS["RAMPUP_DONE"]`` 一处即可（或调用处传 level_override）。
"""

from __future__ import annotations

import json
from typing import Any, Literal, NamedTuple

from job_agent.db import Database
from job_agent.timeutil import utcnow_str

__all__ = [
    "EVENT_LEVELS",
    "EventRecord",
    "Level",
    "emit",
]

Level = Literal["high", "medium", "low"]

# §5.5 I5 EventType 枚举 → 级别的权威映射（分组逐条对照，未使用的成员也收录，
# 便于 S3d 对齐与占位期的拼写检查）。
EVENT_LEVELS: dict[str, Level] = {
    # high
    "LOGIN_EXPIRED": "high",
    "RISK_PAUSE": "high",
    "ADAPTER_BROKEN": "high",
    "OUTBOX_TIMEOUT": "high",
    "BROWSER_LOST": "high",
    "PAGE_EVICTED": "high",
    "SECURITY_CHECK": "high",
    # medium
    "INTERVIEW_INVITE": "medium",
    "KEY_QUESTION": "medium",
    "OFFER_SIGNAL": "medium",
    "CONV_LIMIT_REACHED": "medium",
    "MULTIMODAL_PENDING": "medium",
    "CONTACT_REQUEST": "medium",
    "PLATFORM_LIMIT": "medium",
    "AI_BUDGET_EXHAUSTED": "medium",
    "RAMPUP_DONE": "low",  # 裁决（2026-11-07 MainAgent）：以需求 FR3.9/N1 决策"低级通知"为准
    "CHROME_UPGRADED": "medium",
    "GREETING_MISMATCH": "medium",
    "BROWSE_LIMIT_REACHED": "medium",
    # low
    "DAILY_DIGEST": "low",
    "DRYRUN_DATA_READY": "low",
}


class EventRecord(NamedTuple):
    """占位协议的事件元组（emit 的返回值）。"""

    event_type: str
    level: Level
    payload: dict[str, Any]
    id: int | None  # notification_events 行 id（占位期即已落库）
    created_at: str  # ISO-8601 UTC 文本


def emit(
    db: Database,
    event_type: str,
    payload: dict[str, Any],
    level_override: Level | None = None,
) -> EventRecord:
    """写入 notification_events 并返回事件元组（占位实现，见模块 docstring）。

    - 未知 event_type 抛 ValueError（枚举成员拼写错误尽早暴露）；
    - delivered_json 置 NULL：投递状态由 S3d 通知管线回填。
    """
    if event_type not in EVENT_LEVELS:
        raise ValueError(f"未知事件类型：{event_type!r}（§5.5 I5 枚举之外）")
    level = level_override if level_override is not None else EVENT_LEVELS[event_type]
    created_at = utcnow_str()
    row_id = db.execute_returning_id(
        "INSERT INTO notification_events (event_type, level, payload_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (event_type, level, json.dumps(payload, ensure_ascii=False, sort_keys=True), created_at),
    )
    return EventRecord(
        event_type=event_type, level=level, payload=payload, id=row_id, created_at=created_at
    )
