"""S2d 会话状态单元测试（§5.5 I8；A-CORE-6/19 与 F1/N2 逻辑层）。

全部离线：临时目录建库（绝不写 data/agent.db）。事件断言直接查
notification_events（core/events 占位协议的落库侧）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from job_agent.config import Config
from job_agent.core import dryrun
from job_agent.core.session import BaselineEntry, ConversationState
from job_agent.db import Database, open_database


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = open_database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture()
def state(db: Database) -> ConversationState:
    # 正式模式（dry_run=False）：计数/转人工路径生效；试运行口径单独建状态测
    config = Config.model_validate({"delivery": {"dry_run": False}})
    return ConversationState(db, config)


def seed_conv(
    db: Database,
    conv_id: str,
    *,
    auto: int = 0,
    resume: int = 0,
    takeover: int = 0,
    baseline: int = 0,
    peer_name: str = "HR甲",
) -> None:
    db.execute(
        "INSERT INTO conversations (conv_id, peer_name, is_baseline, manual_takeover,"
        " auto_sent_count, resume_sent_count) VALUES (?, ?, ?, ?, ?, ?)",
        (conv_id, peer_name, baseline, takeover, auto, resume),
    )


def conv_row(db: Database, conv_id: str) -> Any:
    return db.query_one("SELECT * FROM conversations WHERE conv_id = ?", (conv_id,))


def events_of(db: Database, event_type: str) -> list[tuple[str, dict[str, Any]]]:
    rows = db.query(
        "SELECT level, payload_json FROM notification_events WHERE event_type = ? ORDER BY id",
        (event_type,),
    )
    return [(str(r["level"]), json.loads(r["payload_json"])) for r in rows]


# ---- can_auto_send [I8] ----


def test_can_auto_send_message_within_and_at_limit(db: Database, state: ConversationState) -> None:
    # 无会话行的新会话不受限
    assert state.can_auto_send("C-new", "message") == (True, "ok")
    seed_conv(db, "C1", auto=1)  # 默认 auto_reply_limit=2
    assert state.can_auto_send("C1", "message") == (True, "ok")
    seed_conv(db, "C2", auto=2)
    assert state.can_auto_send("C2", "message") == (False, "auto_reply_limit_reached")


def test_can_auto_send_resume_independent_limit(state: ConversationState) -> None:
    seed_conv(state._db, "C1", resume=1)  # A2：简历每会话 ≤1
    assert state.can_auto_send("C1", "resume") == (False, "resume_limit_reached")
    # 消息额度与简历额度互相独立（简历不占消息额度）
    assert state.can_auto_send("C1", "message") == (True, "ok")


def test_can_auto_send_blocked_by_manual_takeover(state: ConversationState) -> None:
    seed_conv(state._db, "C1", takeover=1)
    assert state.can_auto_send("C1", "message") == (False, "manual_takeover")
    assert state.can_auto_send("C1", "resume") == (False, "manual_takeover")


def test_invalid_kind_rejected(state: ConversationState) -> None:
    with pytest.raises(ValueError, match=r"message\|resume"):
        state.can_auto_send("C1", "greeting")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"message\|resume"):
        state.record_auto_send("C1", "greeting")  # type: ignore[arg-type]


# ---- record_auto_send：计数口径与达限转人工 [A2/§8.1/A-CORE-6] ----


def test_record_message_counts_and_takeover_on_limit(
    db: Database, state: ConversationState
) -> None:
    # 两条消息（第 1 条可视为招呼语——口径：含招呼语不含简历）
    state.record_auto_send("C1", "message")
    assert conv_row(db, "C1")["auto_sent_count"] == 1
    assert conv_row(db, "C1")["manual_takeover"] == 0
    assert events_of(db, "CONV_LIMIT_REACHED") == []

    state.record_auto_send("C1", "message")  # 达 auto_reply_limit=2 → 转人工 + 中级告警
    row = conv_row(db, "C1")
    assert row["auto_sent_count"] == 2
    assert row["manual_takeover"] == 1
    assert row["blocked_reason"] == "auto_reply_limit_reached"
    assert state.can_auto_send("C1", "message") == (False, "manual_takeover")

    events = events_of(db, "CONV_LIMIT_REACHED")
    assert len(events) == 1
    level, payload = events[0]
    assert level == "medium"
    assert payload["conv_id"] == "C1"
    assert payload["auto_sent_count"] == 2
    assert payload["auto_reply_limit"] == 2


def test_record_message_limit_one_variant(db: Database) -> None:
    # auto_reply_limit 下限 1（§4 校验）：首条消息即达限转人工
    cfg = Config.model_validate(
        {"messaging": {"auto_reply_limit": 1}, "delivery": {"dry_run": False}}
    )
    st = ConversationState(db, cfg)
    st.record_auto_send("C1", "message")
    row = conv_row(db, "C1")
    assert row["auto_sent_count"] == 1
    assert row["manual_takeover"] == 1
    assert len(events_of(db, "CONV_LIMIT_REACHED")) == 1


def test_record_resume_independent_no_limit_event(db: Database, state: ConversationState) -> None:
    state.record_auto_send("C1", "resume")
    row = conv_row(db, "C1")
    assert row["resume_sent_count"] == 1
    assert row["auto_sent_count"] == 0  # 简历不计入消息额度 [A2]
    assert row["manual_takeover"] == 0  # 简历上限不触发转人工
    assert events_of(db, "CONV_LIMIT_REACHED") == []
    assert state.can_auto_send("C1", "resume") == (False, "resume_limit_reached")
    assert state.can_auto_send("C1", "message") == (True, "ok")


def test_record_creates_row_and_keeps_flags(db: Database, state: ConversationState) -> None:
    seed_conv(db, "C1", baseline=1, auto=0)
    state.record_auto_send("C1", "message")
    row = conv_row(db, "C1")
    assert row["auto_sent_count"] == 1
    assert row["is_baseline"] == 1  # 既有标志不被清掉

    state.record_auto_send("C-unknown", "message")  # 无行会话：建行（peer 占位空串）
    row = conv_row(db, "C-unknown")
    assert row is not None
    assert row["auto_sent_count"] == 1
    assert row["peer_name"] == ""


def test_record_auto_send_dry_run_noop(db: Database) -> None:
    """[N2 重点] 试运行模式下不进 FR6.3 会话自动发送计数（写侧隔离）。"""
    config = Config()  # 出厂默认 dry_run=True
    assert dryrun.is_dry_run(db, config) is True
    st = ConversationState(db, config)
    st.record_auto_send("C1", "message")
    st.record_auto_send("C1", "message")
    st.record_auto_send("C1", "resume")
    row = conv_row(db, "C1")
    assert row is None  # 连会话行都不产生——拟发送走 dryrun.record_planned_send
    assert events_of(db, "CONV_LIMIT_REACHED") == []

    dryrun.set_dry_run(db, config, False)  # 切正式模式后计数恢复
    st.record_auto_send("C1", "message")
    assert conv_row(db, "C1")["auto_sent_count"] == 1


# ---- mark_manual_takeover [I8/B3/F1] ----


def test_mark_manual_takeover_sources(db: Database, state: ConversationState) -> None:
    state.mark_manual_takeover("C1", "digest_miss")
    row = conv_row(db, "C1")
    assert row["manual_takeover"] == 1
    assert row["blocked_reason"] == "manual_takeover:digest_miss"
    # 首因保留：再以 user_flag 置位不覆盖最早原因
    state.mark_manual_takeover("C1", "user_flag")
    assert conv_row(db, "C1")["blocked_reason"] == "manual_takeover:digest_miss"
    # 无行会话也可置位（对账发现的未知会话）
    state.mark_manual_takeover("C2", "user_flag")
    assert conv_row(db, "C2")["manual_takeover"] == 1


def test_mark_manual_takeover_invalid_source(state: ConversationState) -> None:
    with pytest.raises(ValueError, match=r"digest_miss\|user_flag"):
        state.mark_manual_takeover("C1", "greeting_mismatch")


# ---- 基线 [B3/A-CORE-19] ----


def test_baseline_snapshot_and_filter(db: Database, state: ConversationState) -> None:
    count = state.snapshot_baseline(
        [
            BaselineEntry("B1", "HR甲", "JOB-1"),
            BaselineEntry("B2", "HR乙", None),
            BaselineEntry("B3", "求职助手", None),  # VIP 系统会话同样入基线（§5.4）
        ]
    )
    assert count == 3
    assert all(conv_row(db, c)["is_baseline"] == 1 for c in ("B1", "B2", "B3"))

    # 基线会话被过滤，未入库的新会话原样通过，顺序保持
    assert state.baseline_filter(["B1", "NEW-1", "B3", "NEW-2"]) == ["NEW-1", "NEW-2"]
    assert state.baseline_filter([]) == []


def test_baseline_snapshot_preserves_existing_state(db: Database, state: ConversationState) -> None:
    seed_conv(db, "B1", auto=1, takeover=1, peer_name="HR甲")
    state.snapshot_baseline([BaselineEntry("B1", "HR甲-新", "JOB-9")])
    row = conv_row(db, "B1")
    assert row["is_baseline"] == 1
    assert row["auto_sent_count"] == 1  # 计数不清零
    assert row["manual_takeover"] == 1  # 接管状态不清零
    assert row["peer_name"] == "HR甲-新"  # 非空新名可更新
    assert row["job_id"] == "JOB-9"

    # 空名快照不覆盖已有真名（占位语义）
    state.snapshot_baseline([BaselineEntry("B1", "", None)])
    row = conv_row(db, "B1")
    assert row["peer_name"] == "HR甲-新"
    assert row["job_id"] == "JOB-9"  # 已有 job_id 不被 None 覆盖


# ---- 每日 greeting 核验标记 [F1/§I8] ----


def test_greeting_probe_daily_marker(db: Database, state: ConversationState) -> None:
    day1 = date(2026, 11, 8)
    assert state.is_greeting_probe_done(day1) is False
    state.mark_greeting_probe(day1)
    assert state.is_greeting_probe_done(day1) is True
    # 跨日失效：每天首次投递重新核验（FR5.3）
    assert state.is_greeting_probe_done(date(2026, 11, 9)) is False
    state.mark_greeting_probe(date(2026, 11, 9))
    assert state.is_greeting_probe_done(day1) is False  # 只保留最近一日
