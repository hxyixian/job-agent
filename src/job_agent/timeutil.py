"""时间工具：UTC 存储与北京自然日的换算契约 [R6-4 / B18]。

契约（设计文档 §5.5 I1）：
- 所有时间戳以 UTC ISO-8601 文本存储（字典序 == 时间序）；
- "每日"窗口（投递/浏览日限、放量有效运行日、每日摘要）一律按
  **Asia/Shanghai 自然日**划分：日窗口起点 = 北京时间当天 00:00 换算成的
  UTC 时间戳文本，再与 created_at 做字符串比较；
- 滑动窗口（小时限额）直接用 UTC 时间差，与自然日无关。

共用方：S2b（quota）、S2c（调度）、S2d（放量）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

# 数据库时间戳格式（与 0001 迁移 SQL DEFAULT 同构：毫秒精度 + Z）
UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
# 北京时间 = UTC+8（无夏令时，固定偏移即可，无需 zoneinfo）
CST = timezone(timedelta(hours=8))


def _fmt_utc(dt: datetime) -> str:
    """UTC datetime → 毫秒精度 ISO 文本（与 SQL strftime('%...%fZ','now') 同构）。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def as_utc(dt: datetime) -> datetime:
    """任意 datetime → UTC（naive 视为 UTC [S2b 补充，供状态机/配额共用]）。"""
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def utc_str(dt: datetime) -> str:
    """任意 datetime → 毫秒精度 UTC 文本（与库内时间戳同构，可直接字符串比较）。

    [S2b 补充] 滑动窗口起点等"非当前时刻"的时间戳文本必须走本函数：
    ``strftime(UTC_FORMAT)`` 会产出 6 位微秒，与库内 3 位毫秒字典序不一致。
    """
    return _fmt_utc(as_utc(dt))


def utcnow_str() -> str:
    """当前 UTC 时间戳文本（毫秒精度，供写入数据库）。"""
    return _fmt_utc(datetime.now(UTC))


def day_start_utc(now: datetime | None = None) -> str:
    """北京自然日的窗口起点（UTC 时间戳文本）[R6-4]。

    例：北京时间 2026-11-08 02:00（= UTC 2026-11-07 18:00）→
    返回 "2026-11-07T16:00:00.000Z"（北京 11-08 00:00 的 UTC 表示）。
    比较规则：``created_at >= day_start_utc(now)`` 即属于"今天"（北京日）。
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    beijing = now.astimezone(CST)
    beijing_midnight = beijing.replace(hour=0, minute=0, second=0, microsecond=0)
    return _fmt_utc(beijing_midnight.astimezone(UTC))


def beijing_date_str(now: datetime | None = None) -> str:
    """北京自然日标识（YYYY-MM-DD，run_state 的放量日计数等使用）。"""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(CST).strftime("%Y-%m-%d")
