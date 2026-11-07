"""S2a 领域模型 §5.1 逐项核对（字段、继承、默认值、枚举值 ↔ DB CHECK 对齐）。"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from typing import get_args, get_type_hints

from job_agent.models import (
    Conversation,
    JobDetail,
    JobRef,
    LoginState,
    Message,
    MsgType,
    RiskSignal,
    SendOutcome,
)

# 0001_init.sql 的 CHECK 枚举（入库直接写 .value，取值必须一致）
MSG_TYPE_DB_VALUES = {
    "TEXT",
    "IMAGE",
    "VOICE",
    "FILE",
    "JOB_CARD",
    "RESUME_REQUEST_CARD",
    "CONTACT_REQUEST_CARD",
    "INTERVIEW_CARD",
    "SYSTEM",
}
SEND_OUTCOME_DB_VALUES = {"pending", "sent", "failed", "unknown", "skipped_dryrun"}


def test_jobref_field_set_matches_5_1() -> None:
    names = [f.name for f in fields(JobRef)]
    assert names == [
        "job_id",
        "title",
        "company_name",
        "company_key",
        "salary_text",
        "tags",
        "list_snippet",
    ]
    hints = get_type_hints(JobRef)
    assert hints["salary_text"] == str | None  # 薪资仅展示位 [R13]，可为 None


def test_jobdetail_inherits_jobref_and_adds_fields() -> None:
    names = [f.name for f in fields(JobDetail)]
    assert issubclass(JobDetail, JobRef)
    assert names == [
        "job_id",
        "title",
        "company_name",
        "company_key",
        "salary_text",
        "tags",
        "list_snippet",
        "jd_text",
        "experience",
        "company_scale",
        "detail_url",
    ]


def test_jobref_salary_text_accepts_none_and_cipher_text() -> None:
    # R13：薪资原文可能是加密字形（如乱码/数字映射），仅展示记录
    ref = JobRef(
        job_id="J1",
        title="工程师",
        company_name="示例公司",
        company_key="c-1",
        salary_text="ﬞײַﬠﬡ",  # 加密字形占位示例
        tags=[],
        list_snippet="",
    )
    assert ref.salary_text is not None
    assert (
        JobRef(
            job_id="J2",
            title="工程师",
            company_name="示例公司",
            company_key="c-1",
            salary_text=None,
            tags=[],
            list_snippet="",
        ).salary_text
        is None
    )


def test_conversation_fields_and_optional_job_ref() -> None:
    hints = get_type_hints(Conversation)
    assert list(hints) == [
        "conv_id",
        "peer_name",
        "job_ref",
        "last_message_at",
        "unread",
    ]
    conv = Conversation(
        conv_id="C1",
        peer_name="张女士",
        job_ref=None,  # None=取不到关联岗位 [B8]
        last_message_at=datetime(2026, 11, 7, 9, 0, tzinfo=UTC),
        unread=2,
    )
    assert conv.job_ref is None
    assert conv.unread == 2


def test_msgtype_members_match_db_check() -> None:
    assert len(MsgType) == 9
    assert {member.value for member in MsgType} == MSG_TYPE_DB_VALUES
    # str 混入：与 DB 文本直接可比
    assert MsgType.RESUME_REQUEST_CARD == "RESUME_REQUEST_CARD"


def test_message_literal_fields_and_is_ours_auto_default() -> None:
    hints = get_type_hints(Message)
    assert get_args(hints["direction"]) == ("in", "out")
    assert get_args(hints["sender"]) == ("hr", "me", "system")
    assert hints["is_ours_auto"] == bool | None  # 核心层计算 [G6]，适配器不提供
    message = Message(
        platform_msg_id="M1",
        conv_id="C1",
        direction="out",
        sender="me",
        msg_type=MsgType.TEXT,
        text="您好",
        sent_at=datetime(2026, 11, 7, 9, 0, tzinfo=UTC),
    )
    assert message.is_ours_auto is None  # 匹配不上 → None → 按人工消息处理


def test_sendoutcome_values_match_db_check() -> None:
    assert len(SendOutcome) == 5
    assert {member.value for member in SendOutcome} == SEND_OUTCOME_DB_VALUES
    assert SendOutcome.SKIPPED_DRYRUN == "skipped_dryrun"  # 试运行拟发送 [G1]


def test_login_state_members() -> None:
    assert [member.name for member in LoginState] == ["LOGGED_IN", "LOGGED_OUT", "UNKNOWN"]


def test_risk_signal_members_match_5_2() -> None:
    assert [member.name for member in RiskSignal] == [
        "NONE",
        "CAPTCHA",
        "RISK_PAGE",
        "LOGIN_EXPIRED",
        "ADAPTER_BROKEN",
        "PLATFORM_LIMIT",
        "EVICTED",
        "SECURITY_CHECK",
    ]
