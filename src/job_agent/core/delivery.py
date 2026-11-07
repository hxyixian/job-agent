"""投递流水线（设计文档 §7 十步；Stage4 S4c）。

§7 十步 → 本模块落点（编号与设计文档一致，逐条核对的权威索引）：

::

    [调度闸门] Gate.can_deliver ∧ Gate.can_browse("search")（§5.5 I7）；
                平台限额日（run_state.platform_limit_date=当日）当日跳过
                投递步 [B6]；步内每职位再查 can_deliver（限额连续 [G1]）
    步 1 search_jobs(filters, page)：成功后 quota.record_browse("search")
            [G3 翻页计入 search_page_hourly]
    步 2 列表过滤：dedup_check(I3) ∧ exclusion_match(I3) ∧ is_anonymous
            （匿名卡跳过留痕 evaluations.skipped_by='anonymous_company'
            [v13/F12]；薪资过滤已废止 [R13]）
    步 3 get_job_detail：成功后 quota.record_browse("detail") [G3]（不分
            dry_run，浏览即暴露）+ jobs 缓存 UPSERT
    步 4 详情过滤：exclusion_match(JobDetail)——排除组 scope 覆盖
            jd_text（JD 正文）与 tags（福利标签）；v1 无独立"经验要求"
            配置面，该维度由预置排除组承载（见 §4 match 节）
    步 5 evaluations 缓存：job_id 最新 verdict 非 NULL 行（dry_run 与正式
            共用缓存）；未命中 → evaluate_job（§5.5 I4，失败返回
            verdict=skip+ai_error 不抛出）→ 入缓存
    步 6 判定 judge_decision：verdict≠skip ∧ score≥min_score，矛盾从保守
            [FR4.2]；ai.neutral_invest=False 时 neutral 亦跳过；
            low_score/verdict_skip 由本层回填 evaluations.skipped_by
    步 7 dry_run 分支：SendStateMachine.begin(dry_run=True) 单一入口
            [v16 裁决——不使用 dryrun.record_planned_send] → 写
            applications(dry_run=1)；不 resolve、不写 companies [N2]
    步 8 发送：begin(dry_run=False) → adapter.send_greeting（=点击
            「立即沟通」，平台自动发已同步模板 [R12/A1]）→ resolve
            sent/failed；UNKNOWN/PENDING 留 pending（B4：重启→unknown，
            只对账不重发）
    步 9 applications 落库 + companies 去重登记（permanent→blocked_until
            NULL；cooldown→now+cooldown_days 天 [S2a]；FAILED 不登记——
            未发出=无暴露；UNKNOWN/不确定登记——宁可不重投 [FR3.6]）
    步 10 节流：max(随机 action_delay_range_s, Gate.min_interval_remaining
            ("detail")) [H2 下限约束] → 下一职位

**F1 每日首次 send_greeting 成功后的招呼语核验**（试运行跳过——无实际
发送可读回，首个正式运行日首次投递=核验点）：经 adapter.get_messages
读回该会话我方首条消息（direction=out 中 sent_at 最早者），与当前
active 模板 :func:`normalize_greeting_text`（去空白/标点）比较——
不一致 → emit GREETING_MISMATCH（中级）+ 停止本轮投递 + **不触发
manual_takeover**（本层结构上不调用 mark_manual_takeover；该消息在
S2b reconcile 按"岗位+±10min"天然命中 greeting intent [F1]）；一致 →
ConversationState.mark_greeting_probe（当日不再核验）；读回失败/无我方
消息 → inconclusive（不标记、不停投）。引擎级持续暂停由 S4d 依据事件
执行（本层负责停当轮+事件）。

错误处理（§5.2 错误表；异常树 = :mod:`job_agent.adapters.base`，S4a 已
交付，直接 import 做分类分派）：

- LoginRequiredError → 停轮 + LOGIN_EXPIRED（高）；
- RiskDetectedError → rampup.handle_risk_signal（M5 全停+高级事件+浏览
  档位回首档）→ 停轮；
- AdapterBrokenError → 停轮 + ADAPTER_BROKEN（高）；
- PlatformLimitError → run_state.platform_limit_date=当日 +
  PLATFORM_LIMIT（中）→ 停轮（消息处理照常，属 S4d）[B6]；
- TransientError → 指数退避重试 ≤2 次，仍失败按 FAILED 落库（搜索步
  则停轮）；
- 其他 AdapterError → 当前职位标记失败跳过，循环继续 [NFR2]；
- 非 AdapterError 错误（bug）→ 原样上抛，不吞。

被动招呼计入投递限额 [D0]：由 send_intents 计数口径天然覆盖（quota
不分 kind 计数），本模块无特殊逻辑。
"""

from __future__ import annotations

import json
import random
import time
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any, Literal, NamedTuple, TypeVar, cast

