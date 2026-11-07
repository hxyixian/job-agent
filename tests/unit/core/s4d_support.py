"""S4d 测试共用装配（TEST1 全离线：FakeAdapter / FakeClock / 宽时段配置）。

- FakeAdapter 按 §5.2 契约的最小面模拟站点（结构化满足 core.adapter_api.
  AdapterAPI）；异常按 §5.2 异常树**类名**抛出（LocalTransientError 等本地
  类与 S4a adapters.base 的同名类对 classify_adapter_error 等价）；
- 宽时段配置：run_schedule 全周 00:00-23:59（polling 默认共用），闸门恒开，
  时段外用 closed_config（start==end 恒 False）；
- 正式模式（dry_run=False）会惰性激活放量期（§13），自动回复测试统一先
  ``end_rampup_early`` 模拟"已过放量期"；放量期行为单独成测。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from job_agent.ai.classify import ClassifyResult
from job_agent.config import Config
from job_agent.core import dryrun, quota, rampup
from job_agent.core.scheduler import Gate, QuotaAPI
from job_agent.db import Database
from job_agent.models import (
    Conversation,
    JobRef,
    LoginState,
    Message,
    MsgType,
    RiskSignal,
    SendOutcome,
)
from job_agent.timeutil import as_utc, utc_str

UTC = UTC

# 发送任务入队模式（test_messenger.Harness 用）
EXECUTE = "execute"
CAPTURE = "capture"

# 固定基准时刻：2026-11-05 周四 10:00 UTC（北京 18:00，宽时段内）
BASE_TIME = datetime(2026, 11, 5, 10, 0, 0, tzinfo=UTC)


# ---- §5.2 异常类的本地等价物（类名即分类依据）----


class LoginRequiredError(Exception):
    pass


class RiskDetectedError(Exception):
    def __init__(self, signal: RiskSignal) -> None:
        super().__init__(f"risk {signal.name}")
        self.signal = signal


class AdapterBrokenError(Exception):
    pass


class PlatformLimitError(Exception):
    pass


class TransientError(Exception):
    pass


# ---- 时钟 / 配置 ----


class FakeClock:
    """可推进的注入时钟（tz-aware UTC）。"""

    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def wide_config(
    *,
    dry_run: bool = False,
    resume_enabled: bool = False,
    outbox_timeout_min: int = 60,
    every_loop: bool = True,
    auto_reply_limit: int = 2,
) -> Config:
    """宽时段配置（闸门恒开；action_delay 最小化，节流由注入 rng/sleep 控制）。"""
    return Config.model_validate(
        {
            "delivery": {
                "dry_run": dry_run,
                "run_schedule": {
                    "weekdays": [1, 2, 3, 4, 5, 6, 7],
                    "start": "00:00",
                    "end": "23:59",
                },
                "login_patrol": {"every_loop": every_loop, "interval_min": 10},
                "action_delay_range_s": [1, 1],
            },
            "messaging": {
                "positive_response_resume_enabled": resume_enabled,
                "outbox_timeout_min": outbox_timeout_min,
                "auto_reply_limit": auto_reply_limit,
            },
        }
    )


def closed_config(*, dry_run: bool = False) -> Config:
    """时段恒关配置（start==end → is_active 恒 False）。"""
    return Config.model_validate(
        {
            "delivery": {
                "dry_run": dry_run,
                "run_schedule": {
                    "weekdays": [1, 2, 3, 4, 5, 6, 7],
                    "start": "00:00",
                    "end": "00:00",
                },
            },
        }
    )


def end_rampup(db: Database, config: Config) -> None:
    """正式模式下立即结束放量期（模拟已过 3 个有效日；§13 行为单独成测）。"""
    dryrun.is_dry_run(db, config)  # 播种模式标志
    if not dryrun.is_dry_run(db, config):
        rampup.end_rampup_early(db, config)


# ---- 站点桩 ----


class FakeAdapter:
    """§5.2 最小面的内存站点桩（结构化满足 AdapterAPI）。"""

    site_id = "fake"

    def __init__(self) -> None:
        self.convs: dict[str, Conversation] = {}
        self.messages: dict[str, list[Message]] = {}
        self.login_state = LoginState.LOGGED_IN
        self.risk_signal = RiskSignal.NONE
        self.login_checks = 0
        self.detect_calls = 0
        self.list_calls = 0
        self.sent_replies: list[tuple[str, str]] = []
        self.sent_resumes: list[str] = []
        self.reply_outcome = SendOutcome.SENT
        self.resume_outcome = SendOutcome.SENT
        self.reply_error_queue: list[Exception | None] = []
        self.reply_error: Exception | None = None
        self.resume_error: Exception | None = None
        self.get_messages_error: Exception | None = None

    # ---- AdapterAPI ----

    def check_login_state(self) -> LoginState:
        self.login_checks += 1
        return self.login_state

    def detect_risk(self) -> RiskSignal:
        self.detect_calls += 1
        return self.risk_signal

    def list_conversations(self) -> list[Conversation]:
        self.list_calls += 1
        return sorted(self.convs.values(), key=lambda c: as_utc(c.last_message_at), reverse=True)

    def get_messages(self, conv_id: str, since: datetime | None) -> list[Message]:
        if self.get_messages_error is not None:
            raise self.get_messages_error
        msgs = sorted(self.messages.get(conv_id, []), key=lambda m: as_utc(m.sent_at))
        if since is None:
            return list(msgs)
        return [m for m in msgs if as_utc(m.sent_at) > as_utc(since)]

    def send_reply(self, conv_id: str, message: str) -> SendOutcome:
        if self.reply_error_queue:
            error = self.reply_error_queue.pop(0)
            if error is not None:
                raise error
        elif self.reply_error is not None:
            raise self.reply_error
        self.sent_replies.append((conv_id, message))
        return self.reply_outcome

    def send_resume(self, conv_id: str) -> SendOutcome:
        if self.resume_error is not None:
            raise self.resume_error
        self.sent_resumes.append(conv_id)
        return self.resume_outcome

    # ---- 装配助手 ----

    def add_msg(self, msg: Message) -> None:
        self.messages.setdefault(msg.conv_id, []).append(msg)
        conv = self.convs.get(msg.conv_id)
        if conv is not None and as_utc(msg.sent_at) > as_utc(conv.last_message_at):
            self.convs[msg.conv_id] = replace(
                conv, last_message_at=as_utc(msg.sent_at), unread=conv.unread + 1
            )


# ---- 领域对象工厂 ----


def mk_job(job_id: str = "J1") -> JobRef:
    return JobRef(
        job_id=job_id,
        title="Python 后端",
        company_name="示例科技",
        company_key=f"ck-{job_id}",
        salary_text="20-35K",
        tags=["Python"],
        list_snippet="岗位摘要",
    )


def mk_conv(
    conv_id: str,
    at: datetime,
    *,
    job: JobRef | None = None,
    unread: int = 1,
) -> Conversation:
    return Conversation(
        conv_id=conv_id,
        peer_name="HR甲",
        job_ref=job,
        last_message_at=as_utc(at),
        unread=unread,
    )


def mk_msg(
    mid: str,
    conv_id: str,
    *,
    direction: str = "in",
    sender: str = "hr",
    msg_type: MsgType = MsgType.TEXT,
    text: str = "",
    at: datetime | None = None,
) -> Message:
    return Message(
        platform_msg_id=mid,
        conv_id=conv_id,
        direction=direction,  # type: ignore[arg-type]
        sender=sender,  # type: ignore[arg-type]
        msg_type=msg_type,
        text=text,
        sent_at=as_utc(at or BASE_TIME),
    )


# ---- 装配 ----


def make_gate(db: Database, config: Config, clock: Any) -> Gate:
    quota.configure(db, config)
    api = QuotaAPI(
        used_quota=quota.used_quota,
        browse_used=quota.browse_used,
        current_limits=quota.current_limits,  # type: ignore[arg-type]
        min_interval_s=quota.min_interval_s,
    )
    return Gate(config.delivery, api, clock=clock)


def events_of(db: Database, event_type: str) -> list[tuple[str, dict[str, Any]]]:
    rows = db.query(
        "SELECT level, payload_json FROM notification_events WHERE event_type = ? ORDER BY id",
        (event_type,),
    )
    return [(str(r["level"]), json.loads(str(r["payload_json"]))) for r in rows]


def routing_of(db: Database) -> list[tuple[str, str, str]]:
    rows = db.query("SELECT platform_msg_id, category, action FROM msg_routing ORDER BY id")
    return [(str(r["platform_msg_id"]), str(r["category"]), str(r["action"])) for r in rows]


def outbox_rows(db: Database, conv_id: str | None = None) -> list[dict[str, Any]]:
    if conv_id is None:
        rows = db.query("SELECT * FROM outbox ORDER BY id")
    else:
        rows = db.query("SELECT * FROM outbox WHERE conv_id = ? ORDER BY id", (conv_id,))
    return [dict(r) for r in rows]


def conv_row(db: Database, conv_id: str) -> Any:
    return db.query_one("SELECT * FROM conversations WHERE conv_id = ?", (conv_id,))


def seed_conv_row(
    db: Database,
    conv_id: str,
    *,
    auto: int = 0,
    resume: int = 0,
    takeover: int = 0,
    baseline: int = 0,
    job_id: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO conversations (conv_id, peer_name, job_id, is_baseline, manual_takeover,"
        " auto_sent_count, resume_sent_count) VALUES (?, 'HR甲', ?, ?, ?, ?, ?)",
        (conv_id, job_id, baseline, takeover, auto, resume),
    )


def seed_intent(
    db: Database,
    *,
    kind: str,
    target_ref: str,
    digest: str,
    state: str = "pending",
    created_at: datetime,
) -> int:
    return db.execute_returning_id(
        "INSERT INTO send_intents (kind, target_ref, payload_digest, state, dry_run, created_at)"
        " VALUES (?, ?, ?, ?, 0, ?)",
        (kind, target_ref, digest, state, utc_str(created_at)),
    )


def seed_outbox(
    db: Database,
    conv_id: str,
    *,
    draft: str = "草稿内容",
    source: str = "ai",
    state: str = "pending",
    created_at: datetime,
) -> int:
    return db.execute_returning_id(
        "INSERT INTO outbox (conv_id, draft_text, source, state, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (conv_id, draft, source, state, utc_str(created_at)),
    )


def default_classify(category: str) -> ClassifyResult:
    return ClassifyResult(category, "测试注入")


def default_draft(ctx: Any, category: str) -> str:
    return "您好，感谢您的消息，我确认后回复您。"
