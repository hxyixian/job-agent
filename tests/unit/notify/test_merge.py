"""S3d 免打扰合并推送 [B11] + 待发箱超时合并单条 [s6]。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from job_agent.config import Config
from job_agent.db import Database
from job_agent.notify import EventType, center_for, scan_outbox_timeout
from job_agent.timeutil import CST, utc_str

from .conftest import RecordingNotifier, events_in, make_center

BJ = CST
DND = {"enabled": True, "start": "23:00", "end": "07:30", "high_bypass": True}


def at(day: str, hhmmss: str) -> datetime:
    d = datetime.strptime(day, "%Y-%m-%d")
    t = datetime.strptime(hhmmss, "%H:%M:%S")
    return datetime.combine(d.date(), t.time(), tzinfo=BJ)


def dnd_center(
    db: Database, feishu_levels: set[str]
) -> tuple[RecordingNotifier, RecordingNotifier]:
    config = Config.model_validate(
        {
            "notify": {
                "dnd": DND,
                "feishu": {
                    "webhook": "http://127.0.0.1/x",
                    "levels": sorted(feishu_levels),
                },
            }
        }
    )
    feishu = RecordingNotifier(feishu_levels, channel_name="feishu")
    email = RecordingNotifier({"high"}, channel_name="email")
    make_center(db, config, feishu, email)
    return feishu, email


# ---- 免打扰合并推送（flush_deferred） ----


def test_flush_inside_window_is_noop(db: Database) -> None:
    feishu, _ = dnd_center(db, {"high", "medium", "low"})
    center = center_for(db)
    assert center is not None
    center.emit("KEY_QUESTION", {}, now=at("2026-11-09", "23:10:00"))
    assert center.flush_deferred(now=at("2026-11-09", "23:15:00")) == []  # 仍在窗口内
    assert feishu.sent == []
    assert db.query_one("SELECT COUNT(*) AS n FROM notify_dedup")["n"] == 1  # 计数保留


def test_flush_after_window_merges_per_event_type(db: Database) -> None:
    """窗口结束 → 每个 event_type 恰一条合并消息（payload 带 count），dedup 清空。"""
    feishu, email = dnd_center(db, {"high", "medium", "low"})
    center = center_for(db)
    assert center is not None
    night = at("2026-11-09", "23:10:00")
    for _ in range(3):
        center.emit("KEY_QUESTION", {"q": "薪资"}, now=night)
    center.emit("CONTACT_REQUEST", {"conv": "C1"}, now=night)

    pushed = center.flush_deferred(now=at("2026-11-10", "08:00:00"))
    # 两条合并消息（per event_type），均只推一条
    assert [(p["event_type"], p["level"], p["count"]) for p in pushed] == [
        ("CONTACT_REQUEST", "medium", 1),
        ("KEY_QUESTION", "medium", 3),
    ]
    feishu_records = feishu.sent
    assert [r.event_type for r in feishu_records] == ["CONTACT_REQUEST", "KEY_QUESTION"]
    for record, expected_count in zip(feishu_records, (1, 3), strict=True):
        assert record.id is None  # 合并推送不新增落库行
        assert record.payload["merged"] is True
        assert record.payload["count"] == expected_count
        assert "×" in record.payload["message"]
    # medium 合并消息不进邮件（级别过滤仍然生效）
    assert email.sent == []
    # dedup 清空；notification_events 行数不变（仍是 4 条原始事件）
    assert db.query_one("SELECT COUNT(*) AS n FROM notify_dedup")["n"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM notification_events")["n"] == 4


def test_flush_respects_channel_level_filter(db: Database) -> None:
    """low 不在 feishu levels → 合并推送对该渠道 skip:level（邮件也不收 low）。"""
    feishu, email = dnd_center(db, {"high", "medium"})
    center = center_for(db)
    assert center is not None
    center.emit("DAILY_DIGEST", {}, now=at("2026-11-09", "23:10:00"))
    pushed = center.flush_deferred(now=at("2026-11-10", "08:00:00"))
    assert len(pushed) == 1
    assert pushed[0]["delivered"] == {"feishu": "skip:level", "email": "skip:level"}
    assert feishu.sent == [] and email.sent == []


def test_flush_idempotent_and_empty(db: Database) -> None:
    feishu, _ = dnd_center(db, {"high", "medium", "low"})
    center = center_for(db)
    assert center is not None
    assert center.flush_deferred(now=at("2026-11-10", "08:00:00")) == []  # 空表
    center.emit("KEY_QUESTION", {}, now=at("2026-11-09", "23:10:00"))
    first = center.flush_deferred(now=at("2026-11-10", "08:00:00"))
    assert len(first) == 1
    assert center.flush_deferred(now=at("2026-11-10", "08:05:00")) == []  # 已清空
    assert len(feishu.sent) == 1


def test_flush_deferred_high_when_bypass_disabled(db: Database) -> None:
    """high_bypass=false 延后的 high：合并推送按 high 路由（邮件也收）。"""
    config = Config.model_validate({"notify": {"dnd": {**DND, "high_bypass": False}}})
    feishu = RecordingNotifier({"high"}, channel_name="feishu")
    email = RecordingNotifier({"high"}, channel_name="email")
    make_center(db, config, feishu, email)
    center = center_for(db)
    assert center is not None
    center.emit("LOGIN_EXPIRED", {}, now=at("2026-11-09", "23:10:00"))
    assert feishu.sent == [] and email.sent == []
    pushed = center.flush_deferred(now=at("2026-11-10", "08:00:00"))
    assert [(p["event_type"], p["level"], p["count"]) for p in pushed] == [
        ("LOGIN_EXPIRED", "high", 1)
    ]
    assert [r.event_type for r in email.sent] == ["LOGIN_EXPIRED"]  # high 进邮件


def test_flush_clears_unknown_dedup_rows_with_warning(db: Database) -> None:
    """手工改库混入的未知 event_type：清行跳过，不阻塞其余合并推送。"""
    dnd_center(db, {"high", "medium", "low"})
    center = center_for(db)
    assert center is not None
    center.emit("KEY_QUESTION", {}, now=at("2026-11-09", "23:10:00"))
    db.execute(
        "INSERT INTO notify_dedup (event_type, window_start, count) VALUES ('bogus', 'x', 5)"
    )
    pushed = center.flush_deferred(now=at("2026-11-10", "08:00:00"))
    assert [p["event_type"] for p in pushed] == ["KEY_QUESTION"]
    assert db.query_one("SELECT COUNT(*) AS n FROM notify_dedup")["n"] == 0


def test_no_dnd_config_never_defers_or_flushes(db: Database, config: Config) -> None:
    """免打扰关闭：emit 即时投递，flush 恒空（无延后语义）。"""
    feishu = RecordingNotifier({"high", "medium", "low"}, channel_name="feishu")
    center = make_center(db, config, feishu)
    try:
        center.emit("KEY_QUESTION", {}, now=at("2026-11-09", "23:10:00"))
        assert len(feishu.sent) == 1
        assert center.flush_deferred() == []
    finally:
        center.close()


# ---- 待发箱超时合并 [s6/§8.3/FR6.4] ----


def seed_outbox(
    db: Database, conv_id: str, *, state: str, created_at: datetime, reminded: bool = False
) -> None:
    db.execute(
        "INSERT INTO conversations (conv_id, peer_name) VALUES (?, 'HR')",
        (conv_id,),
    )
    db.execute(
        "INSERT INTO outbox (conv_id, draft_text, source, state, created_at, reminded_at)"
        " VALUES (?, '草稿', 'ai', ?, ?, ?)",
        (conv_id, state, utc_str(created_at), utc_str(created_at) if reminded else None),
    )


def test_outbox_scan_no_stale_returns_none(db: Database, config: Config) -> None:
    now = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
    seed_outbox(db, "C1", state="pending", created_at=now - timedelta(minutes=10))  # 未超时
    seed_outbox(db, "C2", state="skipped", created_at=now - timedelta(hours=3))  # 已处理
    assert scan_outbox_timeout(db, config, now=now) is None


def test_outbox_scan_emits_single_merged_high_event(db: Database) -> None:
    config = Config.model_validate({"notify": {"dnd": DND}})
    feishu = RecordingNotifier({"high", "medium"}, channel_name="feishu")
    email = RecordingNotifier({"high"}, channel_name="email")
    make_center(db, config, feishu, email)

    now = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)  # 北京 14:00（窗口外）
    timeout = config.messaging.outbox_timeout_min  # 默认 60
    seed_outbox(db, "C1", state="pending", created_at=now - timedelta(minutes=timeout + 5))
    seed_outbox(db, "C2", state="stale", created_at=now - timedelta(minutes=timeout + 1))
    seed_outbox(db, "C3", state="pending", created_at=now - timedelta(minutes=timeout - 5))  # 未到
    seed_outbox(
        db, "C4", state="pending", created_at=now - timedelta(hours=5), reminded=True
    )  # 已提醒过
    seed_outbox(db, "C5", state="approved", created_at=now - timedelta(hours=5))  # 已确认
    seed_outbox(db, "C6", state="expired", created_at=now - timedelta(hours=5))  # 已处理

    record = scan_outbox_timeout(db, config, now=now)
    assert record is not None
    assert record.event_type == "OUTBOX_TIMEOUT"
    assert record.level == "high"  # §11：待发箱超时（合并一条）= high
    payload = record.payload
    assert payload["count"] == 2  # 只有 C1/C2（pending/stale 且未提醒且超时）
    assert {item["conv_id"] for item in payload["items"]} == {"C1", "C2"}
    assert payload["timeout_min"] == timeout

    # 事件已落库 + 双渠道即时投递（窗口外 high）
    assert len(events_in(db, "OUTBOX_TIMEOUT")) == 1
    assert [r.event_type for r in feishu.sent] == ["OUTBOX_TIMEOUT"]
    assert [r.event_type for r in email.sent] == ["OUTBOX_TIMEOUT"]

    # 每条目只提醒一次：reminded_at 已置位，二次扫描无新事件
    reminded = db.query("SELECT conv_id FROM outbox WHERE reminded_at IS NOT NULL ORDER BY conv_id")
    assert [str(r["conv_id"]) for r in reminded] == ["C1", "C2", "C4"]
    assert scan_outbox_timeout(db, config, now=now) is None
    assert db.query_one("SELECT COUNT(*) AS n FROM notification_events")["n"] == 1


def test_outbox_scan_defers_under_dnd(db: Database) -> None:
    """深夜扫描：OUTBOX_TIMEOUT 受免打扰约束（high_bypass=True → 仍即时投递；
    bypass=False → 延后计数，走与普通事件一致的合并机制）。"""
    config = Config.model_validate({"notify": {"dnd": {**DND, "high_bypass": False}}})
    feishu = RecordingNotifier({"high"}, channel_name="feishu")
    make_center(db, config, feishu)
    night = at("2026-11-09", "23:30:00")
    seed_outbox(db, "C1", state="pending", created_at=night - timedelta(minutes=90))
    record = scan_outbox_timeout(db, config, now=night)
    assert record is not None and record.event_type == "OUTBOX_TIMEOUT"
    assert feishu.sent == []  # 延后
    row = db.query_one("SELECT count FROM notify_dedup WHERE event_type = 'OUTBOX_TIMEOUT'")
    assert int(row["count"]) == 1
    delivered = json.loads(events_in(db, "OUTBOX_TIMEOUT")[0][3])
    assert delivered == {"dnd": "deferred", "count": 1}


def test_outbox_scan_without_center_still_stores(db: Database, config: Config) -> None:
    """未注册通知中心（裸调用）：仅落库——语义与 core.events.emit 占位路径一致。"""
    now = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
    seed_outbox(db, "C1", state="pending", created_at=now - timedelta(hours=2))
    record = scan_outbox_timeout(db, config, now=now)
    assert record is not None
    assert record.event_type == str(EventType.OUTBOX_TIMEOUT)
    assert len(events_in(db, "OUTBOX_TIMEOUT")) == 1