from job_agent.adapters.base import (
    AdapterBrokenError,
    AdapterError,
    LoginRequiredError,
    PlatformLimitError,
    RiskDetectedError,
    SearchFilters,
    SiteAdapter,
    TransientError,
)
from job_agent.ai.evaluate import evaluate_job
from job_agent.ai.types import Evaluation, Verdict
from job_agent.config import Config, PreferenceRule
from job_agent.core import dryrun, events, rampup, runstate
from job_agent.core.quota import Quota
from job_agent.core.rules import DedupDecision, dedup_check, exclusion_match, is_anonymous
from job_agent.core.scheduler import (
    ActionKind,
    Gate,
    GateResult,
    LimitsView,
    QuotaAPI,
    utc_clock,
)
from job_agent.core.session import ConversationState
from job_agent.core.state_machine import SendStateMachine, payload_digest
from job_agent.db import Database
from job_agent.models import (
    JobDetail,
    JobRef,
    ResumeSnapshot,
    RiskSignal,
    SendOutcome,
)
from job_agent.resume.parser import current_resume_snapshot
from job_agent.timeutil import CST, as_utc, beijing_date_str, utc_str

__all__ = [
    "ACTION_FAILED",
    "ACTION_SENT",
    "ACTION_SKIPPED_AI_ERROR",
    "ACTION_SKIPPED_ALREADY_APPLIED",
    "ACTION_SKIPPED_ANONYMOUS",
    "ACTION_SKIPPED_DEDUP",
    "ACTION_SKIPPED_DRYRUN",
    "ACTION_SKIPPED_EXCLUSION",
    "ACTION_SKIPPED_LOW_SCORE",
    "ACTION_SKIPPED_VERDICT",
    "ACTION_UNKNOWN",
    "DETAIL_FAILED",
    "KEY_PLATFORM_LIMIT",
    "DeliveryPipeline",
    "DeliveryReport",
    "GreetingTemplate",
    "JobOutcome",
    "Judgment",
    "judge_decision",
    "normalize_greeting_text",
]

# run_state 键：平台限额日（§7 B6："当日停止投递"的当日标记，北京日文本）
KEY_PLATFORM_LIMIT = "platform_limit_date"

# 步 2/4/6 跳过动作码（报告与测试断言的稳定词汇）
ACTION_SENT = "sent"
ACTION_FAILED = "failed"
ACTION_UNKNOWN = "unknown"
ACTION_SKIPPED_DRYRUN = "skipped_dryrun"
ACTION_SKIPPED_ANONYMOUS = "skipped_anonymous"
ACTION_SKIPPED_EXCLUSION = "skipped_exclusion"
ACTION_SKIPPED_DEDUP = "skipped_dedup"
ACTION_SKIPPED_LOW_SCORE = "skipped_low_score"
ACTION_SKIPPED_VERDICT = "skipped_verdict_skip"
ACTION_SKIPPED_AI_ERROR = "skipped_ai_error"
ACTION_SKIPPED_ALREADY_APPLIED = "skipped_already_applied"
DETAIL_FAILED = "detail_failed"

# TransientError 指数退避基秒（1s → 2s，重试 ≤2 次，§5.2）
_TRANSIENT_BACKOFF_BASE_S = 1.0

# 登录/风控/断件：发送结果不确定 → intent 留 pending（B4 对账），run 级停
_RUN_LEVEL_LABELS = frozenset({"login_required", "risk", "broken"})

Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]
_T = TypeVar("_T")

Evaluator = Callable[[JobDetail, ResumeSnapshot, list[PreferenceRule]], Evaluation]


def _label_of(exc: AdapterError) -> str:
    """§5.2 具体异常 → 行为标签（isinstance 链，特异性优先）。"""
    if isinstance(exc, LoginRequiredError):
        return "login_required"
    if isinstance(exc, RiskDetectedError):
        return "risk"
    if isinstance(exc, AdapterBrokenError):
        return "broken"
    return "platform_limit"


def normalize_greeting_text(text: str) -> str:
    """F1 核验归一化：去全部空白与标点（Unicode P 开头类）后 casefold。

    平台可能对招呼语做空白/标点规整（或模板被平台编辑器改写），逐字
    比较会误报；语义文本（含数字/字母大小写归并）一致即视为同步。
    """
    kept = [ch for ch in text if not ch.isspace() and not unicodedata.category(ch).startswith("P")]
    return "".join(kept).casefold()


@dataclass(frozen=True)
class Judgment:
    """§7 步 6 判定结果（``skipped_by`` 为 None = 投递）。"""

    invest: bool
    skipped_by: str | None


def judge_decision(evaluation: Evaluation, min_score: int, neutral_invest: bool) -> Judgment:
    """判定（纯函数）：invest = verdict≠skip ∧ score≥min_score（矛盾从保守）。

    - ``verdict=skip`` 优先归因 ``verdict_skip``（即使 score 达标——矛盾
      从保守 [FR4.2]）；
    - ``score < min_score`` → ``low_score``（即使 verdict=recommend）；
    - ``neutral`` 且 ``ai.neutral_invest=False`` → 按判定层跳过处理，
      归因 ``verdict_skip``（§9.1 枚举无 neutral 专属码，取最近语义）；
    - ``ai_error``（evaluate_job 失败路径）的归因保留由调用方组合
      （evaluation.skipped_by 优先于本判定）。
    """
    if evaluation.verdict == "skip":
        return Judgment(False, "verdict_skip")
    if evaluation.score < min_score:
        return Judgment(False, "low_score")
    if evaluation.verdict == "neutral" and not neutral_invest:
        return Judgment(False, "verdict_skip")
    return Judgment(True, None)


