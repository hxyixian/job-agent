"""通知层基础契约（§5.5 I5 提供方 / §11）：EventType 全枚举、级别权威表、Notifier 插件接口。

- ``EventType``：§5.5 I5 的英文成员全表（21 个），值 = 成员名（与
  ``notification_events.event_type`` 落库字符串、core 各模块的既有用法一致）；
- ``EVENT_LEVELS``：事件 → 级别的**权威映射**，逐条对照 §5.5 I5 分组注释。
  **RAMPUP_DONE = low**（裁决 2026-11-07：需求 FR3.9/N1"低级通知"为准，
  I5 枚举分组中的 medium 归属是笔误，S2d 占位协议已同步）；
- ``level_of(event)``：枚举成员或字符串 → 级别；未知类型抛 ``ValueError``
  （拼写错误在发射期暴露，不静默丢弃）；
- ``Notifier``：通知渠道插件接口（§11"``Notifier.send(event)``；新增渠道纯增量"）。
  ``applies(level)`` 承担级别过滤（飞书按配置、邮件恒 high），
  ``configured`` 标志渠道参数是否完备（未配置时管线记 skip:not_configured，
  不报错——通知失败不阻断业务）。

中文页面文案由控制台前端映射（I5 注明），本层只使用英文成员与级别字面量。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, ClassVar, Literal, NamedTuple

__all__ = [
    "EVENT_LEVELS",
    "EventRecord",
    "EventType",
    "Level",
    "Notifier",
    "level_of",
]

Level = Literal["high", "medium", "low"]


class EventType(StrEnum):
    """事件类型全枚举（§5.5 I5；值 = 成员名，即落库/查询用的字符串）。"""

    # ---- high ----
    LOGIN_EXPIRED = "LOGIN_EXPIRED"  # 登录失效 [FR1.3]
    RISK_PAUSE = "RISK_PAUSE"  # 风控/验证码暂停 [FR1.4/M5]
    ADAPTER_BROKEN = "ADAPTER_BROKEN"  # 适配器疑似失效 [B7]
    OUTBOX_TIMEOUT = "OUTBOX_TIMEOUT"  # 待发箱超时（合并一条）[s6/FR6.4]
    BROWSER_LOST = "BROWSER_LOST"  # 浏览器会话丢失
    PAGE_EVICTED = "PAGE_EVICTED"  # 页面被驱逐（EVICTED）[G2]
    SECURITY_CHECK = "SECURITY_CHECK"  # 安全验证类路径/页面 [G2/H1]

    # ---- medium ----
    INTERVIEW_INVITE = "INTERVIEW_INVITE"  # 面试邀约
    KEY_QUESTION = "KEY_QUESTION"  # 关键提问待确认
    OFFER_SIGNAL = "OFFER_SIGNAL"  # offer 迹象
    CONV_LIMIT_REACHED = "CONV_LIMIT_REACHED"  # 会话达自动上限 [§8.1]
    MULTIMODAL_PENDING = "MULTIMODAL_PENDING"  # 多模态待处理
    CONTACT_REQUEST = "CONTACT_REQUEST"  # 联系方式交换请求（永不自动处理）
    PLATFORM_LIMIT = "PLATFORM_LIMIT"  # 平台限额触发 [B6]
    AI_BUDGET_EXHAUSTED = "AI_BUDGET_EXHAUSTED"  # AI 日预算耗尽 [B13]
    CHROME_UPGRADED = "CHROME_UPGRADED"  # Chrome 主版本变化联动降档 [V9]
    GREETING_MISMATCH = "GREETING_MISMATCH"  # 平台招呼语与模板不一致 [F1]
    BROWSE_LIMIT_REACHED = "BROWSE_LIMIT_REACHED"  # 我方浏览上限达到 [F10]

    # ---- low ----
    DAILY_DIGEST = "DAILY_DIGEST"  # 每日运行摘要 [s1/B11：摘要不补发]
    DRYRUN_DATA_READY = "DRYRUN_DATA_READY"  # 试运行数据就绪 [FR3.9]
    RAMPUP_DONE = "RAMPUP_DONE"  # 放量期结束——low（裁决 2026-11-07，需求 FR3.9/N1 为准）


# 事件 → 级别权威表（§5.5 I5 分组逐条对照；与 EventType 成员一一对应）
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
    "CHROME_UPGRADED": "medium",
    "GREETING_MISMATCH": "medium",
    "BROWSE_LIMIT_REACHED": "medium",
    # low（RAMPUP_DONE 裁决见 EventType 成员注释）
    "DAILY_DIGEST": "low",
    "DRYRUN_DATA_READY": "low",
    "RAMPUP_DONE": "low",
}


def level_of(event: EventType | str) -> Level:
    """事件（枚举成员或字符串）→ 级别 [I5]；未知类型抛 ``ValueError``。"""
    name = event.value if isinstance(event, EventType) else event
    try:
        return EVENT_LEVELS[name]
    except KeyError:
        raise ValueError(f"未知事件类型：{name!r}（§5.5 I5 枚举之外）") from None


def event_name(event: EventType | str) -> str:
    """事件（枚举成员或字符串）→ 落库用字符串名；未知类型抛 ``ValueError``。"""
    name = event.value if isinstance(event, EventType) else event
    if name not in EVENT_LEVELS:
        raise ValueError(f"未知事件类型：{name!r}（§5.5 I5 枚举之外）")
    return name


class EventRecord(NamedTuple):
    """一次事件发射的元组（``emit`` 的返回值；S2d 占位协议形状保持不变）。

    ``id``：``notification_events`` 行 id（已落库）；合成事件（如免打扰
    合并推送）无对应行，为 ``None``。
    """

    event_type: str
    level: Level
    payload: dict[str, Any]
    id: int | None
    created_at: str  # ISO-8601 UTC 文本


class Notifier(ABC):
    """通知渠道插件接口（§11：``send(event)``；新增渠道纯增量 [FR7.1]）。

    实现方约定：
    - ``applies(level)``：该渠道是否接收此级别（级别过滤在 center 统一调用）；
    - ``configured``：渠道参数是否完备（webhook/SMTP 缺失时为 False，
      管线记 ``skip:not_configured``，**不调用 send、不报错**）；
    - ``send(record)``：执行投递；失败**抛异常**（由管线捕获并记
      ``error:<异常名>``，事件落库不受影响）；返回 ``None``。
    """

    name: ClassVar[str] = "notifier"

    @property
    def configured(self) -> bool:
        return True

    @abstractmethod
    def applies(self, level: Level) -> bool: ...

    @abstractmethod
    def send(self, record: EventRecord) -> None: ...

    def close(self) -> None:  # noqa: B027 可选生命周期钩子，默认无资源可释放
        """释放渠道资源（httpx 客户端等）；由 NotificationCenter.close 统一调用。"""
