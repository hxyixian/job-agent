"""消息轮询与路由引擎（设计文档 §8 / §8.1 / §8.2 / §8.3；任务书 Stage4 S4d 第 1 条）。

轮询循环（:meth:`Messenger.poll_once` 一轮，由编排器按
``delivery.polling.interval_range_min``（默认 3-5 分钟随机）调度）::

    时段闸门 can_poll [P1] → list_conversations → 基线过滤 [B3]
    → 对有新消息的会话 get_messages(since) → 落库 → 我方消息对账（接管检测）
    → 逐条六级路由（msg_routing 留痕）→ 动作 → 待发箱超时扫描 [s6/B10]

**六级路由表**（§8 权威，命中即止，一条消息多意图按最高风险处理 [B1]）：

1. ``SYSTEM`` → ``system``：不回复；§10.4 状态推断，action=
   ``state_inference:none|read_no_reply|closed``；
2. ``CONTACT_REQUEST_CARD`` → ``contact_request``：**永不自动处理**——
   待发箱（manual 占位）+ CONTACT_REQUEST 中级告警；
3. ``RESUME_REQUEST_CARD`` → ``resume_request``：FR3.8 开 →
   ``auto_send_resume`` / ``planned_resume_dryrun``；关/受限 →
   待发箱 + KEY_QUESTION；
4. ``INTERVIEW_CARD`` → ``interview_invite``：AI 草拟进待发箱 +
   INTERVIEW_INVITE 中级告警（草拟失败降级 manual_fallback）；
5. ``IMAGE/VOICE/FILE/JOB_CARD`` → ``multimodal``：AI 不解读 [A2]——
   待发箱（manual 占位）+ MULTIMODAL_PENDING 中级告警；
6. 文本 → AI 四分类 → ``low_risk / key / positive_response / no_reply``（见下）。

文本四分类动作（§8.2；AI 分类失败恒 key [B1]——ai/classify 已兜底）：

- ``low_risk``：草拟→护栏→通过→入队发送（发送成功计 I8，§8.1）；护栏命中→
  待发箱（guard_rejected，草稿保留供人工放行）；会话转人工/达上限/放量期→
  仍草拟进待发箱（no_auto）；dry_run→planned_reply_dryrun（拟发送记录，
  不发送不计数 [N2]）；
- ``key``：草拟→待发箱 + KEY_QUESTION（中级）；
- ``positive_response``：FR3.8 开→附件简历自动投（resume 独立计数 §8.1）；
  关/受限→草拟→待发箱 + OFFER_SIGNAL（中级）；
- ``no_reply``：skip_no_reply——不发送、不计数、不告警 [B2]。

计数与转人工（§8.1，S2d session 提供）：``auto_sent_count`` 覆盖招呼语与
自动回复消息（发送成功后 ``record_auto_send`` +1，达上限自动转人工 +
CONV_LIMIT_REACHED 中级事件）；``resume_sent_count`` 每会话 ≤1、独立计数、
失败不重试（NFR2）。**手动接管检测** [B3/G6/F1] 经
:meth:`SendStateMachine.reconcile`（reply/resume 按 payload_digest ±10min、
greeting 按关联岗位+时间窗不做文本比对）——匹配不上的我方消息 →
``manual_takeover=1``（source=digest_miss，is_ours_auto=0）。每日首投
greeting 核验读回天然命中，不误判 [F1]。

待发箱规则（§8.3/B10）：**人工确认发送不计自动数**
（:meth:`approve_outbox_entry`，count_auto=False）；转人工后新消息仍草拟
进待发箱；会话有新入站消息时既有 pending 条目标记 ``stale``（确认页提示
重新生成）；每轮轮询扫描一次超时滞留（notify.center.scan_outbox_timeout：
合并为一条 OUTBOX_TIMEOUT 高级告警，每条目只提醒一次；时段外到期的条目
下一轮自然并入，不即时唤醒 [B10]）。

dry_run 联动 [N2/FR3.9]：自动发送路径改写拟发送记录
（``send_intents.state=skipped_dryrun``），不触碰适配器、不计数。

发送节流：回复/简历发送经编排器单执行者队列（REPLY 优先级）串行执行，
8-20s 拟人节奏由编排器统一施加（§5.3：与投递共用节奏参数）；本模块发送
任务内仅处理 ``TransientError`` 指数退避重试 ≤2 次（§5.2），仍失败按
FAILED 落库。其余异常按 §5.2 表上抛编排器（登录失效/风控/适配器失效）或
落 FAILED 跳过（未分类，NFR2）。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

from job_agent.ai.classify import ClassifyResult, classify_messages
from job_agent.ai.errors import AIBudgetExhausted
from job_agent.ai.guard import GuardResult, guard_outgoing
from job_agent.ai.reply import ConversationContext, draft_reply
from job_agent.config import Config
from job_agent.core import dryrun, events, rampup, runstate
from job_agent.core.adapter_api import AdapterAPI, TaskFn, classify_adapter_error
from job_agent.core.scheduler import Gate, utc_clock
from job_agent.core.session import ConversationState
from job_agent.core.state_machine import (
    MATCH_WINDOW,
    ReconcileReport,
    SendStateMachine,
    payload_digest,
)
from job_agent.db import Database
from job_agent.models import Conversation, Message, MsgType, ResumeSnapshot, SendOutcome
from job_agent.notify.base import EventRecord
from job_agent.notify.center import scan_outbox_timeout
from job_agent.timeutil import as_utc, beijing_date_str, utc_str

__all__ = [
    "KEY_AI_BUDGET_NOTIFIED",
    "ClassifyFn",
    "DraftFn",
    "GuardFn",
    "Messenger",
    "PollReport",
    "infer_system_status",
]

logger = logging.getLogger(__name__)

# run_state 键：AI 预算耗尽中级告警的当日去重（值=北京日 YYYY-MM-DD）[B13]
KEY_AI_BUDGET_NOTIFIED = "ai_budget_notified_date"

# 分类/草拟/护栏注入点签名（默认绑定 S3b 实现；TEST1 注入桩）
ClassifyFn = Callable[[list[Message]], ClassifyResult]
DraftFn = Callable[[ConversationContext, str], str]
GuardFn = Callable[[str], GuardResult]

# §8 AI 四分类枚举（msg_routing.category CHECK 的子集）
_AI_CATEGORIES = frozenset({"low_risk", "key", "positive_response", "no_reply"})

# §10.4 SYSTEM 状态推断的保守关键词（A4：'已读'文本可定位）
_READ_MARKERS: tuple[str, ...] = ("已读",)
_CLOSED_MARKERS: tuple[str, ...] = ("会话已关闭", "聊天已结束", "职位已关闭", "对方已关闭")

# 待发箱占位草稿（卡片/多模态不经 AI；AI 失败兜底）
_PLACEHOLDER_CONTACT = "【联系方式交换请求】永不自动处理——请人工确认后再发送。"
_PLACEHOLDER_RESUME = "【索要简历】请人工确认后发送附件简历。"
_PLACEHOLDER_MULTIMODAL = "【多模态消息】AI 不解读——请人工查看原消息后处理。"
_PLACEHOLDER_AI_FAILED = "【AI 草拟失败】请人工撰写回复。"

# 简历附件 intent 的 digest 口径（无文本 payload，I2 契约）
_RESUME_DIGEST = payload_digest("")


@dataclass
class PollReport:
    """一轮轮询的结果摘要（日志/测试断言用；不回传线程对象 [I6]）。"""

    polled: bool = False
    conversations: int = 0  # 基线外会话数
    new_messages: int = 0  # 新入库消息数（含我方/系统）
    routed: int = 0  # 路由决策条数（msg_routing 新行）
    outbox_added: int = 0
    stale_marked: int = 0
    auto_queued: int = 0  # 真实自动发送入队数（reply/resume）
    planned_dryrun: int = 0  # 试运行拟发送记录数 [N2]
    takeovers: int = 0  # 新增转人工（digest_miss）
    timeout_alert: EventRecord | None = None
    errors: list[str] = field(default_factory=list)


def infer_system_status(text: str) -> str:
    """§10.4 SYSTEM 状态推断（保守，closed > read_no_reply > none）。

    结果写入 ``msg_routing.action``（``state_inference:<status>``），控制台
    会话状态分布（GET /stats）据此推断；手动标记永远优先（S5 层）。
    """
    if any(marker in text for marker in _CLOSED_MARKERS):
        return "closed"
    if any(marker in text for marker in _READ_MARKERS):
        return "read_no_reply"
    return "none"


class Messenger:
    """消息轮询与路由引擎（§8）。站点操作全部经 AdapterAPI，单线程串行调用。"""

    def __init__(
        self,
        db: Database,
        adapter: AdapterAPI,
        *,
        config_provider: Callable[[], Config],
        gate_provider: Callable[[], Gate],
        session: ConversationState,
        intents: SendStateMachine,
        submit_reply: Callable[[str, TaskFn], None] | None = None,
        classify_fn: ClassifyFn = classify_messages,
        draft_fn: DraftFn = draft_reply,
        guard_fn: GuardFn = guard_outgoing,
        clock: Callable[[], datetime] = utc_clock,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._db = db
        self._adapter = adapter
        self._config_provider = config_provider
        self._gate_provider = gate_provider
        self._session = session
        self._intents = intents
        self._submit = submit_reply
        self._classify_fn = classify_fn
        self._draft_fn = draft_fn
        self._guard_fn = guard_fn
        self._clock = clock
        self._sleep = sleep

    # ---- 装配（编排器 bind；也可构造期直接注入）----

    def set_reply_submitter(self, submit: Callable[[str, TaskFn], None]) -> None:
        """绑定发送入队口（编排器 REPLY 优先级任务的 late-binding 装配位）。"""
        self._submit = submit

    # ---- 轮询主入口（编排器按 interval_range_min 调度）----

    def poll_once(self, now: datetime | None = None) -> PollReport:
        """执行一轮轮询（§8）。时段外（can_poll=False）直接返回，不触站点。

        登录失效/风控/适配器失效类异常（§5.2 熔断行）上抛给编排器统一处置；
        其余（平台限额/瞬时/未分类）按 NFR2 记录后中断本轮/跳过该会话。
        """
        report = PollReport()
        at = self._clock() if now is None else as_utc(now)
        if not self._gate_provider().can_poll(at):
            return report
        report.polled = True
        try:
            convs = self._adapter.list_conversations()
        except Exception as exc:
            report.errors.append(f"list_conversations:{type(exc).__name__}")
            if _is_breaker_error(exc):
                raise  # §5.2 熔断类：交编排器（fatal/M5 全停）
            self._log_adapter_error("list_conversations", exc)
            report.timeout_alert = scan_outbox_timeout(self._db, self._config(), now=at)
            return report
        eligible = set(self._session.baseline_filter([c.conv_id for c in convs]))
        report.conversations = len(eligible)
        for conv in convs:
            if conv.conv_id not in eligible:
                continue  # 基线会话不参与自动化 [B3]
            try:
                self._process_conversation(conv, at, report)
            except Exception as exc:
                report.errors.append(f"{conv.conv_id}:{type(exc).__name__}")
                if _is_breaker_error(exc):
                    raise  # §5.2 熔断类：交编排器（fatal/M5 全停）
                self._log_adapter_error(f"poll:{conv.conv_id}", exc)
        # 待发箱超时扫描（每轮一次；时段外到期条目自然并入下一轮 [B10/s6]）
        report.timeout_alert = scan_outbox_timeout(self._db, self._config(), now=at)
        return report

    # ---- 对账任务（§13：低频，每日一次 + 熔断恢复后；编排器调度）----

    def reconcile_once(self, now: datetime | None = None) -> ReconcileReport | None:
        """取回开放 intent 相关会话的我方消息对账（只对账，绝不重发 [B4/§13]）。"""
        at = self._clock() if now is None else as_utc(now)
        open_intents = self._intents.load_open_intents()
        if not open_intents:
            return None
        conv_ids = {i.target_ref for i in open_intents if i.kind in ("reply", "resume")}
        greeting_jobs = {i.target_ref for i in open_intents if i.kind == "greeting"}
        if greeting_jobs:  # greeting intent 按「会话关联岗位」反查会话 [F1]
            placeholders = ", ".join("?" for _ in greeting_jobs)
            rows = self._db.query(
                f"SELECT conv_id FROM conversations WHERE job_id IN ({placeholders})",
                sorted(greeting_jobs),
            )
            conv_ids.update(str(r["conv_id"]) for r in rows)
        if not conv_ids:
            return None
        earliest = min(i.created_at for i in open_intents)
        earliest_dt = _parse_ts(earliest) if earliest else None
        since = earliest_dt - MATCH_WINDOW if earliest_dt is not None else None
        out_messages: list[Message] = []
        for conv_id in sorted(conv_ids):
            try:
                fetched = self._adapter.get_messages(conv_id, since)
            except Exception as exc:
                if _is_breaker_error(exc):
                    raise  # §5.2 熔断类：交编排器
                self._log_adapter_error(f"reconcile:{conv_id}", exc)
                continue
            self._ensure_conversation_row_by_id(conv_id, at)
            for msg in fetched:
                self._insert_message(msg)
                if msg.direction == "out":
                    out_messages.append(msg)
        report = self._intents.reconcile(out_messages, open_intents)
        self._apply_reconcile(report, None)
        if report.unmatched_intents:
            logger.warning(
                "[§13] %d 个 intent 未找到对应消息，保持 unknown 不重发：%s",
                len(report.unmatched_intents),
                report.unmatched_intents,
            )
        return report

    # ---- 待发箱命令（I6 approve_outbox / skip_outbox 的执行体）----

    def approve_outbox_entry(self, entry_id: int, now: datetime | None = None) -> str:
        """用户确认发送 [B10/§8.3]：**不计自动发送计数**（人工行为）。

        - pending/stale → approved；dry_run 下只记拟发送（不实际发送）[N2]；
        - 正式模式：登记 reply intent（pending）并入队发送，成功 resolve sent；
          ``auto_sent_count`` 不变（count_auto=False）。
        """
        row = self._db.query_one(
            "SELECT conv_id, draft_text, state FROM outbox WHERE id = ?", (entry_id,)
        )
        if row is None:
            return "not_found"
        if row["state"] not in ("pending", "stale"):
            return "invalid_state"
        conv_id, draft = str(row["conv_id"]), str(row["draft_text"])
        if dryrun.is_dry_run(self._db, self._config()):
            dryrun.record_planned_send(self._db, "reply", conv_id, payload_digest(draft))
            self._db.execute("UPDATE outbox SET state = 'approved' WHERE id = ?", (entry_id,))
            return "approved_dryrun"
        intent_id = self._intents.begin("reply", conv_id, payload_digest(draft), dry_run=False)
        self._db.execute("UPDATE outbox SET state = 'approved' WHERE id = ?", (entry_id,))
        self._enqueue_reply(
            f"approve:{entry_id}",
            self._make_reply_task(conv_id, draft, intent_id, count_auto=False),
        )
        return "approved_queued"

    def skip_outbox_entry(self, entry_id: int) -> str:
        """用户跳过待发箱条目（pending/stale → skipped）。"""
        row = self._db.query_one("SELECT state FROM outbox WHERE id = ?", (entry_id,))
        if row is None:
            return "not_found"
        if row["state"] not in ("pending", "stale"):
            return "invalid_state"
        self._db.execute("UPDATE outbox SET state = 'skipped' WHERE id = ?", (entry_id,))
        return "skipped"

    # ---- 内部：单会话处理 ----

    def _process_conversation(self, conv: Conversation, now: datetime, report: PollReport) -> None:
        last_row = self._db.query_one(
            "SELECT sent_at FROM messages WHERE conv_id = ? ORDER BY sent_at DESC LIMIT 1",
            (conv.conv_id,),
        )
        last_ts = _parse_ts(str(last_row["sent_at"])) if last_row is not None else None
        if last_ts is not None and as_utc(conv.last_message_at) <= last_ts:
            return  # 无新消息（列表时间未越过已存最新消息）
        fetched = self._adapter.get_messages(conv.conv_id, last_ts)
        fresh = sorted(
            (m for m in fetched if not self._message_exists(m.platform_msg_id)),
            key=lambda m: (as_utc(m.sent_at), m.platform_msg_id),
        )
        if not fresh:
            return
        self._ensure_conversation_row(conv, now)
        for msg in fresh:
            self._insert_message(msg)
        report.new_messages += len(fresh)
        # 我方消息对账（接管检测 [B3/G6/F1]；is_ours_auto 回填）
        out_new = [m for m in fresh if m.direction == "out"]
        if out_new:
            open_intents = self._intents.load_open_intents()
            self._apply_reconcile(self._intents.reconcile(out_new, open_intents), report)
        # 六级路由（仅入站消息；出站消息由对账路径处理）
        fresh_ids = {m.platform_msg_id for m in fresh}
        context = self._recent_context(conv.conv_id, exclude_ids=fresh_ids)
        in_new = [m for m in fresh if m.direction == "in"]
        if in_new:
            # 草稿过期 [§8.3]：条目**存在期间**再来新消息 → 既有 pending 标记
            # stale（先标记后路由——本轮新产生的草稿不被同批消息标记）
            report.stale_marked += self._db.execute(
                "UPDATE outbox SET state = 'stale' WHERE conv_id = ? AND state = 'pending'",
                (conv.conv_id,),
            )
        for msg in fresh:
            context.append(msg)
            if msg.direction != "in":
                continue
            try:
                category, action = self._route_message(conv, msg, context, now, report)
            except Exception as exc:  # 单条失败不中断循环 [NFR2]
                report.errors.append(f"route:{msg.platform_msg_id}:{type(exc).__name__}")
                logger.warning("[NFR2] 消息 %s 路由失败跳过", msg.platform_msg_id, exc_info=True)
                continue
            self._record_routing(msg.platform_msg_id, category, action, now)
            report.routed += 1

    # ---- 内部：六级路由 ----

    def _route_message(
        self,
        conv: Conversation,
        msg: Message,
        context: list[Message],
        now: datetime,
        report: PollReport,
    ) -> tuple[str, str]:
        """路由单条入站消息（§8 表，命中即止）；返回 (category, action)。"""
        msg_type = msg.msg_type
        if msg_type == MsgType.SYSTEM:
            return "system", f"state_inference:{infer_system_status(msg.text)}"
        if msg_type == MsgType.CONTACT_REQUEST_CARD:
            self._add_outbox(conv.conv_id, _PLACEHOLDER_CONTACT, "manual", now, report)
            self._emit(
                "CONTACT_REQUEST",
                {"conv_id": conv.conv_id, "platform_msg_id": msg.platform_msg_id},
            )
            return "contact_request", "outbox_manual+CONTACT_REQUEST"
        if msg_type == MsgType.RESUME_REQUEST_CARD:
            return "resume_request", self._handle_resume_request(conv, msg, context, now, report)
        if msg_type == MsgType.INTERVIEW_CARD:
            draft_action = self._draft_to_outbox(conv, context, "key", now, report)
            self._emit(
                "INTERVIEW_INVITE",
                {"conv_id": conv.conv_id, "platform_msg_id": msg.platform_msg_id},
            )
            return "interview_invite", f"{draft_action}+INTERVIEW_INVITE"
        if msg_type in (MsgType.IMAGE, MsgType.VOICE, MsgType.FILE, MsgType.JOB_CARD):
            self._add_outbox(conv.conv_id, _PLACEHOLDER_MULTIMODAL, "manual", now, report)
            self._emit(
                "MULTIMODAL_PENDING",
                {
                    "conv_id": conv.conv_id,
                    "platform_msg_id": msg.platform_msg_id,
                    "msg_type": msg_type.value,
                },
            )
            return "multimodal", "outbox_manual+MULTIMODAL_PENDING"
        return self._route_text(conv, msg, context, now, report)

    def _route_text(
        self,
        conv: Conversation,
        msg: Message,
        context: list[Message],
        now: datetime,
        report: PollReport,
    ) -> tuple[str, str]:
        """路由序 6：文本 → AI 四分类（卡片类已在上方拦截，不进 AI [§9.2]）。"""
        result = self._classify_fn(list(context))
        if "AIBudgetExhausted" in result.reason:  # classify 兜底 reason 含异常类名
            self._notify_budget_exhausted(now)
        category = result.category if result.category in _AI_CATEGORIES else "key"
        if category == "no_reply":
            return "no_reply", "skip_no_reply"  # 不发送不计数不告警 [B2]
        if category == "low_risk":
            return "low_risk", self._handle_low_risk(conv, context, now, report)
        if category == "key":
            draft_action = self._draft_to_outbox(conv, context, "key", now, report)
            self._emit(
                "KEY_QUESTION",
                {
                    "conv_id": conv.conv_id,
                    "platform_msg_id": msg.platform_msg_id,
                    "reason": result.reason,
                },
            )
            return "key", f"{draft_action}+KEY_QUESTION"
        return "positive_response", self._handle_positive_response(conv, context, now, report)

    def _handle_low_risk(
        self, conv: Conversation, context: list[Message], now: datetime, report: PollReport
    ) -> str:
        """§8.2 low_risk：草拟→护栏→通过→入队发送计 I8；受限/护栏命中→待发箱。"""
        config = self._config()
        allowed, reason = self._session.can_auto_send(conv.conv_id, "message")
        rampup_active = rampup.is_rampup_active(self._db, config)
        if not allowed or rampup_active:
            # 转人工/达上限仍草拟进待发箱（§8.3）；放量期低风险自动回复关（§13）
            action = self._draft_to_outbox(conv, context, "low_risk", now, report)
            detail = "rampup" if rampup_active else reason
            return f"{action}:no_auto:{detail}"
        draft, failure = self._safe_draft(conv, context, "low_risk", now)
        if draft is None:
            self._add_outbox(conv.conv_id, _PLACEHOLDER_AI_FAILED, "manual", now, report)
            return f"outbox_manual:{failure}"
        if dryrun.is_dry_run(self._db, config):
            dryrun.record_planned_send(self._db, "reply", conv.conv_id, payload_digest(draft))
            report.planned_dryrun += 1
            return "planned_reply_dryrun"
        guard = self._guard_fn(draft)
        if not guard.ok:
            # 护栏命中 → 降级待发箱并标注原因 [§9.4/B12]；草稿保留供人工放行
            self._add_outbox(conv.conv_id, draft, "ai", now, report)
            note = "; ".join(v.reason for v in guard.reasons)[:120]
            return f"outbox_draft:guard_rejected:{note}"
        intent_id = self._intents.begin("reply", conv.conv_id, payload_digest(draft), dry_run=False)
        self._enqueue_reply(
            f"reply:{conv.conv_id}", self._make_reply_task(conv.conv_id, draft, intent_id)
        )
        report.auto_queued += 1
        return "auto_send_reply"

    def _handle_resume_request(
        self,
        conv: Conversation,
        msg: Message,
        context: list[Message],
        now: datetime,
        report: PollReport,
    ) -> str:
        """路由序 3：RESUME_REQUEST_CARD（FR3.8 开关；卡片不进 AI）。"""
        if self._resume_auto_allowed(conv.conv_id):
            return self._auto_send_resume(conv.conv_id, now, report)
        self._add_outbox(conv.conv_id, _PLACEHOLDER_RESUME, "manual", now, report)
        self._emit(
            "KEY_QUESTION",
            {
                "conv_id": conv.conv_id,
                "platform_msg_id": msg.platform_msg_id,
                "reason": "resume_request_manual",
            },
        )
        return "outbox_manual+KEY_QUESTION"

    def _handle_positive_response(
        self, conv: Conversation, context: list[Message], now: datetime, report: PollReport
    ) -> str:
        """§8.2 positive_response：FR3.8 开→自动投简历；关/受限→草拟+中级告警。"""
        if self._resume_auto_allowed(conv.conv_id):
            return self._auto_send_resume(conv.conv_id, now, report)
        draft_action = self._draft_to_outbox(conv, context, "positive_response", now, report)
        self._emit("OFFER_SIGNAL", {"conv_id": conv.conv_id, "reason": "positive_response"})
        return f"{draft_action}+OFFER_SIGNAL"

    def _resume_auto_allowed(self, conv_id: str) -> bool:
        """FR3.8 开 ∧ 非放量期（§13 期间 FR3.8 关）∧ 会话简历额度可用（I8）。"""
        config = self._config()
        if not config.messaging.positive_response_resume_enabled:
            return False
        if rampup.is_rampup_active(self._db, config):
            return False
        return self._session.can_auto_send(conv_id, "resume")[0]

    def _auto_send_resume(self, conv_id: str, now: datetime, report: PollReport) -> str:
        """附件简历自动投（§8.1 resume 独立计数；dry_run 记拟发送 [N2]）。"""
        if dryrun.is_dry_run(self._db, self._config()):
            dryrun.record_planned_send(self._db, "resume", conv_id, _RESUME_DIGEST)
            report.planned_dryrun += 1
            return "planned_resume_dryrun"
        intent_id = self._intents.begin("resume", conv_id, _RESUME_DIGEST, dry_run=False)
        self._enqueue_reply(f"resume:{conv_id}", self._make_resume_task(conv_id, intent_id))
        report.auto_queued += 1
        return "auto_send_resume"

    # ---- 内部：草拟与待发箱 ----

    def _draft_to_outbox(
        self,
        conv: Conversation,
        context: list[Message],
        category: str,
        now: datetime,
        report: PollReport,
    ) -> str:
        """AI 草拟进待发箱；失败降级 manual 占位（返回动作码 draft/manual_fallback）。"""
        draft, _failure = self._safe_draft(conv, context, category, now)
        if draft is None:
            self._add_outbox(conv.conv_id, _PLACEHOLDER_AI_FAILED, "manual", now, report)
            return "outbox_manual_fallback"
        self._add_outbox(conv.conv_id, draft, "ai", now, report)
        return "outbox_draft"

    def _safe_draft(
        self, conv: Conversation, context: list[Message], category: str, now: datetime
    ) -> tuple[str | None, str]:
        """草拟（异常吞掉）；返回 (草稿, 失败码 ok/ai_budget_exhausted/ai_error)。"""
        try:
            text = self._draft_fn(self._conv_context(conv, tuple(context[-10:])), category)
        except AIBudgetExhausted:
            self._notify_budget_exhausted(now)  # 存量草拟转人工 + 中级告警 [B13]
            return None, "ai_budget_exhausted"
        except Exception:
            logger.warning("[FR9.3] AI 草拟失败，降级待发箱人工处理", exc_info=True)
            return None, "ai_error"
        return text, "ok"

    def _add_outbox(
        self, conv_id: str, draft_text: str, source: str, now: datetime, report: PollReport
    ) -> int:
        report.outbox_added += 1
        return self._db.execute_returning_id(
            "INSERT INTO outbox (conv_id, draft_text, source, state, created_at)"
            " VALUES (?, ?, ?, 'pending', ?)",
            (conv_id, draft_text, source, utc_str(now)),
        )

    def _notify_budget_exhausted(self, now: datetime) -> None:
        """AI_BUDGET_EXHAUSTED 中级告警，每日最多一次（B13；on_event 钩子的兜底）。"""
        today = beijing_date_str(now)
        if runstate.get_state(self._db, KEY_AI_BUDGET_NOTIFIED) == today:
            return
        runstate.set_state(self._db, KEY_AI_BUDGET_NOTIFIED, today)
        events.emit(
            self._db,
            "AI_BUDGET_EXHAUSTED",
            {"date": today, "action": "stop_auto_reply_drafts_to_manual"},
        )

    # ---- 内部：发送任务（编排器 REPLY 优先级执行）----

    def _enqueue_reply(self, name: str, task: TaskFn) -> None:
        if self._submit is None:
            raise RuntimeError("Messenger 未绑定发送入队口（set_reply_submitter / 构造注入）")
        self._submit(name, task)

    def _make_reply_task(
        self, conv_id: str, text: str, intent_id: int, *, count_auto: bool = True
    ) -> TaskFn:
        def run() -> None:
            outcome = self._send_with_retry(lambda: self._adapter.send_reply(conv_id, text))
            self._settle_intent(conv_id, "message", intent_id, outcome, count_auto=count_auto)

        return run

    def _make_resume_task(self, conv_id: str, intent_id: int) -> TaskFn:
        def run() -> None:
            outcome = self._send_with_retry(lambda: self._adapter.send_resume(conv_id))
            self._settle_intent(conv_id, "resume", intent_id, outcome, count_auto=True)

        return run

    def _send_with_retry(self, send: Callable[[], SendOutcome]) -> SendOutcome:
        """TransientError 指数退避重试 ≤2 次，仍失败按 FAILED 落库（§5.2）。"""
        backoff = 1.0
        for attempt in range(3):
            try:
                return send()
            except Exception as exc:
                if classify_adapter_error(exc) != "transient":
                    raise  # 登录/风控/适配器失效等交编排器统一处置 [§5.2]
                if attempt == 2:
                    return SendOutcome.FAILED
                self._sleep(backoff)
                backoff *= 2
        raise AssertionError("unreachable")

    def _settle_intent(
        self,
        conv_id: str,
        kind: Literal["message", "resume"],
        intent_id: int,
        outcome: SendOutcome,
        *,
        count_auto: bool,
    ) -> None:
        """发送结果落库：sent→计数（I8）；failed→不重试（NFR2）；unknown→保持 pending。"""
        if outcome == SendOutcome.SENT:
            self._intents.resolve(intent_id, "sent")
            if count_auto:
                self._session.record_auto_send(conv_id, kind)
        elif outcome == SendOutcome.FAILED:
            self._intents.resolve(intent_id, "failed")
        else:  # UNKNOWN / PENDING：保持 pending → 启动恢复 unknown → 对账 [B4]
            logger.warning("[B4] intent %s 发送结果未确认（%s），保持待对账", intent_id, outcome)

    # ---- 内部：对账结果回填 ----

    def _apply_reconcile(self, report: ReconcileReport, poll: PollReport | None) -> None:
        """reconcile 报告落库：命中回填 is_ours_auto=1；匹配不上→转人工 [B3]。"""
        for match in report.matched:
            self._db.execute(
                "UPDATE messages SET is_ours_auto = 1 WHERE platform_msg_id = ?",
                (match.platform_msg_id,),
            )
        for msg_id in report.manual_messages:
            self._db.execute(
                "UPDATE messages SET is_ours_auto = 0 WHERE platform_msg_id = ?",
                (msg_id,),
            )
            row = self._db.query_one(
                "SELECT conv_id FROM messages WHERE platform_msg_id = ?", (msg_id,)
            )
            if row is not None:
                self._session.mark_manual_takeover(str(row["conv_id"]), "digest_miss")
                if poll is not None:
                    poll.takeovers += 1

    # ---- 内部：上下文与落库 ----

    def _conv_context(self, conv: Conversation, recent: tuple[Message, ...]) -> ConversationContext:
        """draft_reply 上下文（§5.5 I4）：简历快照 + 已确认方向 + 目标城市 + 关联岗位。"""
        job = conv.job_ref
        return ConversationContext(
            conv_id=conv.conv_id,
            peer_name=conv.peer_name,
            job_title=job.title if job is not None else "",
            company_name=job.company_name if job is not None else "",
            target_city=self._config().target.city,
            directions=_confirmed_directions(self._db),
            resume=_current_resume(self._db),
            recent_messages=recent,
        )

    def _recent_context(self, conv_id: str, *, exclude_ids: set[str]) -> list[Message]:
        """会话已有消息尾部（≤9 条，时间升序）——分类/草拟的滚动上下文底座。"""
        rows = self._db.query(
            "SELECT * FROM messages WHERE conv_id = ? ORDER BY sent_at DESC LIMIT 9",
            (conv_id,),
        )
        ctx = [_row_to_message(r) for r in rows if r["platform_msg_id"] not in exclude_ids]
        ctx.reverse()
        return ctx

    def _ensure_conversation_row(self, conv: Conversation, now: datetime) -> None:
        """会话行 UPSERT（messages/outbox 外键依赖；job_id 只在原值为空时补写）。"""
        job_id = conv.job_ref.job_id if conv.job_ref is not None else None
        self._db.execute(
            "INSERT INTO conversations (conv_id, peer_name, job_id, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(conv_id) DO UPDATE SET"
            " updated_at = excluded.updated_at,"
            " peer_name = CASE WHEN excluded.peer_name != ''"
            " THEN excluded.peer_name ELSE conversations.peer_name END,"
            " job_id = CASE WHEN conversations.job_id IS NULL"
            " THEN excluded.job_id ELSE conversations.job_id END",
            (conv.conv_id, conv.peer_name, job_id, utc_str(now)),
        )

    def _ensure_conversation_row_by_id(self, conv_id: str, now: datetime) -> None:
        """对账路径的轻量建行（无 Conversation 对象时；peer 留空待轮询补全）。"""
        row = self._db.query_one("SELECT conv_id FROM conversations WHERE conv_id = ?", (conv_id,))
        if row is None:
            self._db.execute(
                "INSERT INTO conversations (conv_id, peer_name, updated_at) VALUES (?, '', ?)",
                (conv_id, utc_str(now)),
            )

    def _insert_message(self, msg: Message) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO messages (platform_msg_id, conv_id, direction, sender,"
            " msg_type, text, sent_at, is_ours_auto)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                msg.platform_msg_id,
                msg.conv_id,
                msg.direction,
                msg.sender,
                msg.msg_type.value,
                msg.text,
                utc_str(as_utc(msg.sent_at)),
            ),
        )

    def _message_exists(self, platform_msg_id: str) -> bool:
        return (
            self._db.query_one(
                "SELECT 1 AS x FROM messages WHERE platform_msg_id = ?", (platform_msg_id,)
            )
            is not None
        )

    def _record_routing(
        self, platform_msg_id: str, category: str, action: str, now: datetime
    ) -> None:
        self._db.execute_returning_id(
            "INSERT INTO msg_routing (platform_msg_id, category, action, handled_at)"
            " VALUES (?, ?, ?, ?)",
            (platform_msg_id, category, action, utc_str(now)),
        )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        events.emit(self._db, event_type, payload)

    def _config(self) -> Config:
        return self._config_provider()

    @staticmethod
    def _log_adapter_error(where: str, exc: Exception) -> None:
        logger.warning("[NFR2] %s 站点操作失败（%s：%s）", where, type(exc).__name__, exc)


# ---- 模块级小工具 ----

#: §5.2 表中"熔断：停全部任务"的异常类（其余按 NFR2 跳过继续）
_BREAKER_ERROR_KINDS = frozenset({"login_required", "risk_detected", "adapter_broken"})


def _is_breaker_error(exc: Exception) -> bool:
    return classify_adapter_error(exc) in _BREAKER_ERROR_KINDS


def _parse_ts(ts: str) -> datetime | None:
    """库内时间戳文本 → UTC datetime（非法返回 None，保守处理）。"""
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return as_utc(parsed)


def _row_to_message(row: Any) -> Message:
    """messages 行 → Message（is_ours_auto 保三态：NULL/0/1）。"""
    sent_at = _parse_ts(str(row["sent_at"]))
    return Message(
        platform_msg_id=str(row["platform_msg_id"]),
        conv_id=str(row["conv_id"]),
        direction=cast(Literal["in", "out"], str(row["direction"])),
        sender=cast(Literal["hr", "me", "system"], str(row["sender"])),
        msg_type=MsgType(str(row["msg_type"])),
        text=str(row["text"]),
        sent_at=sent_at if sent_at is not None else datetime.min.replace(tzinfo=UTC),
        is_ours_auto=None if row["is_ours_auto"] is None else bool(row["is_ours_auto"]),
    )


def _current_resume(db: Database) -> ResumeSnapshot:
    """当前结构化简历快照（无版本 → 空快照：draft_reply 声明不得虚构）。"""
    row = db.query_one("SELECT structured_json FROM resume_snapshots WHERE is_current = 1")
    if row is None:
        return ResumeSnapshot()
    try:
        data = json.loads(str(row["structured_json"]))
    except ValueError:
        return ResumeSnapshot()
    if not isinstance(data, dict):
        return ResumeSnapshot()

    def _strings(key: str) -> list[str]:
        value = data.get(key)
        return [str(v) for v in value] if isinstance(value, list) else []

    return ResumeSnapshot(
        education=_strings("education"),
        experience=_strings("experience"),
        skills=_strings("skills"),
        projects=_strings("projects"),
    )


def _confirmed_directions(db: Database) -> tuple[str, ...]:
    """已确认职位方向的关键词（扁平；无 → 空表）。"""
    rows = db.query("SELECT keywords_json FROM job_directions WHERE is_confirmed = 1")
    keywords: list[str] = []
    for row in rows:
        try:
            data = json.loads(str(row["keywords_json"]))
        except ValueError:
            continue
        if isinstance(data, list):
            keywords.extend(str(k) for k in data)
    return tuple(keywords)
