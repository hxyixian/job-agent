"""S3d notify/center：全量落库 + 级别路由 + delivered_json + 通知中心查询。"""

from __future__ import annotations

import json

import pytest

from job_agent.config import Config
from job_agent.db import Database
from job_agent.notify import (
    EventType,
    NotificationCenter,
    center_for,
    default_notifiers,
    list_events,
    store_event,
    unregister_center,
)
from job_agent.notify.email import EmailNotifier
from job_agent.notify.feishu import FeishuNotifier

from .conftest import RecordingNotifier, events_in, make_center


@pytest.fixture()
def center(db: Database, config: Config) -> NotificationCenter:
    """默认路由配置：feishu=[high,medium]，email=[high]（录制型假渠道）。"""
    feishu = RecordingNotifier({"high", "medium"}, channel_name="feishu")
    email = RecordingNotifier({"high"}, channel_name="email")
    instance = make_center(db, config, feishu, email)
    yield instance
    instance.close()


# ---- ① 全量落库（含级别与覆盖） ----


def test_emit_stores_every_event_and_returns_record(
    db: Database, center: NotificationCenter
) -> None:
    record = center.emit("LOGIN_EXPIRED", {"reason": "patrol"}, now=None)
    assert record.event_type == "LOGIN_EXPIRED"
    assert record.level == "high"
    assert record.payload == {"reason": "patrol"}
    assert record.id is not None and record.id > 0
    assert record.created_at.endswith("Z")

    center.emit(EventType.RAMPUP_DONE, {"days": 3})  # 枚举成员同构
    center.emit("DAILY_DIGEST", {}, level_override="high")  # 覆盖路由级别

    rows = events_in(db)
    assert [r[0] for r in rows] == ["LOGIN_EXPIRED", "RAMPUP_DONE", "DAILY_DIGEST"]
    assert [r[1] for r in rows] == ["high", "low", "high"]  # 覆盖改变落库级别
    assert json.loads(rows[2][2]) == {}


def test_emit_unknown_event_rejected_no_row(db: Database, center: NotificationCenter) -> None:
    with pytest.raises(ValueError, match="未知事件类型"):
        center.emit("NOT_A_EVENT", {})
    with pytest.raises(ValueError, match="未知事件类型"):
        store_event(db, "NOT_A_EVENT", {}, "high")  # 带 override 也先校验
    assert db.query_one("SELECT COUNT(*) AS n FROM notification_events")["n"] == 0


def test_store_event_payload_serialization_matches_placeholder(db: Database) -> None:
    """payload 序列化与 S2d 占位协议逐字节同构（ensure_ascii=False + sort_keys）。"""
    store_event(db, "OFFER_SIGNAL", {"z": 1, "a": "中文", "m": [3, 2]})
    raw = events_in(db, "OFFER_SIGNAL")[0][2]
    assert raw == '{"a": "中文", "m": [3, 2], "z": 1}'
    assert events_in(db, "OFFER_SIGNAL")[0][3] == ""  # delivered_json 未路由时保持 NULL


# ---- ②④ 级别路由与渠道状态 ----


def test_level_routing_by_channel_filters(db: Database, center: NotificationCenter) -> None:
    feishu, email = center.notifiers  # type: ignore[assignment]
    center.emit("RISK_PAUSE", {"signal": "CAPTCHA"})  # high → 双渠道
    center.emit("KEY_QUESTION", {"q": "薪资"})  # medium → 仅 feishu
    center.emit("DAILY_DIGEST", {"n": 5})  # low → 都不投

    assert [r.event_type for r in feishu.sent] == ["RISK_PAUSE", "KEY_QUESTION"]
    assert [r.event_type for r in email.sent] == ["RISK_PAUSE"]

    statuses = [json.loads(r[3]) for r in events_in(db)]
    assert statuses[0] == {"feishu": "ok", "email": "ok"}
    assert statuses[1] == {"feishu": "ok", "email": "skip:level"}
    assert statuses[2] == {"feishu": "skip:level", "email": "skip:level"}