class GreetingTemplate(NamedTuple):
    """当前 active 模板（greeting_templates.is_current=1）。"""

    content: str
    version: int


@dataclass
class JobOutcome:
    """单职位在本轮流水线的处置结果（detail 为稳定 snake_case 码）。"""

    job_id: str
    company_key: str
    action: str
    detail: str = ""


@dataclass
class DeliveryReport:
    """一次 :meth:`DeliveryPipeline.run_page` 的结果报告。

    ``stop_reason``：None=页内职位全部走完；否则为停止码——
    ``platform_limit_today``（B6 当日停投）/ Gate 稳定码（out_of_delivery_
    schedule / hourly_limit_reached / daily_limit_reached / circuit_breaker /
    prerequisites_not_met）/ ``browse_search_limit`` / ``browse_detail_limit``
    （[F10] 与 PLATFORM_LIMIT 严格区分）/ ``greeting_mismatch`` [F1] /
    ``platform_limit`` / ``login_expired`` / ``risk_pause`` /
    ``adapter_broken`` / ``transient_exhausted`` / ``adapter_error`` /
    ``greeting_template_missing`` / ``resume_missing``。

    ``greeting_probe``：``skipped_dry_run``（试运行不核验 [F1]）/
    ``not_reached``（当日尚无成功发送）/ ``match`` / ``mismatch`` /
    ``unreadable``（读回失败，不标记）/ ``already_done``（当日已核验）。
    """

    page: int
    jobs_seen: int = 0
    dry_run: bool = False
    outcomes: list[JobOutcome] = field(default_factory=list)
    stop_reason: str | None = None
    greeting_probe: str = "not_reached"
    eval_cache_hits: int = 0
    evaluator_calls: int = 0
    events_emitted: list[str] = field(default_factory=list)
    #: 已发过 BROWSE_LIMIT_REACHED 的浏览类别（run 内去重，防事件风暴）
    browse_limit_events: set[str] = field(default_factory=set)

    def count(self, action: str) -> int:
        """指定动作码的职位数。"""
        return sum(1 for o in self.outcomes if o.action == action)


class _AdapterFailure(Exception):
    """适配器调用失败（分类标签 + 原始异常；模块内部流转用）。"""

    def __init__(self, label: str, exc: BaseException) -> None:
        super().__init__(f"{label}: {exc!r}")
        self.label = label
        self.exc = exc


@dataclass(frozen=True)
class _CachedEval:
    """evaluations 行的内存视图（步 5 产物）。"""

    row_id: int
    evaluation: Evaluation


