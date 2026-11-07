"""S3d 对接点验证：core/events.py（S2d 占位协议签名）→ notify 管线转发。

既有调用方（core/session、core/rampup、core/dryrun 及其 267 用例）不改一行，
事件发射统一进入 notify 管线：
- 未注册中心 → 仅落库（占位协议行为，逐字节兼容）；
- 注册中心 → 完整管线（级别路由/免打扰/渠道投递），落库语义不变。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from job_agent.config import Config
from job_agent.core import dryrun, events, rampup
from job_agent.core.events import EventRecord
from job_agent.db import Database
from job_agent.models import RiskSignal

from .conftest import RecordingNotifier, events_in, make_center


def test_bare_emit_without_center_matches_placeholder_semantics(db: Database) -> None:
    """未注册中心：emit 签名/返回值/落库与 S2d 占位协议完全一致。"""
    record = events.emit(db, "RAMPUP_DONE", {"source": "auto_recover"})
    assert isinstance(record, EventRecord)
    assert record.event_type == "RAMPUP_DONE"
    assert record.level == "low"  # 裁决后的权威级别
    assert record.id is not None
    row = events_in(db, "RAMPUP_DONE")[0]
    assert row[1] == "low"
    assert json.loads(row[2]) == {"source": "auto_recover"}
    assert row[3] == ""  # delivered_json 保持 NULL（无路由）

    # level_override 同构
    assert events.emit(db, "DAILY_DIGEST", {}, "high").level == "high"

    # 未知类型拒绝
    with pytest.raises(ValueError, match="未知事件类型"):
        events.emit(db, "NOPE", {})


def test_forwarded_emit_when_center_registered(db: Database, config: Config) -> None:
    """注册中心后：同一 events.emit 调用走完整管线，落库恰一行。"""
    feishu = RecordingNotifier({"high", "medium"}, channel_name="feishu")
    email = RecordingNotifier({"high"}, channel_name="email")
    center = make_center(db, config, feishu, email)
    try:
        record = events.emit(db, "CONV_LIMIT_REACHED", {"conv_id": "C1", "auto_sent_count": 2})
        assert record.event_type == "CONV_LIMIT_REACHED"
        assert record.level == "medium"
        assert [r.event_type for r in feishu.sent] == ["CONV_LIMIT_REACHED"]  # 管线投递
        assert email.sent == []
        rows = events_in(db, "CONV_LIMIT_REACHED")
        assert len(rows) == 1  # 落库语义不变：恰好一行
        assert json.loads(rows[0][3]) == {"feishu": "ok", "email": "skip:level"}
    finally:
        center.close()


def test_close_restores_bare_behavior(db: Database, config: Config) -> None:
    feishu = RecordingNotifier({"high"}, channel_name="feishu")
    center = make_center(db, config, feishu)
    events.emit(db, "LOGIN_EXPIRED", {})
    assert len(feishu.sent) == 1
    center.close()
    events.emit(db, "BROWSER_LOST", {})
    assert len(feishu.sent) == 1  # 注销后不再投递
    assert len(events_in(db)) == 2  # 但仍落库


def test_real_core_callers_flow_through_pipeline(db: Database, config: Config) -> None:
    """既有 core 调用方零改动接入：session 计数告警 + rampup 风控事件均进管线。"""
    feishu = RecordingNotifier({"high", "medium"}, channel_name="feishu")
    center = make_center(db, config, feishu)
    try:
        # S2d session.record_auto_send 的 CONV_LIMIT_REACHED（medium）
        from job_agent.core.session import ConversationState

        real = Config.model_validate({"delivery": {"dry_run": False}})
        state = ConversationState(db, real)
        state.record_auto_send("C1", "message")
        state.record_auto_send("C1", "message")  # 达限 → 转人工 + 事件
        # S2d rampup.handle_risk_signal 的 PAGE_EVICTED（high）
        at = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
        rampup.handle_risk_signal(db, config, RiskSignal.EVICTED, at)
        assert rampup.is_risk_paused(db) is True

        assert [(r.event_type, r.level) for r in feishu.sent] == [
            ("CONV_LIMIT_REACHED", "medium"),
            ("PAGE_EVICTED", "high"),
        ]
        assert len(events_in(db, "CONV_LIMIT_REACHED")) == 1
        assert len(events_in(db, "PAGE_EVICTED")) == 1
        assert db.query_one("SELECT COUNT(*) AS n FROM notify_dedup")["n"] == 0
    finally:
        center.close()


def test_dryrun_module_unaffected(db: Database, config: Config) -> None:
    """core/dryrun 不发射事件（拟发送隔离）——对接后行为不变。"""
    center = make_center(db, config, RecordingNotifier({"high", "medium", "low"}))
    try:
        dryrun.set_dry_run(db, config, False)
        dryrun.record_planned_send(db, "greeting", "J1", "digest")
        assert events_in(db) == []
    finally:
        center.close()
