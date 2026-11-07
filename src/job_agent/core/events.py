"""I5 事件协议转发层（S2d 占位协议 → S3d 通知管线，2026-11-07 对接完成）。

对接方式（S3d 交付裁定：不改名、不破坏签名）：

- 级别权威表 ``EVENT_LEVELS`` / 枚举 ``EventType`` / 落库 INSERT 的单一实现
  在 :mod:`job_agent.notify`（base = §5.5 I5 权威表，RAMPUP_DONE=low）；
  本模块对以上名字**再导出**，既有导入路径（core/session、core/rampup、
  core/dryrun 及其测试）不变；
- ``emit(db, ...)``：查 :func:`job_agent.notify.center_for`——
  - 该 db 实例注册了 ``NotificationCenter``（引擎装配方构造即自动注册）→
    转发到完整管线（级别路由 → 免打扰/合并 [B11] → 渠道投递），返回同一
    :class:`EventRecord`；
  - 未注册 → 退化为"仅落库"（:func:`notify.center.store_event`），与 S2d
    占位协议行为逐字节一致（无通知装配的调用方/测试可直接断言落库）。
- 两种路径下**事件全量落库语义不变**：每条 emit 恰好产生一行
  ``notification_events``（payload 序列化参数、created_at 格式、未知
  event_type 抛 ``ValueError`` 均保持原样）；差别仅在注册管线后
  ``delivered_json`` 由 NULL 变为投递状态汇总（渠道状态/免打扰延后标记）。
"""

from __future__ import annotations

from typing import Any

from job_agent.db import Database
from job_agent.notify.base import (
    EVENT_LEVELS,
    EventRecord,
    EventType,
    Level,
    event_name,
    level_of,
)
from job_agent.notify.center import center_for, store_event

__all__ = [
    "EVENT_LEVELS",
    "EventRecord",
    "EventType",
    "Level",
    "emit",
    "event_name",
    "level_of",
]


def emit(
    db: Database,
    event_type: str,
    payload: dict[str, Any],
    level_override: Level | None = None,
) -> EventRecord:
    """写入 notification_events（全量）并返回事件元组；注册了通知中心时走完整管线。

    - 未知 event_type 抛 ValueError（枚举成员拼写错误尽早暴露）；
    - ``level_override`` 与 I5 的 ``emit(event, payload, level_override)``
      签名同构（覆盖路由级别，落库级别随之改变）。
    """
    center = center_for(db)
    if center is not None:
        return center.emit(event_type, payload, level_override)
    return store_event(db, event_type, payload, level_override)