class DeliveryPipeline:
    """§7 十步流水线（每职位顺序执行；单职位失败不中断循环 [NFR2]）。

    装配：引擎传入 db/config/adapter（§5.2 ``SiteAdapter``，S4a 交付的
    adapters.base 契约，Mock/Zhipin 同注册机制）；gate 缺省以内部
    ``Quota`` 实例绑定 I1 四函数构造（I7）。时钟/睡眠/随机数/评估器全部
    可注入（离线测试）；``clock`` 的 naive 值一律视为 UTC（timeutil 契约）。
    """

    def __init__(
        self,
        db: Database,
        config: Config,
        adapter: SiteAdapter,
        *,
        gate: Gate | None = None,
        clock: Clock = utc_clock,
        sleep: Sleeper = time.sleep,
        rng: random.Random | None = None,
        evaluator: Evaluator | None = None,
        transient_retries: int = 2,
    ) -> None:
        self._db = db
        self._config = config
        self._adapter = adapter
        self._clock = clock
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()
        self._quota = Quota(db, config)
        self._gate = (
            gate
            if gate is not None
            else Gate(
                config.delivery,
                QuotaAPI(
                    used_quota=self._quota.used,
                    browse_used=self._quota.browse_used,
                    # Limits（frozen dataclass，只读属性）结构化满足 LimitsView
                    # 协议（五个 int 属性同名同型）；mypy 对"可变协议属性 ←
                    # 只读实现"不做结构收窄，此处 cast 是唯一落点（Gate 只读）
                    current_limits=cast(
                        "Callable[[datetime], LimitsView]", self._quota.current_limits
                    ),
                    min_interval_s=self._quota.min_interval_s,
                ),
                clock=clock,
            )
        )
        self._session = ConversationState(db, config)
        self._sm = SendStateMachine(db)
        self._evaluate: Evaluator = evaluator if evaluator is not None else evaluate_job
        self._transient_retries = transient_retries

    # ---- 对外入口 ----

    def run_page(self, page: int = 1, filters: SearchFilters | None = None) -> DeliveryReport:
        """抓取并处理搜索结果的一页（§7 步 0-10；翻页由调用方递增 page）。

        ``filters`` 缺省 = 空过滤（``SearchFilters()``：空关键词 + 配置城市
        语义由适配器解释）；搜索词由编排层从已确认职位方向组装。
        """
        effective_filters = SearchFilters() if filters is None else filters
        report = DeliveryReport(page=page)
        report.dry_run = dryrun.is_dry_run(self._db, self._config)
        if report.dry_run:
            report.greeting_probe = "skipped_dry_run"  # 试运行不核验 [F1]

        # [B6] 平台限额日：当日跳过投递步（其余任务照常，属 S4d 编排）
        if self._platform_limit_today(self._now()):
            report.stop_reason = "platform_limit_today"
            return report

        # [调度闸门] 投递闸门 ∧ 搜索浏览闸门（§7 步 0）
        deliver = self._gate.can_deliver(self._now())
        if not deliver.allowed:
            report.stop_reason = deliver.reason
            return report
        browse = self._browse_gate("search", report)
        if not browse.allowed:
            self._note_browse_limit("search", browse.reason, report)
            report.stop_reason = (
                "browse_search_limit" if browse.reason.startswith("browse_") else browse.reason
            )
            return report

        # 前置 ④ 主动招呼语 / ② 简历（B14：dry_run 下仍必须）
        template = self._load_greeting_template()
        if template is None:
            report.stop_reason = "greeting_template_missing"
            return report
        resume = current_resume_snapshot(self._db)
        if resume is None:
            report.stop_reason = "resume_missing"
            return report

        # 步 1：搜索（TransientError 退避重试；耗尽停轮）
        try:
            refs = self._adapter_call(
                "search_jobs", lambda: self._adapter.search_jobs(effective_filters, page)
            )
        except _AdapterFailure as failure:
            report.stop_reason = self._run_level_failure(failure, report)
            return report
        self._gate.record_action("search", self._now())
        self._quota.record_browse("search", self._now())
        report.jobs_seen = len(refs)

        for ref in refs:
            deliver = self._gate.can_deliver(self._now())
            if not deliver.allowed:
                report.stop_reason = deliver.reason
                return report
            stop = self._process_job(ref, template, resume, report)
            if stop is not None:
                report.stop_reason = stop
                return report
        return report  # stop_reason=None：页内职位全部走完

    # ---- 单职位十步（步 2-10）----

    def _process_job(
        self,
        ref: JobRef,
        template: GreetingTemplate,
        resume: ResumeSnapshot,
        report: DeliveryReport,
    ) -> str | None:
        """处理单职位；返回 None=继续下一职位，否则为停止码。"""
        dry = report.dry_run
        exclude_groups = self._config.match.exclude_groups
        blacklist = self._config.match.company_blacklist

        # 步 2：列表过滤（dedup ∧ exclusion ∧ 匿名跳过 [v13]）
        if is_anonymous(ref):
            self._record_anonymous(ref, dry)
            report.outcomes.append(
                JobOutcome(
                    ref.job_id, ref.company_key, ACTION_SKIPPED_ANONYMOUS, "anonymous_company"
                )
            )
            return None
        if exclusion_match(ref, exclude_groups, blacklist):
            report.outcomes.append(
                JobOutcome(ref.job_id, ref.company_key, ACTION_SKIPPED_EXCLUSION, "list_exclusion")
            )
            return None
        dedup = self._dedup_decision(ref, self._now())
        if not dedup.allowed:
            report.outcomes.append(
                JobOutcome(ref.job_id, ref.company_key, ACTION_SKIPPED_DEDUP, dedup.reason)
            )
            return None
        if self._already_applied(ref.job_id):
            report.outcomes.append(
                JobOutcome(
                    ref.job_id,
                    ref.company_key,
                    ACTION_SKIPPED_ALREADY_APPLIED,
                    "real_application_exists",
                )
            )
            return None

        # 步 3：详情（成功后浏览计数+1 [G3]，不分模式）
        gate_result = self._browse_gate("detail", report)
        if not gate_result.allowed:
            self._note_browse_limit("detail", gate_result.reason, report)
            return (
                "browse_detail_limit"
                if gate_result.reason.startswith("browse_")
                else gate_result.reason
            )
        try:
            detail = self._adapter_call(
                "get_job_detail", lambda: self._adapter.get_job_detail(ref.job_id)
            )
        except _AdapterFailure as failure:
            return self._handle_detail_failure(ref, failure, report)
        self._gate.record_action("detail", self._now())
        self._quota.record_browse("detail", self._now())
        self._cache_job(detail)

        # 步 4：详情过滤（排除组 scope=jd_text/tags）
        if exclusion_match(detail, exclude_groups, blacklist):
            report.outcomes.append(
                JobOutcome(
                    ref.job_id, ref.company_key, ACTION_SKIPPED_EXCLUSION, "detail_exclusion"
                )
            )
            self._throttle()
            return None

        # 步 5：评估缓存（命中跳过 AI；dry_run 与正式共用）
        cached = self._evaluation_for(detail, resume, report)

        # 步 6：判定（verdict≠skip ∧ score≥min_score，矛盾从保守）
        judgment = judge_decision(
            cached.evaluation,
            self._config.ai.min_score,
            self._config.ai.neutral_invest,
        )
        if not judgment.invest:
            reason = cached.evaluation.skipped_by or judgment.skipped_by or "verdict_skip"
            self._backfill_skipped_by(cached.row_id, reason)
            action = {
                "low_score": ACTION_SKIPPED_LOW_SCORE,
                "ai_error": ACTION_SKIPPED_AI_ERROR,
            }.get(reason, ACTION_SKIPPED_VERDICT)
            report.outcomes.append(JobOutcome(ref.job_id, ref.company_key, action, reason))
            self._throttle()
            return None

        digest = payload_digest(template.content)

        # 步 7：dry_run 分支（begin(dry_run=True) 单一入口 [v16]）
        if dry:
            intent_id = self._sm.begin("greeting", ref.job_id, digest, dry_run=True)
            self._insert_application(
                ref,
                template.version,
                cached.row_id,
                "skipped_dryrun",
                dry_run=True,
                sent_at=None,
            )
            report.outcomes.append(
                JobOutcome(
                    ref.job_id, ref.company_key, ACTION_SKIPPED_DRYRUN, f"intent:{intent_id}"
                )
            )
            self._throttle()
            return None

        # 步 8-9：发送（begin → send_greeting → resolve）+ 落库登记
        stop = self._send(ref, template, cached.row_id, digest, report)
        if stop is None:
            # 步 10：节流（随机 8-20s 与 H2 最小间隔取大者）→ 下一职位
            self._throttle()
        return stop

    # ---- 步 8-9：发送与登记 ----

    def _send(
        self,
        ref: JobRef,
        template: GreetingTemplate,
        eval_row_id: int,
        digest: str,
        report: DeliveryReport,
    ) -> str | None:
        today = self._probe_date()
        probe_pending = not self._session.is_greeting_probe_done(today)

        # F1 核验基准：发送前会话快照（新会话差分定位核验目标）
        conv_before: set[str] | None = None
        if probe_pending:
            try:
                convs = self._adapter_call("list_conversations", self._adapter.list_conversations)
                conv_before = {c.conv_id for c in convs}
            except _AdapterFailure:
                conv_before = None  # 基准不可得 → 核验 inconclusive

        intent_id = self._sm.begin("greeting", ref.job_id, digest, dry_run=False)
        try:
            outcome = self._adapter_call(
                "send_greeting", lambda: self._adapter.send_greeting(ref, template.content)
            )
        except _AdapterFailure as failure:
            return self._handle_send_failure(ref, template, eval_row_id, intent_id, failure, report)

        if outcome == SendOutcome.SENT:
            self._sm.resolve(intent_id, "sent")
            self._insert_application(
                ref,
                template.version,
                eval_row_id,
                "sent",
                dry_run=False,
                sent_at=utc_str(self._now()),
            )
            self._register_company(ref)  # 步 9：去重登记（仅正式模式到达）
            rampup.record_rampup_day_if_delivered(self._db, self._config, self._now())
            report.outcomes.append(
                JobOutcome(ref.job_id, ref.company_key, ACTION_SENT, f"intent:{intent_id}")
            )
            if probe_pending:
                return self._probe_after_send(ref, conv_before, template, report)
            if report.greeting_probe == "not_reached":
                report.greeting_probe = "already_done"
            return None

        if outcome == SendOutcome.FAILED:
            self._sm.resolve(intent_id, "failed")
            self._insert_application(
                ref, template.version, eval_row_id, "failed", dry_run=False, sent_at=None
            )
            report.outcomes.append(
                JobOutcome(ref.job_id, ref.company_key, ACTION_FAILED, "adapter_failed")
            )
            return None

        # UNKNOWN / PENDING：结果不确定 → intent 留 pending（B4 重启→unknown，
        # 只对账不重发）；applications 记 unknown；公司保守登记（宁可不重投）
        self._insert_application(
            ref, template.version, eval_row_id, "unknown", dry_run=False, sent_at=None
        )
        self._register_company(ref)
        report.outcomes.append(
            JobOutcome(ref.job_id, ref.company_key, ACTION_UNKNOWN, f"intent:{intent_id}")
        )
        return None

    def _handle_send_failure(
        self,
        ref: JobRef,
        template: GreetingTemplate,
        eval_row_id: int,
        intent_id: int,
        failure: _AdapterFailure,
        report: DeliveryReport,
    ) -> str | None:
        """send_greeting 抛错（分类后）的处置（§5.2 错误表）。"""
        if failure.label in _RUN_LEVEL_LABELS:
            # 登录/风控/断件：发送结果不确定 → intent 留 pending（B4 对账）；
            # applications 记 unknown + 公司保守登记；run 级停
            self._insert_application(
                ref, template.version, eval_row_id, "unknown", dry_run=False, sent_at=None
            )
            self._register_company(ref)
            report.outcomes.append(
                JobOutcome(
                    ref.job_id,
                    ref.company_key,
                    ACTION_UNKNOWN,
                    f"intent:{intent_id} ({failure.label})",
                )
            )
            return self._run_level_failure(failure, report)
        # platform_limit / transient（耗尽）/ adapter：平台已拒绝或未发出
        # → resolve failed + applications failed，不登记公司（未发出=无暴露）
        self._sm.resolve(intent_id, "failed")
        self._insert_application(
            ref, template.version, eval_row_id, "failed", dry_run=False, sent_at=None
        )
        if failure.label == "platform_limit":
            report.outcomes.append(
                JobOutcome(ref.job_id, ref.company_key, ACTION_FAILED, "platform_limit")
            )
            return self._run_level_failure(failure, report)
        reason = "transient_exhausted" if failure.label == "transient" else "adapter_error"
        report.outcomes.append(JobOutcome(ref.job_id, ref.company_key, ACTION_FAILED, reason))
        return None  # 单职位失败不中断循环 [NFR2]

    # ---- F1 每日核验 ----

    def _probe_after_send(
        self,
        ref: JobRef,
        conv_before: set[str] | None,
        template: GreetingTemplate,
        report: DeliveryReport,
    ) -> str | None:
        """读回新会话我方首条消息与当前模板归一化比较 [F1]。

        返回 ``greeting_mismatch``（停轮）或 None；结果写
        ``report.greeting_probe``。该消息**不触发接管**——本层不调用
        mark_manual_takeover，且其在 S2b reconcile 按"岗位+±10min"天然
        命中 greeting intent（§8.1 例外条款）。
        """
        today = self._probe_date()
        conv_id = self._locate_probe_conv(conv_before, ref.job_id)
        if conv_id is None:
            report.greeting_probe = "unreadable"
            return None
        try:
            messages = self._adapter_call(
                "get_messages", lambda: self._adapter.get_messages(conv_id, None)
            )
        except _AdapterFailure:
            report.greeting_probe = "unreadable"
            return None
        ours = [m for m in messages if m.direction == "out"]
        if not ours:
            report.greeting_probe = "unreadable"
            return None
        first = min(ours, key=lambda m: (m.sent_at, m.platform_msg_id))
        if normalize_greeting_text(first.text) == normalize_greeting_text(template.content):
            self._session.mark_greeting_probe(today)
            report.greeting_probe = "match"
            return None

        report.greeting_probe = "mismatch"
        # 不标记当日核验（次轮重投仍会核验——引擎级暂停之外的双保险），
        # 也不调用 mark_manual_takeover（F1：不误判接管）
        self._emit(
            report,
            "GREETING_MISMATCH",
            {
                "conv_id": conv_id,
                "job_id": ref.job_id,
                "template_version": template.version,
                "template_text": template.content,
                "platform_text": first.text,
                "action": "pause_delivery",
                "manual_takeover": False,
                "message": (
                    "平台实际招呼语与控制台模板不一致：已暂停投递；"
                    "该消息不触发人工接管，请先在平台同步招呼语 [F1]"
                ),
            },
        )
        return "greeting_mismatch"

    def _locate_probe_conv(self, conv_before: set[str] | None, job_id: str) -> str | None:
        """定位核验目标会话：新会话中关联岗位匹配者优先，否则唯一新会话。"""
        try:
            convs = self._adapter_call("list_conversations", self._adapter.list_conversations)
        except _AdapterFailure:
            return None
        candidates = (
            list(convs)
            if conv_before is None
            else [c for c in convs if c.conv_id not in conv_before]
        )
        for conv in candidates:
            try:
                job = self._adapter_call(
                    "get_conversation_job", partial(self._conv_job_of, conv.conv_id)
                )
            except _AdapterFailure:
                continue
            if job is not None and job.job_id == job_id:
                return conv.conv_id
        if len(candidates) == 1:
            return candidates[0].conv_id
        return None

    def _conv_job_of(self, conv_id: str) -> JobRef | None:
        """get_conversation_job 的零参包装（partial 绑定，规避循环闭包）。"""
        return self._adapter.get_conversation_job(conv_id)

    def _probe_date(self) -> date:
        """核验标记的"当日"（北京自然日 [R6-4]）。"""
        return as_utc(self._clock()).astimezone(CST).date()

    # ---- 步 5：评估缓存 ----

    def _evaluation_for(
        self,
        detail: JobDetail,
        resume: ResumeSnapshot,
        report: DeliveryReport,
    ) -> _CachedEval:
        """缓存命中 → 复用；未命中 → AI 评估并入缓存（§7 步 5）。"""
        row = self._db.query_one(
            "SELECT id, score, verdict, reason, skipped_by FROM evaluations"
            " WHERE job_id = ? AND verdict IS NOT NULL ORDER BY id DESC LIMIT 1",
            (detail.job_id,),
        )
        if row is not None:
            report.eval_cache_hits += 1
            return _CachedEval(
                row_id=int(row["id"]),
                evaluation=Evaluation(
                    score=int(row["score"] or 0),
                    verdict=cast(Verdict, str(row["verdict"])),
                    reason=str(row["reason"] or ""),
                    skipped_by=cast("str | None", row["skipped_by"]),
                ),
            )
        evaluation = self._evaluate(detail, resume, self._config.ai.preferences)
        report.evaluator_calls += 1
        row_id = self._db.execute_returning_id(
            "INSERT INTO evaluations (job_id, score, verdict, reason, skipped_by, dry_run)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                detail.job_id,
                evaluation.score,
                evaluation.verdict,
                evaluation.reason,
                evaluation.skipped_by,
                int(report.dry_run),
            ),
        )
        return _CachedEval(row_id=row_id, evaluation=evaluation)

    def _backfill_skipped_by(self, row_id: int, reason: str) -> None:
        """判定层归因回填（ai_error 等既有留痕优先，不覆盖）。"""
        self._db.execute(
            "UPDATE evaluations SET skipped_by = ? WHERE id = ? AND skipped_by IS NULL",
            (reason, row_id),
        )

    # ---- 步 9：落库与去重登记 ----

    def _insert_application(
        self,
        ref: JobRef,
        greeting_version: int,
        eval_id: int,
        status: str,
        *,
        dry_run: bool,
        sent_at: str | None,
    ) -> int:
        return self._db.execute_returning_id(
            "INSERT INTO applications"
            " (job_id, company_key, greeting_version, eval_id, status, dry_run, sent_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ref.job_id, ref.company_key, greeting_version, eval_id, status, int(dry_run), sent_at),
        )

    def _register_company(self, ref: JobRef) -> None:
        """companies 去重登记（仅正式模式调用；试运行不写 [N2]）。

        - permanent → mode='permanent'，blocked_until=NULL；
        - cooldown → blocked_until=now+cooldown_days 天 [S2a 建议]；
        - 已有记录不覆盖（首登语义为准——不为重复投递场景降级拉黑模式）。
        """
        mode = (
            "permanent" if self._config.delivery.dedup.mode == "permanent_blacklist" else "cooldown"
        )
        blocked_until = (
            utc_str(self._now() + timedelta(days=self._config.delivery.dedup.cooldown_days))
            if mode == "cooldown"
            else None
        )
        self._db.execute(
            "INSERT INTO companies (company_key, name, mode, blocked_until, source)"
            " VALUES (?, ?, ?, ?, 'delivery')"
            " ON CONFLICT DO NOTHING",
            (ref.company_key, ref.company_name, mode, blocked_until),
        )

    def _dedup_decision(self, ref: JobRef, now: datetime) -> DedupDecision:
        row = self._db.query_one(
            "SELECT mode, blocked_until FROM companies WHERE company_key = ?",
            (ref.company_key,),
        )
        return dedup_check(ref.company_key, row, self._config.delivery.dedup.mode, now)

    def _already_applied(self, job_id: str) -> bool:
        row = self._db.query_one(
            "SELECT id FROM applications WHERE job_id = ? AND dry_run = 0", (job_id,)
        )
        return row is not None

    def _cache_job(self, detail: JobDetail) -> None:
        """jobs 缓存 UPSERT（写入时机由 S4c 定：详情成功后；first_seen 保留）。"""
        self._db.execute(
            "INSERT INTO jobs (job_id, title, company_key, detail_json)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(job_id) DO UPDATE SET title = excluded.title,"
            " company_key = excluded.company_key, detail_json = excluded.detail_json",
            (
                detail.job_id,
                detail.title,
                detail.company_key,
                json.dumps(asdict(detail), ensure_ascii=False),
            ),
        )

    def _record_anonymous(self, ref: JobRef, dry_run: bool) -> None:
        """匿名卡跳过留痕（evaluations.skipped_by='anonymous_company' [v13]）。"""
        self._db.execute_returning_id(
            "INSERT INTO evaluations (job_id, reason, skipped_by, dry_run) VALUES (?, ?, ?, ?)",
            (
                ref.job_id,
                "匿名公司卡跳过（无真实公司名可判）[v13]",
                "anonymous_company",
                int(dry_run),
            ),
        )

    # ---- 适配器调用（TransientError 退避重试 + 分类）----

    def _adapter_call(self, action: str, fn: Callable[[], _T]) -> _T:
        """执行适配器调用：TransientError 指数退避重试 ≤N 次（§5.2）。

        §5.2 异常树（adapters.base）按类分派包装为 :class:`_AdapterFailure`
        上抛；**非** AdapterError 的错误（编程 bug）原样上抛——不吞、
        不伪装成适配器失败。
        """
        attempt = 0
        while True:
            try:
                return fn()
            except TransientError as exc:
                if attempt < self._transient_retries:
                    self._sleep(_TRANSIENT_BACKOFF_BASE_S * (2**attempt))
                    attempt += 1
                    continue
                raise _AdapterFailure("transient", exc) from exc
            except (
                LoginRequiredError,
                RiskDetectedError,
                AdapterBrokenError,
                PlatformLimitError,
            ) as exc:
                raise _AdapterFailure(_label_of(exc), exc) from exc
            except AdapterError as exc:
                raise _AdapterFailure("adapter", exc) from exc

    def _handle_detail_failure(
        self, ref: JobRef, failure: _AdapterFailure, report: DeliveryReport
    ) -> str | None:
        """get_job_detail 失败的处置（无在途 intent；不登记公司）。"""
        if failure.label in _RUN_LEVEL_LABELS or failure.label == "platform_limit":
            report.outcomes.append(
                JobOutcome(ref.job_id, ref.company_key, DETAIL_FAILED, failure.label)
            )
            return self._run_level_failure(failure, report)
        reason = "transient_exhausted" if failure.label == "transient" else "adapter_error"
        report.outcomes.append(JobOutcome(ref.job_id, ref.company_key, DETAIL_FAILED, reason))
        return None  # 单职位失败不中断循环 [NFR2]

    def _run_level_failure(self, failure: _AdapterFailure, report: DeliveryReport) -> str:
        """登录/风控/断件/平台限额的统一处置（§5.2 错误表），返回停止码。"""
        now = self._now()
        if failure.label == "login_required":
            self._emit(
                report,
                "LOGIN_EXPIRED",
                {
                    "action": "pause_all_tasks",
                    "source": "delivery",
                    "error": repr(failure.exc),
                    "message": "登录失效：停止全部任务，等待用户重新登录 [FR1.3]",
                },
            )
            return "login_expired"
        if failure.label == "risk":
            signal: RiskSignal | None = None
            if isinstance(failure.exc, RiskDetectedError):
                signal = failure.exc.signal
            if signal is not None:
                try:
                    record = rampup.handle_risk_signal(self._db, self._config, signal, now)
                    report.events_emitted.append(record.event_type)
                    return "risk_pause"
                except ValueError:
                    pass  # 非风控类 signal（LOGIN_EXPIRED 等）：按通用风控暂停处理
            self._emit(
                report,
                "RISK_PAUSE",
                {
                    "action": "pause_all_tasks",
                    "source": "delivery",
                    "signal": str(signal),
                    "error": repr(failure.exc),
                    "message": "检测到风控信号：暂停全部任务，等待用户处理 [FR1.4]",
                },
            )
            return "risk_pause"
        if failure.label == "broken":
            self._emit(
                report,
                "ADAPTER_BROKEN",
                {
                    "action": "pause_all_tasks",
                    "source": "delivery",
                    "error": repr(failure.exc),
                    "message": "适配器疑似失效（关键元素连续定位失败）[B7]",
                },
            )
            return "adapter_broken"
        if failure.label == "platform_limit":
            today = beijing_date_str(now)
            runstate.set_state(self._db, KEY_PLATFORM_LIMIT, today)
            self._emit(
                report,
                "PLATFORM_LIMIT",
                {
                    "action": "stop_delivery_today",
                    "date": today,
                    "job_id": None,
                    "message": "平台今日沟通达上限：当日停止投递，消息处理照常 [B6]",
                },
            )
            return "platform_limit"
        # transient（搜索步耗尽）/ adapter（搜索步未分类失败）：无在途职位
        return "transient_exhausted" if failure.label == "transient" else "adapter_error"

    # ---- 闸门与节流 ----

    def _browse_gate(self, kind: ActionKind, report: DeliveryReport) -> GateResult:
        """浏览闸门（I7）；min_interval 未满时先等满再复查一次。"""
        result = self._gate.can_browse(kind, self._now())
        if not result.allowed and result.reason == "min_interval":
            self._sleep(self._gate.min_interval_remaining(kind, self._now()))
            result = self._gate.can_browse(kind, self._now())
        return result

    def _note_browse_limit(
        self, kind: Literal["detail", "search"], reason: str, report: DeliveryReport
    ) -> None:
        """浏览上限触达通知（中级，[F10] 与 PLATFORM_LIMIT 严格区分）。"""
        if not reason.startswith("browse_") or kind in report.browse_limit_events:
            return
        report.browse_limit_events.add(kind)
        self._emit(
            report,
            "BROWSE_LIMIT_REACHED",
            {
                "kind": kind,
                "reason": reason,
                "action": "stop_browse",
                "message": "我方浏览上限达到：停止对应浏览动作（与平台限额无关）[F10/G3]",
            },
        )

    def _throttle(self) -> None:
        """步 10 节流：max(随机延时, detail 最小间隔余量) [H2]。"""
        low, high = self._config.delivery.action_delay_range_s
        delay = self._rng.uniform(float(low), float(high))
        floor = self._gate.min_interval_remaining("detail", self._now())
        wait = max(delay, floor)
        if wait > 0:
            self._sleep(wait)

    # ---- 杂项 ----

    def _now(self) -> datetime:
        return self._clock()

    def _load_greeting_template(self) -> GreetingTemplate | None:
        row = self._db.query_one(
            "SELECT content, version FROM greeting_templates"
            " WHERE kind = 'active' AND is_current = 1"
        )
        if row is None:
            return None
        return GreetingTemplate(content=str(row["content"]), version=int(row["version"]))

    def _platform_limit_today(self, now: datetime) -> bool:
        value = runstate.get_state(self._db, KEY_PLATFORM_LIMIT)
        return isinstance(value, str) and value == beijing_date_str(now)

    def _emit(self, report: DeliveryReport, event_type: str, payload: dict[str, Any]) -> None:
        """事件发射（I5；级别由 EVENT_LEVELS 权威表路由）+ 报告留痕。"""
        events.emit(self._db, event_type, payload)
        report.events_emitted.append(event_type)
