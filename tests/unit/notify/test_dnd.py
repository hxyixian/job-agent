"""S3d 免打扰三态 [B11]：窗口外即时推 / 窗口内 high 旁路 / 窗口内中低延后合并。

免打扰窗口以北京时间表述（R6-4 契约），默认 23:00-07:30 跨午夜。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from job_agent.config import Config, DndConfig
from job_agent.db import Database
from job_agent.notify import dnd_window_bounds
from job_agent.timeutil import CST, utc_str

from .conftest import RecordingNotifier, events_in, make_center

# 北京时刻助手（DND 以北京时间表述）
BJ = CST


def at(day: str, hhmmss: str) -> datetime:
    d = datetime.strptime(day, "%Y-%m-%d")
    t = datetime.strptime(hhmmss, "%H:%M:%S")
    return datetime.combine(d.date(), t.time(), tzinfo=BJ)


def dnd_config(
    *, enabled: bool = True, start: str = "23:00", end: str = "07:30", high_bypass: bool = True
) -> DndConfig:
    return DndConfig(enabled=enabled, start=start, end=end, high_bypass=high_bypass)


# ---- 窗口几何（含跨午夜与边界） ----


@pytest.mark.parametrize(
    ("start", "end", "day", "hhmmss", "active"),
    [
        # 跨午夜 23:00-07:30（出厂默认）
        ("23:00", "07:30", "2026-11-09", "23:00:00", True),  # 起点含
        ("23:00", "07:30", "2026-11-09", "23:59:59", True),
        ("23:00", "07:30", "2026-11-10", "03:00:00", True),  # 凌晨段（昨天 start）
        ("23:00", "07:30", "2026-11-10", "07:29:59", True),
        ("23:00", "07:30", "2026-11-10", "07:30:00", False),  # 终点不含
        ("23:00", "07:30", "2026-11-10", "12:00:00", False),
        ("23:00", "07:30", "2026-11-10", "22:59:59", False),
        # 同日窗口 10:00-12:00
        ("10:00", "12:00", "2026-11-09", "09:59:59", False),
        ("10:00", "12:00", "2026-11-09", "10:00:00", True),
        ("10:00", "12:00", "2026-11-09", "11:59:59", True),
        ("10:00", "12:00", "2026-11-09", "12:00:00", False),
        # 零长度窗口恒不生效
        ("23:00", "23:00", "2026-11-09", "23:30:00", False),
    ],
)
def test_dnd_window_bounds_geometry(
    start: str, end: str, day: str, hhmmss: str, active: bool
) -> None:
    cfg = dnd_config(start=start, end=end)
    moment = at(day, hhmmss)
    window = dnd_window_bounds(cfg, moment)
    assert (window is not None) == active
    if window is not None:
        begin, close = window
        assert begin < close
        assert begin <= moment.astimezone(UTC) < close


def test_dnd_window_bounds_cross_midnight_wraps_yesterday() -> None:
    cfg = dnd_config()  # 23:00-07:30
    begin, close = dnd_window_bounds(cfg, at("2026-11-10", "02:00:00"))  # 凌晨段
    # 窗口 = 前一晚（11-09）23:00 北京 ~ 当日（11-10）07:30 北京 → UTC 15:00 / 23:30
    assert utc_str(begin) == "2026-11-09T15:00:00.000Z"
    assert utc_str(close) == "2026-11-09T23:30:00.000Z"


def test_dnd_window_bounds_respects_enabled_flag() -> None:
    assert dnd_window_bounds(dnd_config(enabled=False), at("2026-11-10", "02:00:00")) is None


# ---- 三态路由 ----


def _center_with_dnd(db: Database, dnd: DndConfig) -> tuple[RecordingNotifier, RecordingNotifier]:
    config = Config.model_validate({"notify": {"dnd": dnd.model_dump()}})
    feishu = RecordingNotifier({"high", "medium", "low"}, channel_name="feishu")
    email = RecordingNotifier({"high"}, channel_name="email")
    make_center(db, config, feishu, email)
    return feishu, email


def test_state_1_outside_window_delivers_immediately(db: Database) -> None:
    """状态一：免打扰未启用/窗口外 → 即时投递（无 dedup 痕迹）。"""
    feishu, email = _center_with_dnd(db, dnd_config())
    record = _emit(db, "KEY_QUESTION", at("2026-11-09", "15:00:00"))
    assert record.event_type == "KEY_QUESTION"
    assert [r.event_type for r in feishu.sent] == ["KEY_QUESTION"]
    assert [r.event_type for r in email.sent] == []  # medium 不进邮件
    assert _dedup_rows(db) == {}


def test_state_2_high_bypasses_window(db: Database) -> None:
    """状态二：窗口内 + high + high_bypass → 不受限即时投递 [B11]。"""
    feishu, email = _center_with_dnd(db, dnd_config())
    _emit(db, "RISK_PAUSE", at("2026-11-09", "23:30:00"))
    assert [r.event_type for r in feishu.sent] == ["RISK_PAUSE"]
    assert [r.event_type for r in email.sent] == ["RISK_PAUSE"]
    assert _dedup_rows(db) == {}


def test_state_3_medium_low_deferred_with_dedup_count(db: Database) -> None:
    """状态三：窗口内 + 中/低 → 延后合并（notify_dedup 计数；通知中心仍实时）。"""
    feishu, email = _center_with_dnd(db, dnd_config())
    night = at("2026-11-09", "23:10:00")
    _emit(db, "KEY_QUESTION", night)
    _emit(db, "KEY_QUESTION", night + timedelta(minutes=5))
    _emit(db, "DAILY_DIGEST", night + timedelta(minutes=6))

    assert feishu.sent == [] and email.sent == []  # 不即时投递
    assert _dedup_rows(db) == {
        "KEY_QUESTION": 2,
        "DAILY_DIGEST": 1,
    }
    # 通知中心实时性：事件在免打扰期间已全量可查 [§11 "通知中心始终实时"]
    rows = events_in(db)
    assert [r[0] for r in rows] == ["KEY_QUESTION", "KEY_QUESTION", "DAILY_DIGEST"]
    assert json.loads(rows[0][3]) == {"dnd": "deferred", "count": 1}
    assert json.loads(rows[1][3]) == {"dnd": "deferred", "count": 2}


def test_high_deferred_when_bypass_disabled(db: Database) -> None:
    """high_bypass=false：high 也受限（"可配"的关态）→ 延后。"""
    feishu, email = _center_with_dnd(db, dnd_config(high_bypass=False))
    _emit(db, "LOGIN_EXPIRED", at("2026-11-09", "23:30:00"))
    assert feishu.sent == [] and email.sent == []
    assert _dedup_rows(db) == {"LOGIN_EXPIRED": 1}


def test_deferred_window_start_is_stable_within_one_window(db: Database) -> None:
    """同一免打扰窗口内 window_start 不变；跨窗口重新计数。"""
    _center_with_dnd(db, dnd_config())
    _emit(db, "KEY_QUESTION", at("2026-11-09", "23:10:00"))
    _emit(db, "KEY_QUESTION", at("2026-11-10", "06:50:00"))  # 同一窗口（11-09 23:00~11-10 07:30）
    rows = db.query("SELECT window_start, count FROM notify_dedup")
    assert len(rows) == 1
    assert int(rows[0]["count"]) == 2
    assert str(rows[0]["window_start"]) == "2026-11-09T15:00:00.000Z"  # 11-09 23:00 北京

    _emit(db, "KEY_QUESTION", at("2026-11-10", "23:05:00"))  # 下一晚的新窗口
    row = db.query_one("SELECT window_start, count FROM notify_dedup")
    assert int(row["count"]) == 1  # 新窗口从 1 重新计数
    assert str(row["window_start"]) == "2026-11-10T15:00:00.000Z"  # 11-10 23:00 北京


def test_disabled_dnd_never_defers(db: Database) -> None:
    feishu, _ = _center_with_dnd(db, dnd_config(enabled=False))
    _emit(db, "DAILY_DIGEST", at("2026-11-09", "23:10:00"))  # 深夜也不受限
    assert [r.event_type for r in feishu.sent] == ["DAILY_DIGEST"]


# ---- 内部：经注册管线的发射助手 ----


def _emit(db: Database, event_type: str, now: datetime):
    from job_agent.notify import EventRecord, center_for

    center = center_for(db)
    assert center is not None, "夹具应已注册通知中心"
    record = center.emit(event_type, {}, now=now)
    assert isinstance(record, EventRecord)
    return record


def _dedup_rows(db: Database) -> dict[str, int]:
    return {str(r["event_type"]): int(r["count"]) for r in db.query("SELECT * FROM notify_dedup")}
