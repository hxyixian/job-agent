"""S4d messenger 单元测试（§8 六级路由全表 / §8.1 计数与转人工 / §8.3 待发箱）。

对应验收：A-CORE-6（计数转人工）、A-CORE-7（路由全表）、A-CORE-10（待发箱
部分）；全部离线（AI 注入桩，TEST1）。

约定：正式模式（dry_run=False）统一先 ``end_rampup``（放量期低风险自动
回复关是 §13 行为，单独成测）；发送任务用 EXECUTE 桩（入队即执行）或
CAPTURE 桩（捕获断言），真实队列行为在 test_orchestrator.py 覆盖。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from job_agent.ai.classify import ClassifyResult
from job_agent.ai.errors import AIBudgetExhausted
from job_agent.ai.guard import guard_outgoing
from job_agent.core.messenger import Messenger, infer_system_status
from job_agent.core.session import BaselineEntry, ConversationState
from job_agent.core.state_machine import SendStateMachine, payload_digest
from job_agent.db import Database
from job_agent.models import MsgType, SendOutcome

from .s4d_support import (
    BASE_TIME,
    EXECUTE,
    FakeAdapter,
    FakeClock,
    LoginRequiredError,
    TransientError,
    closed_config,
    conv_row,
    default_classify,
    default_draft,
    end_rampup,
    events_of,
    make_gate,
    mk_conv,
    mk_msg,
    outbox_rows,
    routing_of,
    seed_conv_row,
    seed_intent,
    seed_outbox,
    wide_config,
)

CAPTURE = "capture"


@pytest.fixture()
def db(tmp_path) -> Database:
    from job_agent.db import open_database

    database = open_database(tmp_path / "test.db")
    yield database
    database.close()


class Harness:
    """单测装配束：messenger + 桩 + 捕获。"""

    def __init__(
        self,
        db: Any,
        *,
        dry_run: bool = False,
        resume_enabled: bool = False,
        classify: str = "low_risk",
        draft: Callable[[Any, str], str] | None = None,
        outbox_timeout_min: int = 60,
        submit_mode: str = EXECUTE,
        keep_rampup: bool = False,
    ) -> None:
        self.db = db
        self.adapter = FakeAdapter()
        self.clock = FakeClock()
        self.config = wide_config(
            dry_run=dry_run,
            resume_enabled=resume_enabled,
            outbox_timeout_min=outbox_timeout_min,
        )
        if not keep_rampup:
            end_rampup(db, self.config)
        self.gate = make_gate(db, self.config, self.clock)
        self.session = ConversationState(db, self.config)
        self.intents = SendStateMachine(db)
        self.submitted: list[tuple[str, Any]] = []
        self.classify_result = default_classify(classify)
        self.draft_fn = draft or default_draft
        self.sleeps: list[float] = []
        self.messenger = Messenger(
            db,
            self.adapter,
            config_provider=lambda: self.config,
            gate_provider=lambda: self.gate,
            session=self.session,
            intents=self.intents,
            submit_reply=self._submit if submit_mode == CAPTURE else self._submit_execute,
            classify_fn=lambda msgs: self.classify_result,
            draft_fn=self.draft_fn,
            guard_fn=guard_outgoing,
            clock=self.clock,
            sleep=self.sleeps.append,
        )

    def _submit(self, name: str, fn: Any) -> None:
        self.submitted.append((name, fn))

    def _submit_execute(self, name: str, fn: Any) -> None:
        self.submitted.append((name, fn))
        fn()

    def poll(self) -> Any:
        return self.messenger.poll_once()


@pytest.fixture()
def h(db: Any) -> Harness:
    return Harness(db)


def one_conv_one_msg(
    harness: Harness, msg_type: MsgType = MsgType.TEXT, text: str = "在吗"
) -> None:
    harness.adapter.convs["C1"] = mk_conv("C1", BASE_TIME + timedelta(minutes=1))
    harness.adapter.add_msg(mk_msg("M1", "C1", msg_type=msg_type, text=text, at=BASE_TIME))


def push_message(harness: Harness, mid: str, text: str, at_min: float) -> None:
    at = BASE_TIME + timedelta(minutes=at_min)
    harness.adapter.convs["C1"] = mk_conv("C1", at)
    harness.adapter.add_msg(mk_msg(mid, "C1", text=text, at=at))


# ---- 时段闸门 [P1] ----


def test_poll_outside_schedule_skips_site(db: Any) -> None:
    harness = Harness(db)
    harness.gate = make_gate(db, closed_config(dry_run=False), harness.clock)
    harness.adapter.convs["C1"] = mk_conv("C1", BASE_TIME)
    report = harness.poll()
    assert report.polled is False
    assert harness.adapter.list_calls == 0


# ---- 路由序 1：SYSTEM → 状态推断（§10.4）----


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("对方已读", "read_no_reply"),
        ("会话已关闭", "closed"),
        ("系统通知", "none"),
    ],
)
def test_route_system_inference(h: Harness, text: str, status: str) -> None:
    one_conv_one_msg(h, MsgType.SYSTEM, text)
    report = h.poll()
    assert report.polled and report.routed == 1
    assert routing_of(h.db) == [("M1", "system", f"state_inference:{status}")]
    assert outbox_rows(h.db) == []
    assert events_of(h.db, "KEY_QUESTION") == []


def test_closed_marker_beats_read(h: Harness) -> None:
    assert infer_system_status("已读；会话已关闭") == "closed"


# ---- 路由序 2：CONTACT_REQUEST_CARD 永不自动 ----


def test_route_contact_request_never_auto(db: Any) -> None:
    # 即使 FR3.8 开、额度可用、放量期已过，也绝不自动处理（§8 路由序 2）
    harness = Harness(db, resume_enabled=True)
    one_conv_one_msg(harness, MsgType.CONTACT_REQUEST_CARD)
    report = harness.poll()
    assert routing_of(db) == [("M1", "contact_request", "outbox_manual+CONTACT_REQUEST")]
    rows = outbox_rows(db)
    assert len(rows) == 1 and rows[0]["state"] == "pending" and rows[0]["source"] == "manual"
    assert events_of(db, "CONTACT_REQUEST")[0][0] == "medium"
    assert harness.adapter.sent_replies == [] and harness.adapter.sent_resumes == []
    assert report.auto_queued == 0


# ---- 路由序 3：RESUME_REQUEST_CARD × FR3.8 ----


def test_route_resume_request_switch_off(h: Harness) -> None:
    one_conv_one_msg(h, MsgType.RESUME_REQUEST_CARD)
    h.poll()
    assert routing_of(h.db) == [("M1", "resume_request", "outbox_manual+KEY_QUESTION")]
    assert len(outbox_rows(h.db)) == 1
    assert events_of(h.db, "KEY_QUESTION")[0][0] == "medium"
    assert h.adapter.sent_resumes == []


def test_route_resume_request_switch_on_auto(db: Any) -> None:
    harness = Harness(db, resume_enabled=True)
    one_conv_one_msg(harness, MsgType.RESUME_REQUEST_CARD)
    report = harness.poll()
    assert routing_of(db) == [("M1", "resume_request", "auto_send_resume")]
    assert harness.adapter.sent_resumes == ["C1"]
    assert conv_row(db, "C1")["resume_sent_count"] == 1
    state = db.query_one("SELECT state FROM send_intents WHERE kind = 'resume'")
    assert state is not None and state["state"] == "sent"
    assert report.auto_queued == 1


def test_route_resume_request_takeover_blocks_auto(db: Any) -> None:
    harness = Harness(db, resume_enabled=True)
    seed_conv_row(db, "C1", takeover=1)
    one_conv_one_msg(harness, MsgType.RESUME_REQUEST_CARD)
    harness.poll()
    assert routing_of(db)[0][2] == "outbox_manual+KEY_QUESTION"
    assert harness.adapter.sent_resumes == []


def test_route_resume_request_dry_run_records_planned(db: Any) -> None:
    harness = Harness(db, dry_run=True, resume_enabled=True)
    one_conv_one_msg(harness, MsgType.RESUME_REQUEST_CARD)
    report = harness.poll()
    assert routing_of(db) == [("M1", "resume_request", "planned_resume_dryrun")]
    assert harness.adapter.sent_resumes == []
    row = db.query_one("SELECT state, dry_run FROM send_intents WHERE kind = 'resume'")
    assert row is not None and row["state"] == "skipped_dryrun" and row["dry_run"] == 1
    assert report.planned_dryrun == 1


# ---- 路由序 4：INTERVIEW_CARD ----


def test_route_interview_card_drafts_to_outbox(h: Harness) -> None:
    one_conv_one_msg(h, MsgType.INTERVIEW_CARD, "周三下午 3 点方便面试吗")
    h.poll()
    _pid, category, action = routing_of(h.db)[0]
    assert (category, action) == ("interview_invite", "outbox_draft+INTERVIEW_INVITE")
    rows = outbox_rows(h.db)
    assert len(rows) == 1 and rows[0]["source"] == "ai"
    assert events_of(h.db, "INTERVIEW_INVITE")[0][0] == "medium"


def test_route_interview_card_ai_failure_fallback(db: Any) -> None:
    def boom(_ctx: Any, _category: str) -> str:
        raise RuntimeError("AI 挂了")

    harness = Harness(db, draft=boom)
    one_conv_one_msg(harness, MsgType.INTERVIEW_CARD)
    harness.poll()
    _pid, category, action = routing_of(db)[0]
    assert category == "interview_invite"
    assert action == "outbox_manual_fallback+INTERVIEW_INVITE"
    assert outbox_rows(db)[0]["source"] == "manual"
    assert events_of(db, "INTERVIEW_INVITE")  # 告警不受草拟失败影响


# ---- 路由序 5：多模态 ×4（AI 不解读 [A2]）----


@pytest.mark.parametrize(
    "msg_type",
    [MsgType.IMAGE, MsgType.VOICE, MsgType.FILE, MsgType.JOB_CARD],
)
def test_route_multimodal_pending_manual(h: Harness, msg_type: MsgType) -> None:
    one_conv_one_msg(h, msg_type)
    h.poll()
    assert routing_of(h.db) == [("M1", "multimodal", "outbox_manual+MULTIMODAL_PENDING")]
    assert outbox_rows(h.db)[0]["source"] == "manual"
    assert events_of(h.db, "MULTIMODAL_PENDING")[0][0] == "medium"
    assert h.submitted == []


# ---- 路由序 6：文本 → AI 四分类 ----


def test_route_low_risk_auto_send_counts_i8(h: Harness) -> None:
    one_conv_one_msg(h)
    report = h.poll()
    assert routing_of(h.db) == [("M1", "low_risk", "auto_send_reply")]
    assert h.adapter.sent_replies == [("C1", default_draft(None, "low_risk"))]
    assert conv_row(h.db, "C1")["auto_sent_count"] == 1
    row = h.db.query_one("SELECT state FROM send_intents WHERE kind = 'reply'")
    assert row is not None and row["state"] == "sent"
    assert report.auto_queued == 1


def test_route_low_risk_guard_rejected_degrades(db: Any) -> None:
    def leak(_ctx: Any, _category: str) -> str:
        return "我的手机号 13800138000，微信同号"

    harness = Harness(db, draft=leak)
    one_conv_one_msg(harness)
    harness.poll()
    _pid, category, action = routing_of(db)[0]
    assert category == "low_risk"
    assert action.startswith("outbox_draft:guard_rejected:")
    assert "[contact/phone]" in action  # 护栏原因标注 [§9.4]
    assert harness.adapter.sent_replies == []
    assert outbox_rows(db)[0]["draft_text"].startswith("我的手机号")


def test_route_low_risk_at_limit_drafts_only(db: Any) -> None:
    harness = Harness(db)
    seed_conv_row(db, "C1", auto=2)  # 默认上限 2
    one_conv_one_msg(harness)
    harness.poll()
    _pid, category, action = routing_of(db)[0]
    assert category == "low_risk"
    assert action.startswith("outbox_draft:no_auto:auto_reply_limit_reached")
    assert harness.adapter.sent_replies == []


def test_route_low_risk_rampup_disables_auto_reply(db: Any) -> None:
    from job_agent.core import rampup

    # keep_rampup：dry_run=False → 放量期惰性激活（§13：低风险自动回复关）
    harness = Harness(db, keep_rampup=True)
    assert rampup.is_rampup_active(db, harness.config) is True
    one_conv_one_msg(harness)
    harness.poll()
    _pid, category, action = routing_of(db)[0]
    assert category == "low_risk"
    assert action.startswith("outbox_draft:no_auto:rampup")
    assert harness.adapter.sent_replies == []


def test_route_low_risk_dry_run_records_planned(db: Any) -> None:
    harness = Harness(db, dry_run=True)
    one_conv_one_msg(harness)
    report = harness.poll()
    assert routing_of(db) == [("M1", "low_risk", "planned_reply_dryrun")]
    assert harness.adapter.sent_replies == []
    row = db.query_one("SELECT state, dry_run FROM send_intents WHERE kind = 'reply'")
    assert row is not None and row["state"] == "skipped_dryrun" and row["dry_run"] == 1
    assert conv_row(db, "C1")["auto_sent_count"] == 0  # 试运行不计数 [N2]
    assert report.planned_dryrun == 1


def test_route_key_question_to_outbox_with_alert(h: Harness) -> None:
    h.classify_result = ClassifyResult("key", "问薪资")
    one_conv_one_msg(h, text="期望薪资多少")
    h.poll()
    _pid, category, action = routing_of(h.db)[0]
    assert (category, action) == ("key", "outbox_draft+KEY_QUESTION")
    assert outbox_rows(h.db)[0]["source"] == "ai"
    assert events_of(h.db, "KEY_QUESTION")[0][0] == "medium"


def test_route_positive_response_off_to_outbox(h: Harness) -> None:
    h.classify_result = ClassifyResult("positive_response", "感兴趣")
    one_conv_one_msg(h)
    h.poll()
    _pid, category, action = routing_of(h.db)[0]
    assert (category, action) == ("positive_response", "outbox_draft+OFFER_SIGNAL")
    assert events_of(h.db, "OFFER_SIGNAL")[0][0] == "medium"


def test_route_positive_response_on_auto_resume(db: Any) -> None:
    harness = Harness(db, resume_enabled=True)
    harness.classify_result = ClassifyResult("positive_response", "索要简历")
    one_conv_one_msg(harness)
    harness.poll()
    assert routing_of(db)[0][2] == "auto_send_resume"
    assert harness.adapter.sent_resumes == ["C1"]
    assert conv_row(db, "C1")["resume_sent_count"] == 1


def test_route_positive_response_resume_limit_blocks(db: Any) -> None:
    harness = Harness(db, resume_enabled=True)
    seed_conv_row(db, "C1", resume=1)
    harness.classify_result = ClassifyResult("positive_response", "再次索要简历")
    one_conv_one_msg(harness)
    harness.poll()
    _pid, category, action = routing_of(db)[0]
    assert category == "positive_response"
    assert action.startswith("outbox_draft") and action.endswith("+OFFER_SIGNAL")
    assert harness.adapter.sent_resumes == []


def test_route_no_reply_silent(h: Harness) -> None:
    h.classify_result = ClassifyResult("no_reply", "好的")
    one_conv_one_msg(h, text="好的")
    h.poll()
    assert routing_of(h.db) == [("M1", "no_reply", "skip_no_reply")]
    assert outbox_rows(h.db) == []
    assert h.adapter.sent_replies == [] and h.submitted == []
    assert conv_row(h.db, "C1")["auto_sent_count"] == 0
    assert events_of(h.db, "KEY_QUESTION") == []  # 不发送不计数不告警 [B2]


def test_route_unknown_category_defensive_key(h: Harness) -> None:
    h.classify_result = ClassifyResult("bogus", "防御分支")
    one_conv_one_msg(h)
    h.poll()
    assert routing_of(h.db)[0][1] == "key"


# ---- §8.1 计数与转人工（A-CORE-6）----


def test_auto_reply_limit_reached_takes_over(db: Any) -> None:
    harness = Harness(db)
    seed_conv_row(db, "C1", auto=1)  # 已发 1 条，上限 2
    one_conv_one_msg(harness)
    harness.poll()  # 第 2 条：计数 2 → 达上限转人工 + 中级事件
    row = conv_row(db, "C1")
    assert row["auto_sent_count"] == 2 and row["manual_takeover"] == 1
    assert row["blocked_reason"] == "auto_reply_limit_reached"
    levels = events_of(db, "CONV_LIMIT_REACHED")
    assert levels[0][0] == "medium"
    # 转人工后：新消息仍草拟进待发箱（§8.3），不再自动发送
    push_message(harness, "M2", "在吗", 2)
    harness.poll()
    _pid, _category, action = routing_of(db)[-1]
    assert action.startswith("outbox_draft:no_auto:manual_takeover")
    assert len(harness.adapter.sent_replies) == 1


def test_manual_takeover_by_digest_miss(h: Harness) -> None:
    h.adapter.convs["C1"] = mk_conv("C1", BASE_TIME + timedelta(minutes=1))
    # 用户在 App 手回（无匹配 intent）
    h.adapter.add_msg(
        mk_msg("U1", "C1", direction="out", sender="me", text="我手动回的", at=BASE_TIME)
    )
    report = h.poll()
    row = conv_row(h.db, "C1")
    assert row["manual_takeover"] == 1
    assert row["blocked_reason"] == "manual_takeover:digest_miss"
    stored = h.db.query_one("SELECT is_ours_auto FROM messages WHERE platform_msg_id = 'U1'")
    assert stored is not None and stored["is_ours_auto"] == 0
    assert report.takeovers == 1


def test_auto_out_message_matched_no_takeover(h: Harness) -> None:
    digest = payload_digest("自动回复内容")
    seed_intent(
        h.db,
        kind="reply",
        target_ref="C1",
        digest=digest,
        created_at=BASE_TIME - timedelta(minutes=1),
    )
    h.adapter.convs["C1"] = mk_conv("C1", BASE_TIME + timedelta(minutes=1))
    h.adapter.add_msg(
        mk_msg("A1", "C1", direction="out", sender="me", text="自动回复内容", at=BASE_TIME)
    )
    report = h.poll()
    row = conv_row(h.db, "C1")
    assert row["manual_takeover"] == 0
    stored = h.db.query_one("SELECT is_ours_auto FROM messages WHERE platform_msg_id = 'A1'")
    assert stored is not None and stored["is_ours_auto"] == 1
    intent = h.db.query_one("SELECT state FROM send_intents WHERE kind = 'reply'")
    assert intent is not None and intent["state"] == "sent"
    assert report.takeovers == 0


def test_baseline_conversation_skipped(h: Harness) -> None:
    h.session.snapshot_baseline([BaselineEntry("C0", "HR旧", None)])
    h.adapter.convs["C0"] = mk_conv("C0", BASE_TIME)
    h.adapter.add_msg(mk_msg("B1", "C0", text="旧会话新消息", at=BASE_TIME))
    report = h.poll()
    assert report.conversations == 0
    assert h.db.query_one("SELECT 1 FROM messages WHERE conv_id = 'C0'") is None
    assert routing_of(h.db) == []


# ---- 发送失败语义（§5.2 / NFR2 / B4）----


def test_resume_send_failed_not_retried_not_counted(db: Any) -> None:
    harness = Harness(db, resume_enabled=True)
    harness.classify_result = ClassifyResult("positive_response", "索要简历")
    harness.adapter.resume_outcome = SendOutcome.FAILED
    one_conv_one_msg(harness)
    harness.poll()
    assert harness.adapter.sent_resumes == ["C1"]  # 调用一次即失败
    intent = db.query_one("SELECT state FROM send_intents WHERE kind = 'resume'")
    assert intent is not None and intent["state"] == "failed"
    assert conv_row(db, "C1")["resume_sent_count"] == 0


def test_transient_error_retries_then_failed(db: Any) -> None:
    harness = Harness(db)
    harness.adapter.reply_error_queue = [TransientError(), TransientError(), TransientError()]
    one_conv_one_msg(harness)
    harness.poll()
    assert harness.sleeps == [1.0, 2.0]  # ≤2 次指数退避重试（1s → 2s）[§5.2]
    intent = db.query_one("SELECT state FROM send_intents WHERE kind = 'reply'")
    assert intent is not None and intent["state"] == "failed"
    assert conv_row(db, "C1")["auto_sent_count"] == 0
    assert harness.adapter.sent_replies == []


def test_fatal_error_propagates_from_task(db: Any) -> None:
    harness = Harness(db, submit_mode=CAPTURE)
    harness.adapter.reply_error = LoginRequiredError()
    one_conv_one_msg(harness)
    harness.poll()
    assert len(harness.submitted) == 1
    with pytest.raises(LoginRequiredError):  # 编排器按 §5.2 分类处置
        harness.submitted[0][1]()


# ---- AI 预算（B13）----


def test_budget_exhausted_alerts_once_per_day(db: Any) -> None:
    def budget_boom(_ctx: Any, _category: str) -> str:
        raise AIBudgetExhausted("当日预算耗尽")

    harness = Harness(db, draft=budget_boom)
    one_conv_one_msg(harness)
    harness.poll()
    _pid, category, action = routing_of(db)[0]
    assert category == "low_risk" and action == "outbox_manual:ai_budget_exhausted"
    assert events_of(db, "AI_BUDGET_EXHAUSTED")[0][0] == "medium"
    # 同日第二次不再告警
    push_message(harness, "M2", "还在吗", 2)
    harness.poll()
    assert len(events_of(db, "AI_BUDGET_EXHAUSTED")) == 1


def test_budget_exhausted_sniffed_from_classify_reason(db: Any) -> None:
    harness = Harness(db)
    harness.classify_result = ClassifyResult("key", "分类失败兜底 key：AIBudgetExhausted")
    one_conv_one_msg(harness)
    harness.poll()
    assert len(events_of(db, "AI_BUDGET_EXHAUSTED")) == 1


# ---- 待发箱规则（§8.3/B10）----


def test_new_message_marks_pending_draft_stale(h: Harness) -> None:
    h.classify_result = ClassifyResult("key", "问薪资")
    one_conv_one_msg(h)
    h.poll()  # 产生 pending 草稿
    assert outbox_rows(h.db)[0]["state"] == "pending"
    push_message(h, "M2", "追问", 2)
    report = h.poll()
    assert outbox_rows(h.db)[0]["state"] == "stale"
    assert report.stale_marked == 1


def test_manual_approve_not_counted_and_sends(h: Harness) -> None:
    seed_conv_row(h.db, "C1")
    entry_id = seed_outbox(h.db, "C1", draft="人工确认的回复", created_at=BASE_TIME)
    detail = h.messenger.approve_outbox_entry(entry_id)
    assert detail == "approved_queued"
    assert outbox_rows(h.db)[0]["state"] == "approved"
    assert h.adapter.sent_replies == [("C1", "人工确认的回复")]
    assert conv_row(h.db, "C1")["auto_sent_count"] == 0  # 人工行为不计自动数 [B10]
    intent = h.db.query_one("SELECT state FROM send_intents WHERE kind = 'reply'")
    assert intent is not None and intent["state"] == "sent"


def test_manual_approve_dry_run_records_planned(db: Any) -> None:
    harness = Harness(db, dry_run=True)
    seed_conv_row(db, "C1")
    entry_id = seed_outbox(db, "C1", created_at=BASE_TIME)
    detail = harness.messenger.approve_outbox_entry(entry_id)
    assert detail == "approved_dryrun"
    assert harness.adapter.sent_replies == []
    row = db.query_one("SELECT state FROM send_intents WHERE kind = 'reply'")
    assert row is not None and row["state"] == "skipped_dryrun"


def test_skip_outbox_entry(h: Harness) -> None:
    seed_conv_row(h.db, "C1")
    entry_id = seed_outbox(h.db, "C1", created_at=BASE_TIME)
    assert h.messenger.skip_outbox_entry(entry_id) == "skipped"
    assert h.messenger.skip_outbox_entry(entry_id) == "invalid_state"
    assert h.messenger.skip_outbox_entry(9999) == "not_found"


def test_outbox_timeout_merged_single_alert(h: Harness) -> None:
    seed_conv_row(h.db, "C1")
    seed_outbox(h.db, "C1", created_at=BASE_TIME - timedelta(hours=2))
    seed_outbox(h.db, "C1", created_at=BASE_TIME - timedelta(hours=3))
    report = h.poll()
    assert report.timeout_alert is not None
    alerts = events_of(h.db, "OUTBOX_TIMEOUT")
    assert len(alerts) == 1  # 合并为一条高级告警 [s6]
    assert alerts[0][0] == "high"
    assert alerts[0][1]["count"] == 2
    rows = outbox_rows(h.db)
    assert all(r["reminded_at"] is not None for r in rows)
    # 下一轮不再重复提醒（每条目只提醒一次）
    h.clock.advance(timedelta(minutes=10))
    report2 = h.poll()
    assert report2.timeout_alert is None
    assert len(events_of(h.db, "OUTBOX_TIMEOUT")) == 1


def test_outbox_fresh_entries_no_alert(h: Harness) -> None:
    seed_conv_row(h.db, "C1")
    seed_outbox(h.db, "C1", created_at=BASE_TIME - timedelta(minutes=10))
    report = h.poll()
    assert report.timeout_alert is None


# ---- 对账任务（§13）----


def test_reconcile_once_settles_unknown_intent(db: Any) -> None:
    harness = Harness(db)
    digest = payload_digest("崩溃前发出的回复")
    seed_intent(
        db,
        kind="reply",
        target_ref="C1",
        digest=digest,
        state="unknown",
        created_at=BASE_TIME - timedelta(minutes=30),
    )
    harness.adapter.convs["C1"] = mk_conv("C1", BASE_TIME - timedelta(minutes=25))
    harness.adapter.add_msg(
        mk_msg(
            "R1",
            "C1",
            direction="out",
            sender="me",
            text="崩溃前发出的回复",
            at=BASE_TIME - timedelta(minutes=29),
        )
    )
    report = harness.messenger.reconcile_once()
    assert report is not None and len(report.matched) == 1
    intent = db.query_one("SELECT state FROM send_intents WHERE kind = 'reply'")
    assert intent is not None and intent["state"] == "sent"


def test_reconcile_once_no_open_intents_noop(h: Harness) -> None:
    assert h.messenger.reconcile_once() is None
    assert h.adapter.list_calls == 0


# ---- 其他细节 ----


def test_breaker_error_from_poll_propagates(db: Any) -> None:
    # §5.2 熔断类（登录失效）从轮询上抛，交编排器全停；未分类才 NFR2 跳过
    harness = Harness(db)
    harness.adapter.convs["C1"] = mk_conv("C1", BASE_TIME)
    harness.adapter.get_messages_error = LoginRequiredError()
    with pytest.raises(LoginRequiredError):
        harness.poll()


def test_poll_report_counts_and_dedup(h: Harness) -> None:
    one_conv_one_msg(h)
    report = h.poll()
    assert (report.polled, report.conversations, report.new_messages, report.routed) == (
        True,
        1,
        1,
        1,
    )
    # 再轮询：无新消息（列表时间未推进）→ 不触 get_messages
    report2 = h.poll()
    assert report2.new_messages == 0 and report2.routed == 0


def test_takeover_conv_count_frozen(db: Any) -> None:
    # 已转人工 + 已达上限：不再自动发送，计数冻结
    harness = Harness(db)
    seed_conv_row(db, "C1", auto=2, takeover=1)
    one_conv_one_msg(harness)
    harness.poll()
    assert conv_row(db, "C1")["auto_sent_count"] == 2


def test_out_message_not_routed(h: Harness) -> None:
    h.adapter.convs["C1"] = mk_conv("C1", BASE_TIME + timedelta(minutes=1))
    h.adapter.add_msg(
        mk_msg(
            "S1",
            "C1",
            direction="out",
            sender="system",
            msg_type=MsgType.SYSTEM,
            text="已发送简历",
            at=BASE_TIME,
        )
    )
    report = h.poll()
    assert report.routed == 0  # 出站消息走对账路径，不路由
