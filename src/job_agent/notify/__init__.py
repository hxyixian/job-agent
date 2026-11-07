"""通知层（§11 / §5.5 I5 提供方，Stage3 S3d）。

路由管线：emit → 全量落库 notification_events → 级别路由 → 免打扰/合并
[B11] → 飞书 Webhook（签名）+ SMTP（仅 high）；控制台通知中心直查
``notification_events``（始终实时）。渠道插件接口 :class:`Notifier`
（新增渠道纯增量 [FR7.1]）。

与既有调用方的对接：``core.events.emit(db, ...)``（S2d 占位协议签名）在
注册了 :class:`NotificationCenter` 的库上自动转发到本层完整管线，
未注册时保持"仅落库"行为——详见 :mod:`job_agent.core.events`。
"""

from job_agent.notify.base import (
    EVENT_LEVELS,
    EventRecord,
    EventType,
    Level,
    Notifier,
    event_name,
    level_of,
)
from job_agent.notify.center import (
    NotificationCenter,
    center_for,
    default_notifiers,
    dnd_window_bounds,
    list_events,
    register_center,
    scan_outbox_timeout,
    store_event,
    unregister_center,
)
from job_agent.notify.email import EmailNotifier
from job_agent.notify.feishu import FeishuNotifier, format_event_text, gen_sign

__all__ = [
    "EVENT_LEVELS",
    "EmailNotifier",
    "EventRecord",
    "EventType",
    "FeishuNotifier",
    "Level",
    "NotificationCenter",
    "Notifier",
    "center_for",
    "default_notifiers",
    "dnd_window_bounds",
    "event_name",
    "format_event_text",
    "gen_sign",
    "level_of",
    "list_events",
    "register_center",
    "scan_outbox_timeout",
    "store_event",
    "unregister_center",
]