def test_level_override_changes_routing(db: Database, center: NotificationCenter) -> None:
    feishu, email = center.notifiers  # type: ignore[assignment]
    center.emit("DAILY_DIGEST", {}, level_override="high")  # low 事件提级 → 邮件也收
    assert [r.event_type for r in email.sent] == ["DAILY_DIGEST"]
    assert [r.level for r in email.sent] == ["high"]
    assert [r.level for r in feishu.sent] == ["high"]


def test_channel_failure_recorded_not_raised(db: Database, config: Config) -> None:
    bad = RecordingNotifier({"high"}, channel_name="bad", error_on=frozenset({"RISK_PAUSE"}))
    ok = RecordingNotifier({"high"}, channel_name="ok")
    center = make_center(db, config, bad, ok)
    try:
        record = center.emit("RISK_PAUSE", {})  # 不向上抛
        assert record.event_type == "RISK_PAUSE"
    finally:
        center.close()
    status = json.loads(events_in(db, "RISK_PAUSE")[0][3])
    assert status == {"bad": "error:RuntimeError", "ok": "ok"}
    assert len(ok.sent) == 1  # 失败渠道不影响其他渠道


def test_unconfigured_channel_skipped(db: Database, config: Config) -> None:
    absent = RecordingNotifier({"high"}, channel_name="absent", configured=False)
    center = make_center(db, config, absent)
    try:
        center.emit("LOGIN_EXPIRED", {})
    finally:
        center.close()
    assert absent.sent == []  # configured=False 不调用 send
    status = json.loads(events_in(db, "LOGIN_EXPIRED")[0][3])
    assert status == {"absent": "skip:not_configured"}


def test_default_notifiers_assembly(config: Config) -> None:
    cfg = Config.model_validate(
        {"notify": {"feishu": {"webhook": "https://example.invalid/hook", "secret": "s"}}}
    )
    notifiers = default_notifiers(cfg)
    assert [n.name for n in notifiers] == ["feishu", "email"]
    feishu = notifiers[0]
    assert isinstance(feishu, FeishuNotifier)
    assert feishu.applies("high") and feishu.applies("medium")
    assert not feishu.applies("low")  # 出厂 levels=[high, medium]
    email = notifiers[1]
    assert isinstance(email, EmailNotifier)
    assert email.applies("high") and not email.applies("medium")
    assert email.configured is False  # SMTP 未配置 → skip:not_configured


# ---- 通知中心查询（§10.2 GET /notifications 数据源；始终实时） ----


def test_query_events_filter_order_and_pagination(db: Database, center: NotificationCenter) -> None:
    for i in range(5):
        center.emit("KEY_QUESTION", {"i": i})
        center.emit("DAILY_DIGEST", {"i": i})

    all_rows = list_events(db)
    assert len(all_rows) == 10
    assert [r["event_type"] for r in all_rows][:2] == ["DAILY_DIGEST", "KEY_QUESTION"]  # id 倒序

    only_low = list_events(db, level="low")
    assert {r["event_type"] for r in only_low} == {"DAILY_DIGEST"}
    assert len(list_events(db, event_type="KEY_QUESTION")) == 5
    page = list_events(db, level="medium", limit=2, offset=1)
    assert [json.loads(r["payload_json"])["i"] for r in page] == [3, 2]

    via_center = center.query_events(level="low", limit=3)
    assert len(via_center) == 3
    assert via_center[0]["level"] == "low"


def test_center_registration_scoped_to_db_instance(db: Database, config: Config) -> None:
    """center_for 按 db 实例解析：未注册/已注销 → None；构造即注册。"""
    assert center_for(db) is None
    center = make_center(db, config)
    try:
        assert center_for(db) is center
    finally:
        center.close()
    assert center_for(db) is None

    # 手动 register/unregister（装配方重建 center 的等价路径）
    unregister_center(db)  # 幂等
    assert center_for(db) is None


def test_delivered_json_row_updated_in_place(db: Database, center: NotificationCenter) -> None:
    center.emit("RISK_PAUSE", {})
    row = db.query_one(
        "SELECT delivered_json FROM notification_events WHERE event_type = 'RISK_PAUSE'"
    )
    assert json.loads(str(row["delivered_json"])) == {"feishu": "ok", "email": "ok"}
