"""S4c 投递流水线单元测试（设计文档 §7 十步 / F1 核验 / B6 / F10）。

覆盖任务书 S4c 验收点（A-CORE-1 投递半链路 / A-CORE-3 限额与浏览上限 /
A-CORE-8 去重双模式逻辑、核验三态、dry_run 单一入口），全部离线：
FakeAdapter 继承 S4a 交付的 ``SiteAdapter``（真实 §5.2 契约，无网络/
浏览器），FakeClock 推进式时钟（sleep 即前进，模型真实时间流逝）。
"""

from __future__ import annotations

import json
import random
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

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
from job_agent.ai.types import Evaluation
from job_agent.config import Config, ExcludeGroup, MatchConfig
from job_agent.core import dryrun, runstate
from job_agent.core.delivery import (
    ACTION_FAILED,
    ACTION_SENT,
    ACTION_SKIPPED_AI_ERROR,
    ACTION_SKIPPED_ALREADY_APPLIED,
    ACTION_SKIPPED_ANONYMOUS,
    ACTION_SKIPPED_DEDUP,
    ACTION_SKIPPED_DRYRUN,
    ACTION_SKIPPED_EXCLUSION,
    ACTION_SKIPPED_LOW_SCORE,
    ACTION_SKIPPED_VERDICT,
    ACTION_UNKNOWN,
    KEY_PLATFORM_LIMIT,
    DeliveryPipeline,
    DeliveryReport,
    judge_decision,
    normalize_greeting_text,
)
from job_agent.core.quota import Quota
from job_agent.core.session import KEY_GREETING_PROBE
from job_agent.core.state_machine import SendStateMachine, payload_digest
from job_agent.db import Database, open_database
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

# 北京周一 12:00（默认投递时段 10:00-16:00 工作日内）；冷却到期复投的
# 第二时刻 = +31 天（2026-12-10 周四 12:00，同在时段内）
T0 = datetime(2026, 11, 9, 4, 0, tzinfo=UTC)
T_EXPIRED = datetime(2026, 12, 10, 4, 0, tzinfo=UTC)

TEMPLATE = "您好，我对该职位很感兴趣，期待与您进一步沟通！"


# ---- 测试桩（继承 SiteAdapter：真实 §5.2 契约，离线无浏览器）----


class FakeClock:
    """推进式时钟：sleep(seconds) 即前进，slept 全量记录（节流/退避断言）。"""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now = self.now + timedelta(seconds=seconds)


def _to_ref(j: JobDetail) -> JobRef:
    return JobRef(
        job_id=j.job_id,
        title=j.title,
        company_name=j.company_name,
        company_key=j.company_key,
        salary_text=j.salary_text,
        tags=list(j.tags),
        list_snippet=j.list_snippet,
    )


class FakeAdapter(SiteAdapter):
    """SiteAdapter 桩：投递面可注入逐次错误/平台招呼语/发送结果。

    非投递面（登录/风控探测、回复/简历发送、基线）为最小桩实现——
    本测试文件不触达；mock 适配器全场景注入见 S4a 自己的测试。
    """

    site_id = "fake-s4c"

    def __init__(self, jobs: list[JobDetail], clock: FakeClock) -> None:
        super().__init__()
        self.jobs: dict[str, JobDetail] = {j.job_id: j for j in jobs}
        self.clock = clock
        self.search_errors: list[Exception] = []
        self.detail_errors: dict[str, list[Exception]] = {}
        self.send_errors: list[Exception] = []
        self.send_outcome: SendOutcome = SendOutcome.SENT
        # None=读回时使用传入的模板文本（=平台与模板一致）；否则为平台预设
        self.platform_greeting: str | None = None
        self.search_calls = 0
        self.detail_calls: list[str] = []
        self.send_calls: list[str] = []
        self.list_convs_calls = 0
        self.messages_calls = 0
        self.conv_job_calls = 0
        self.log: list[str] = []
        self.convs: dict[str, list[Message]] = {}
        self.conv_job: dict[str, str] = {}
        self._seq = 0

    # ---- §5.2 投递面 ----

    def search_jobs(self, filters: SearchFilters, page: int) -> list[JobRef]:
        self.search_calls += 1
        self.log.append(f"search:{page}")
        if self.search_errors:
            raise self.search_errors.pop(0)
        return [_to_ref(j) for j in self.jobs.values()]

    def get_job_detail(self, job_id: str) -> JobDetail:
        self.detail_calls.append(job_id)
        self.log.append(f"detail:{job_id}")
        errors = self.detail_errors.get(job_id)
        if errors:
            raise errors.pop(0)
        detail = self.jobs.get(job_id)
        if detail is None:
            raise AdapterError(f"job not found: {job_id}")
        return detail

    def send_greeting(self, job: JobRef, message: str) -> SendOutcome:
        self.send_calls.append(job.job_id)
        self.log.append(f"send:{job.job_id}")
        if self.send_errors:
            raise self.send_errors.pop(0)
        if self.send_outcome == SendOutcome.FAILED:
            return SendOutcome.FAILED
        conv_id = f"conv-{job.job_id}"
        text = message if self.platform_greeting is None else self.platform_greeting
        self._seq += 1
        self.convs[conv_id] = [
            Message(
                platform_msg_id=f"m{self._seq}",
                conv_id=conv_id,
                direction="out",
                sender="me",
                msg_type=MsgType.TEXT,
                text=text,
                sent_at=self.clock.now,
            )
        ]
        self.conv_job[conv_id] = job.job_id
        return self.send_outcome

    def list_conversations(self) -> list[Conversation]:
        self.list_convs_calls += 1
        self.log.append("list_convs")
        return [
            Conversation(
                conv_id=c,
                peer_name="HR甲",
                job_ref=None,
                last_message_at=self.clock.now,
                unread=0,
            )
            for c in self.convs
        ]

    def get_messages(self, conv_id: str, since: datetime | None) -> list[Message]:
        self.messages_calls += 1
        self.log.append(f"messages:{conv_id}")
        return list(self.convs.get(conv_id, []))

    def get_conversation_job(self, conv_id: str) -> JobRef | None:
        self.conv_job_calls += 1
        self.log.append(f"convjob:{conv_id}")
        job_id = self.conv_job.get(conv_id)
        return _to_ref(self.jobs[job_id]) if job_id is not None else None

    # ---- §5.2 非投递面（最小桩）----

    def check_login_state(self) -> LoginState:
        return LoginState.LOGGED_IN

    def detect_risk(self) -> RiskSignal:
        return RiskSignal.NONE

    def send_reply(self, conv_id: str, message: str) -> SendOutcome:
        raise NotImplementedError("S4c 投递测试不触达")

    def send_resume(self, conv_id: str) -> SendOutcome:
        raise NotImplementedError("S4c 投递测试不触达")

    def snapshot_conversation_baseline(self) -> int:
        return len(self.convs)


# ---- 装配助手 ----


def make_config(
    *,
    dry_run: bool = True,
    hourly: int = 25,
    daily: int = 100,
    detail_hourly: int = 40,
    detail_daily: int = 200,
    search_hourly: int = 30,
    min_score: int = 60,
    neutral_invest: bool = True,
    dedup_mode: str = "permanent_blacklist",
    cooldown_days: int = 30,
    min_detail: int = 30,
    delay: tuple[int, int] = (8, 20),
    company_blacklist: list[str] | None = None,
    all_day_schedule: bool = False,
) -> Config:
    """构造测试配置；``all_day_schedule``=全周 00:00-23:59（真实时钟锚定用）。"""
    delivery: dict[str, object] = {
        "dry_run": dry_run,
        "hourly_limit": hourly,
        "daily_limit": daily,
        "browse_limits": {
            "detail_hourly": detail_hourly,
            "detail_daily": detail_daily,
            "search_page_hourly": search_hourly,
        },
        "min_interval_s": {"detail": min_detail, "search": 30},
        "action_delay_range_s": [delay[0], delay[1]],
        "dedup": {"mode": dedup_mode, "cooldown_days": cooldown_days},
    }
    if all_day_schedule:
        delivery["run_schedule"] = {
            "weekdays": [1, 2, 3, 4, 5, 6, 7],
            "start": "00:00",
            "end": "23:59",
        }
    return Config.model_validate(
        {
            "delivery": delivery,
            "ai": {"min_score": min_score, "neutral_invest": neutral_invest},
            "match": {"company_blacklist": company_blacklist or []},
        }
    )


def make_detail(
    job_id: str,
    *,
    company: str = "星辰科技",
    company_key: str | None = None,
    title: str = "Python工程师",
    jd: str = "负责后端服务开发与维护，要求熟悉 Python。",
    tags: tuple[str, ...] = ("python", "五险一金"),
) -> JobDetail:
    return JobDetail(
        job_id=job_id,
        title=title,
        company_name=company,
        company_key=company_key or f"key-{company}",
        salary_text="15-25K",
        tags=list(tags),
        list_snippet=f"{title} {company}",
        jd_text=jd,
        experience="3-5年",
        company_scale="500-999人",
        detail_url=f"https://example.com/job/{job_id}",
    )


def seed_template(db: Database, content: str = TEMPLATE, version: int = 1) -> None:
    db.execute(
        "INSERT INTO greeting_templates (kind, content, version, is_current)"
        " VALUES ('active', ?, ?, 1)",
        (content, version),
    )


def seed_resume(db: Database) -> None:
    db.execute(
        "INSERT INTO resume_snapshots (structured_json, is_current) VALUES (?, 1)",
        (json.dumps({"education": [], "experience": ["5年后端"], "skills": [], "projects": []}),),
    )


def seed_company(
    db: Database, company_key: str, mode: str, blocked_until: str | None = None
) -> None:
    db.execute(
        "INSERT INTO companies (company_key, name, mode, blocked_until, source)"
        " VALUES (?, ?, ?, ?, 'seed')",
        (company_key, "种子公司", mode, blocked_until),
    )


def seed_real_application(db: Database, job_id: str, company_key: str) -> None:
    db.execute(
        "INSERT INTO applications (job_id, company_key, status, dry_run) VALUES (?, ?, 'sent', 0)",
        (job_id, company_key),
    )


def set_mode(db: Database, dry: bool) -> None:
    """直接写 run_state 模式键（绕过 set_dry_run 的放量联动，隔离测试对象）。"""
    runstate.set_state(db, dryrun.KEY_DRY_RUN, dry)


def events_of(db: Database, event_type: str) -> list[tuple[str, dict[str, Any]]]:
    rows = db.query(
        "SELECT level, payload_json FROM notification_events WHERE event_type = ? ORDER BY id",
        (event_type,),
    )
    return [(str(r["level"]), json.loads(r["payload_json"])) for r in rows]


def rows_of(db: Database, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(sql, params)]


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = open_database(tmp_path / "test.db")
    seed_template(database)
    seed_resume(database)
    yield database
    database.close()


def make_pipeline(
    db: Database,
    adapter: FakeAdapter,
    config: Config,
    clock: FakeClock,
) -> tuple[DeliveryPipeline, Counter[str]]:
    """装配流水线 + 评估调用计数器（默认评估恒 recommend/90，供缓存断言）。"""
    calls: Counter[str] = Counter()

    def evaluator(job: JobDetail, resume: object, prefs: list[object]) -> Evaluation:
        calls["evaluate"] += 1
        return Evaluation(score=90, verdict="recommend", reason="匹配")

    pipeline = DeliveryPipeline(
        db,
        config,
        adapter,
        clock=clock,
        sleep=clock.sleep,
        rng=random.Random(7),
        evaluator=evaluator,
    )
    return pipeline, calls


def outcomes_by_action(report: DeliveryReport) -> dict[str, int]:
    counts: Counter[str] = Counter(o.action for o in report.outcomes)
    return dict(counts)


# ---- A-CORE-1：试运行投递半链路 ----


def test_dry_run_half_chain(db: Database) -> None:
    """试运行：拟发送留痕完整、零实际发送、零 companies 写入 [N2]。"""
    config = make_config(dry_run=True)
    clock = FakeClock(T0)
    adapter = FakeAdapter(
        [
            make_detail("j1"),
            make_detail("j2", company="月轮科技"),
            make_detail("j3", company="流云网络"),
        ],
        clock,
    )
    pipeline, calls = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert report.dry_run is True
    assert report.stop_reason is None
    assert report.jobs_seen == 3
    assert outcomes_by_action(report) == {ACTION_SKIPPED_DRYRUN: 3}
    # send_intents：3 条 skipped_dryrun（dry_run=1），无任何 sent
    intents = rows_of(
        db, "SELECT kind, target_ref, state, dry_run, payload_digest FROM send_intents"
    )
    assert len(intents) == 3
    assert all(
        i["kind"] == "greeting" and i["state"] == "skipped_dryrun" and i["dry_run"] == 1
        for i in intents
    )
    assert {i["target_ref"] for i in intents} == {"j1", "j2", "j3"}
    assert all(i["payload_digest"] == payload_digest(TEMPLATE) for i in intents)
    assert rows_of(db, "SELECT id FROM send_intents WHERE state = 'sent'") == []
    # applications：dry_run=1 独立存放
    apps = rows_of(db, "SELECT job_id, status, dry_run, greeting_version FROM applications")
    assert len(apps) == 3
    assert all(a["status"] == "skipped_dryrun" and a["dry_run"] == 1 for a in apps)
    assert all(a["greeting_version"] == 1 for a in apps)
    # 试运行不污染 [N2]：去重名单与实际发送均零
    assert rows_of(db, "SELECT company_key FROM companies") == []
    assert adapter.send_calls == []
    # 试运行不核验 [F1]
    assert adapter.messages_calls == 0
    assert adapter.list_convs_calls == 0
    assert report.greeting_probe == "skipped_dry_run"
    # 评估与岗位缓存留痕；步 10 节流发生
    assert calls["evaluate"] == 3
    assert len(rows_of(db, "SELECT job_id FROM jobs")) == 3
    assert clock.slept


def test_dry_run_single_entry_via_state_machine(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v16 裁决：dry_run 分支统一走 begin(dry_run=True)，不经 record_planned_send。"""

    def boom(*args: object, **kwargs: object) -> int:
        raise AssertionError("dry_run 分支不得调用 dryrun.record_planned_send [v16 裁决]")

    monkeypatch.setattr(dryrun, "record_planned_send", boom)
    config = make_config(dry_run=True)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1")], clock)
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert report.count(ACTION_SKIPPED_DRYRUN) == 1
    row = db.query_one("SELECT id, state, dry_run FROM send_intents WHERE target_ref = 'j1'")
    assert row is not None and row["state"] == "skipped_dryrun" and row["dry_run"] == 1
    # I2 契约：dry_run intent 为终态，不得 resolve（证明走的是状态机单一入口）
    with pytest.raises(ValueError, match="不得 resolve"):
        SendStateMachine(db).resolve(int(row["id"]), "sent")


def test_evaluation_cache_reused_across_runs(db: Database) -> None:
    """步 5：evaluations 命中跳过 AI（dry_run 与正式共用缓存）。"""
    config = make_config(dry_run=True)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1")], clock)
    pipeline, calls = make_pipeline(db, adapter, config, clock)

    first = pipeline.run_page()
    second = pipeline.run_page()

    assert first.evaluator_calls == 1 and calls["evaluate"] == 1
    assert second.eval_cache_hits == 1 and second.evaluator_calls == 0
    assert len(rows_of(db, "SELECT id FROM evaluations WHERE verdict = 'recommend'")) == 1


# ---- A-CORE-8：去重双模式 ----


def test_dedup_permanent_registers_and_blocks_second_encounter(db: Database) -> None:
    """正式投递后登记 permanent（blocked_until=NULL）；同公司再现被拒。"""
    config = make_config(dry_run=False)
    set_mode(db, dry=False)

    clock1 = FakeClock(T0)
    first_adapter = FakeAdapter([make_detail("j1", company="远山信息")], clock1)
    pipeline1, _ = make_pipeline(db, first_adapter, config, clock1)
    report1 = pipeline1.run_page()
    assert report1.count(ACTION_SENT) == 1

    company = db.query_one("SELECT mode, blocked_until, source FROM companies")
    assert company is not None
    assert company["mode"] == "permanent"
    assert company["blocked_until"] is None
    assert company["source"] == "delivery"

    # 同公司新岗位（+1 小时，仍在时段内）：dedup 拦截，不进详情
    clock2 = FakeClock(T0 + timedelta(hours=1))
    second_adapter = FakeAdapter(
        [make_detail("j2", company="远山信息", title="高级后端工程师")], clock2
    )
    pipeline2, _ = make_pipeline(db, second_adapter, config, clock2)
    report2 = pipeline2.run_page()

    assert outcomes_by_action(report2) == {ACTION_SKIPPED_DEDUP: 1}
    assert report2.outcomes[0].detail == "permanent"
    assert second_adapter.detail_calls == []
    assert second_adapter.send_calls == []


def test_dedup_cooldown_blocked_until_and_expiry(db: Database) -> None:
    """cooldown：登记 blocked_until=now+N 天 [S2a]；到期放行再投。"""
    config = make_config(dry_run=False, dedup_mode="cooldown", cooldown_days=30)
    set_mode(db, dry=False)

    clock1 = FakeClock(T0)
    adapter1 = FakeAdapter([make_detail("j1", company="南风软件")], clock1)
    pipeline1, _ = make_pipeline(db, adapter1, config, clock1)
    assert pipeline1.run_page().count(ACTION_SENT) == 1

    row = db.query_one("SELECT mode, blocked_until FROM companies")
    assert row is not None
    assert row["mode"] == "cooldown"
    assert row["blocked_until"] == "2026-12-09T04:00:00.000Z"  # T0 + 30 天

    # 冷却期内：拦截
    clock2 = FakeClock(T0 + timedelta(days=1))
    adapter2 = FakeAdapter([make_detail("j2", company="南风软件")], clock2)
    report2 = make_pipeline(db, adapter2, config, clock2)[0].run_page()
    assert outcomes_by_action(report2) == {ACTION_SKIPPED_DEDUP: 1}
    assert report2.outcomes[0].detail == "cooldown_active"

    # 到期（+31 天，周四 12:00 北京，仍在投递时段）：放行
    clock3 = FakeClock(T_EXPIRED)
    adapter3 = FakeAdapter([make_detail("j3", company="南风软件")], clock3)
    report3 = make_pipeline(db, adapter3, config, clock3)[0].run_page()
    assert report3.count(ACTION_SENT) == 1
    assert report3.outcomes[0].detail.startswith("intent:")


def test_failed_send_not_registered_dedup(db: Database) -> None:
    """发送失败（transient 耗尽）不登记公司：未发出=无暴露，可重试。"""
    config = make_config(dry_run=False)
    set_mode(db, dry=False)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1", company="青梧数据")], clock)
    adapter.send_errors = [TransientError("net"), TransientError("net"), TransientError("net")]
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert outcomes_by_action(report) == {ACTION_FAILED: 1}
    assert report.outcomes[0].detail == "transient_exhausted"
    assert rows_of(db, "SELECT state FROM send_intents")[0]["state"] == "failed"
    assert rows_of(db, "SELECT status FROM applications")[0]["status"] == "failed"
    assert rows_of(db, "SELECT company_key FROM companies") == []
    # 退避 1s + 2s（≤2 次重试）
    assert clock.slept[:2] == [1.0, 2.0]


# ---- A-CORE-3：限额与浏览上限（[F10] 与 PLATFORM_LIMIT 严格区分）----


def test_browse_detail_limit_stops_new_details(db: Database) -> None:
    """详情日限达 → 不再打开新 JD（存量流程走完）+ BROWSE_LIMIT_REACHED。"""
    config = make_config(dry_run=True, detail_daily=2)
    clock = FakeClock(T0)
    jobs = [make_detail(f"j{i}", company=f"公司{i}") for i in range(1, 5)]
    adapter = FakeAdapter(jobs, clock)
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert report.jobs_seen == 4
    assert report.count(ACTION_SKIPPED_DRYRUN) == 2  # 存量（已开详情）走完
    assert report.stop_reason == "browse_detail_limit"
    assert adapter.detail_calls == ["j1", "j2"]  # 不再打开新 JD
    events = events_of(db, "BROWSE_LIMIT_REACHED")
    assert len(events) == 1
    level, payload = events[0]
    assert level == "medium"
    assert payload["kind"] == "detail"
    assert payload["reason"] == "browse_detail_daily_limit"
    # [F10] 与 PLATFORM_LIMIT 严格区分：零平台限额事件、零误停投递日标记
    assert events_of(db, "PLATFORM_LIMIT") == []
    assert runstate.get_state(db, KEY_PLATFORM_LIMIT) is None


def test_platform_limit_stops_delivery_for_today(db: Database) -> None:
    """PlatformLimitError → 当日停投 + PLATFORM_LIMIT（中）[B6]。"""
    config = make_config(dry_run=False)
    set_mode(db, dry=False)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1"), make_detail("j2", company="霁光传媒")], clock)
    adapter.send_errors = [PlatformLimitError("今日沟通达上限")]
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert report.stop_reason == "platform_limit"
    assert outcomes_by_action(report) == {ACTION_FAILED: 1}
    assert report.outcomes[0].detail == "platform_limit"
    assert rows_of(db, "SELECT state FROM send_intents")[0]["state"] == "failed"
    events = events_of(db, "PLATFORM_LIMIT")
    assert len(events) == 1 and events[0][0] == "medium"
    assert events_of(db, "BROWSE_LIMIT_REACHED") == []  # 两者不混淆
    assert runstate.get_state(db, KEY_PLATFORM_LIMIT) == "2026-11-09"

    # 同日再启动：直接跳过投递步（不搜索）
    clock2 = FakeClock(T0 + timedelta(hours=1))
    adapter2 = FakeAdapter([make_detail("j1")], clock2)
    report2 = make_pipeline(db, adapter2, config, clock2)[0].run_page()
    assert report2.stop_reason == "platform_limit_today"
    assert adapter2.search_calls == 0


def test_hourly_limit_stops_mid_page(db: Database) -> None:
    """小时限额（send_intents 推算，不分模式 [G1]）页中触达即停。

    时钟锚定真实当前时刻：SendStateMachine.begin 的 created_at 取墙钟
    （I2 契约无时钟注入位），滑窗计数需要两者同窗——全日时段配置消除
    运行时刻对调度闸门的影响。
    """
    config = make_config(dry_run=False, hourly=2, all_day_schedule=True)
    set_mode(db, dry=False)
    clock = FakeClock(datetime.now(UTC))
    jobs = [make_detail(f"j{i}", company=f"公司{i}") for i in range(1, 4)]
    adapter = FakeAdapter(jobs, clock)
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert report.stop_reason == "hourly_limit_reached"
    assert report.count(ACTION_SENT) == 2
    assert adapter.send_calls == ["j1", "j2"]
    assert len(rows_of(db, "SELECT id FROM send_intents WHERE state = 'sent'")) == 2


# ---- F1：每日首次 send_greeting 成功后的招呼语核验（三态）----


def test_probe_match_marks_and_continues(db: Database) -> None:
    """一致：标记当日核验，无事件，后续职位继续投递。"""
    config = make_config(dry_run=False)
    set_mode(db, dry=False)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1"), make_detail("j2", company="拾光互娱")], clock)
    adapter.platform_greeting = TEMPLATE
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert report.greeting_probe == "match"
    assert report.stop_reason is None
    assert report.count(ACTION_SENT) == 2
    assert events_of(db, "GREETING_MISMATCH") == []
    assert runstate.get_state(db, KEY_GREETING_PROBE) == {"last_date": "2026-11-09"}

    # 当日已核验：再次投递不再读回
    clock2 = FakeClock(T0 + timedelta(hours=1))
    adapter2 = FakeAdapter([make_detail("j3", company="听澜文化")], clock2)
    report2 = make_pipeline(db, adapter2, config, clock2)[0].run_page()
    assert report2.greeting_probe == "already_done"
    assert adapter2.messages_calls == 0


def test_probe_mismatch_pauses_delivery_without_takeover(db: Database) -> None:
    """一字之差：GREETING_MISMATCH（中）+ 停止投递 + 不误判接管 [F1/A-CORE-26]。"""
    config = make_config(dry_run=False)
    set_mode(db, dry=False)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1"), make_detail("j2", company="拾光互娱")], clock)
    adapter.platform_greeting = TEMPLATE.replace("期待", "期侍")  # 一个字符
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert report.greeting_probe == "mismatch"
    assert report.stop_reason == "greeting_mismatch"
    assert adapter.send_calls == ["j1"]  # 第二个职位未再投递
    events = events_of(db, "GREETING_MISMATCH")
    assert len(events) == 1
    level, payload = events[0]
    assert level == "medium"
    assert payload["action"] == "pause_delivery"
    assert payload["template_version"] == 1
    assert payload["platform_text"] == adapter.platform_greeting
    # 不误判人工接管（F1）：无任何 manual_takeover 置位
    assert rows_of(db, "SELECT conv_id FROM conversations WHERE manual_takeover = 1") == []
    assert rows_of(db, "SELECT conv_id FROM conversations") == []
    # 核验未标记：修复后次轮仍会核验
    assert runstate.get_state(db, KEY_GREETING_PROBE) is None
    # 首个投递本身有效
    assert rows_of(db, "SELECT status FROM applications")[0]["status"] == "sent"


def test_probe_normalized_equivalent_is_match(db: Database) -> None:
    """空白/标点差异（归一化等价）不触发 [A-CORE-26③]。"""
    config = make_config(dry_run=False)
    set_mode(db, dry=False)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1")], clock)
    adapter.platform_greeting = "您好 我对该职位很感兴趣 期待与您进一步沟通"
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert report.greeting_probe == "match"
    assert events_of(db, "GREETING_MISMATCH") == []
    assert runstate.get_state(db, KEY_GREETING_PROBE) == {"last_date": "2026-11-09"}
    # 归一化函数本身：去空白/标点 + casefold
    assert normalize_greeting_text("Hello, World! 你 好。") == "helloworld你好"
    assert normalize_greeting_text(TEMPLATE) == normalize_greeting_text(
        "您好，我对该职位很感兴趣，期待与您进一步沟通"
    )


def test_probe_unreadable_when_no_out_message(db: Database) -> None:
    """读回失败/无我方消息：inconclusive——不标记、不停投、无事件。"""
    config = make_config(dry_run=False)
    set_mode(db, dry=False)
    clock = FakeClock(T0)

    class NoConvAdapter(FakeAdapter):
        def send_greeting(self, job: JobRef, message: str) -> SendOutcome:
            outcome = super().send_greeting(job, message)
            self.convs.clear()  # 平台读回失败模拟：会话不可见
            return outcome

    adapter = NoConvAdapter([make_detail("j1")], clock)
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert report.greeting_probe == "unreadable"
    assert report.stop_reason is None
    assert report.count(ACTION_SENT) == 1
    assert events_of(db, "GREETING_MISMATCH") == []
    assert runstate.get_state(db, KEY_GREETING_PROBE) is None


# ---- §7 步 2/4/5/6：过滤、缓存与判定 ----


def test_anonymous_card_skipped_without_detail_or_eval(db: Database) -> None:
    """匿名卡：skipped_by=anonymous_company，不进详情与评估 [v13/A-CORE-28]。"""
    config = make_config(dry_run=True)
    clock = FakeClock(T0)
    adapter = FakeAdapter(
        [make_detail("j-anon", company="某知名企业"), make_detail("j-ok", company="启明网络")],
        clock,
    )
    pipeline, calls = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert outcomes_by_action(report) == {
        ACTION_SKIPPED_ANONYMOUS: 1,
        ACTION_SKIPPED_DRYRUN: 1,
    }
    assert "j-anon" not in adapter.detail_calls
    anon = rows_of(db, "SELECT verdict, skipped_by FROM evaluations WHERE job_id = 'j-anon'")
    assert anon == [{"verdict": None, "skipped_by": "anonymous_company"}]
    assert calls["evaluate"] == 1  # 匿名卡不评估


def test_list_filter_exclusion_and_dedup(db: Database) -> None:
    """步 2：黑名单/排除关键词命中跳过；companies 既有记录拦截。"""
    config = make_config(dry_run=False, company_blacklist=["黑名单公司"])
    set_mode(db, dry=False)
    seed_company(db, "key-既有公司", "permanent")
    clock = FakeClock(T0)
    adapter = FakeAdapter(
        [
            make_detail("j-black", company="黑名单公司"),
            make_detail("j-dup", company="既有公司"),
            make_detail("j-ok", company="白泽智能"),
        ],
        clock,
    )
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert outcomes_by_action(report) == {
        ACTION_SKIPPED_EXCLUSION: 1,
        ACTION_SKIPPED_DEDUP: 1,
        ACTION_SENT: 1,
    }
    assert adapter.detail_calls == ["j-ok"]  # 列表级过滤不再打开详情
    assert report.outcomes[0].detail == "list_exclusion"
    assert report.outcomes[1].detail == "permanent"


def test_detail_level_exclusion_skips_after_browse(db: Database) -> None:
    """步 4：JD 正文级排除（排除组 scope=jd_text）在详情后跳过。"""
    config = make_config(dry_run=True).model_copy(
        update={
            "match": MatchConfig(
                exclude_groups=[
                    ExcludeGroup(id="jd-block", enabled=True, keywords=["外包"], scope=["jd_text"])
                ]
            )
        }
    )
    clock = FakeClock(T0)
    adapter = FakeAdapter(
        [make_detail("j-outer", jd="本项目为软件外包驻场开发。"), make_detail("j-ok")],
        clock,
    )
    pipeline, calls = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert outcomes_by_action(report) == {
        ACTION_SKIPPED_EXCLUSION: 1,
        ACTION_SKIPPED_DRYRUN: 1,
    }
    assert report.outcomes[0].detail == "detail_exclusion"
    assert set(adapter.detail_calls) == {"j-outer", "j-ok"}  # 详情后才判（步 4 顺序）
    assert calls["evaluate"] == 1  # 排除的不再评估


def test_judgment_conservative_and_backfill(db: Database) -> None:
    """步 6 判定：矛盾从保守；low_score/verdict_skip 回填 evaluations。"""
    j1 = judge_decision(Evaluation(score=95, verdict="skip", reason="r"), 60, True)
    assert (j1.invest, j1.skipped_by) == (False, "verdict_skip")  # 高分但 skip：保守
    j2 = judge_decision(Evaluation(score=59, verdict="recommend", reason="r"), 60, True)
    assert (j2.invest, j2.skipped_by) == (False, "low_score")
    j3 = judge_decision(Evaluation(score=80, verdict="neutral", reason="r"), 60, False)
    assert (j3.invest, j3.skipped_by) == (False, "verdict_skip")
    j4 = judge_decision(Evaluation(score=80, verdict="neutral", reason="r"), 60, True)
    assert (j4.invest, j4.skipped_by) == (True, None)

    # 流水线回填：低分 → evaluations.skipped_by=low_score
    config = make_config(dry_run=True, min_score=95)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1")], clock)

    def low_eval(job: JobDetail, resume: object, prefs: list[object]) -> Evaluation:
        return Evaluation(score=90, verdict="recommend", reason="尚可")

    pipeline = DeliveryPipeline(
        db,
        config,
        adapter,
        clock=clock,
        sleep=clock.sleep,
        rng=random.Random(7),
        evaluator=low_eval,
    )
    report = pipeline.run_page()
    assert outcomes_by_action(report) == {ACTION_SKIPPED_LOW_SCORE: 1}
    row = db.query_one("SELECT score, verdict, skipped_by FROM evaluations")
    assert row is not None
    assert (row["score"], row["verdict"], row["skipped_by"]) == (90, "recommend", "low_score")


def test_judgment_neutral_invest_off_and_ai_error_preserved(db: Database) -> None:
    """neutral_invest=False → verdict_skip；ai_error 归因不被覆盖。"""
    config = make_config(dry_run=True, neutral_invest=False)
    clock = FakeClock(T0)
    adapter = FakeAdapter(
        [make_detail("j-neutral"), make_detail("j-err", company="枕星科技")], clock
    )

    def evaluator(job: JobDetail, resume: object, prefs: list[object]) -> Evaluation:
        if job.job_id == "j-neutral":
            return Evaluation(score=90, verdict="neutral", reason="基本匹配")
        return Evaluation.ai_error("AI 评估失败：预算耗尽")

    pipeline = DeliveryPipeline(
        db,
        config,
        adapter,
        clock=clock,
        sleep=clock.sleep,
        rng=random.Random(7),
        evaluator=evaluator,
    )
    report = pipeline.run_page()

    assert outcomes_by_action(report) == {
        ACTION_SKIPPED_VERDICT: 1,
        ACTION_SKIPPED_AI_ERROR: 1,
    }
    rows = {r["job_id"]: r for r in rows_of(db, "SELECT job_id, skipped_by FROM evaluations")}
    assert rows["j-neutral"]["skipped_by"] == "verdict_skip"
    assert rows["j-err"]["skipped_by"] == "ai_error"  # 保留原始归因


def test_already_applied_guard(db: Database) -> None:
    """同岗位已有正式投递记录（uq 索引语义）：跳过，不再进详情。"""
    config = make_config(dry_run=False)
    set_mode(db, dry=False)
    seed_real_application(db, "j1", "key-旧公司")
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1", company_key="key-旧公司")], clock)
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert outcomes_by_action(report) == {ACTION_SKIPPED_ALREADY_APPLIED: 1}
    assert adapter.detail_calls == []


# ---- §5.2 错误表其余路径 ----


def test_transient_retry_then_success_on_send(db: Database) -> None:
    """TransientError 退避 ≤2 次重试后成功：按 sent 收口。"""
    config = make_config(dry_run=False)
    set_mode(db, dry=False)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1")], clock)
    adapter.send_errors = [TransientError("抖动"), TransientError("抖动")]
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert report.count(ACTION_SENT) == 1
    assert len(adapter.send_calls) == 3  # 初次 + 2 次重试
    assert clock.slept[:2] == [1.0, 2.0]  # 指数退避
    assert rows_of(db, "SELECT state FROM send_intents")[0]["state"] == "sent"


def test_transient_exhaustion_skips_job_and_continues(db: Database) -> None:
    """重试耗尽按 FAILED 落库；单职位失败不中断循环 [NFR2]。"""
    config = make_config(dry_run=False)
    set_mode(db, dry=False)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1"), make_detail("j2", company="暮山文化")], clock)
    adapter.send_errors = [TransientError("抖动")] * 3
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert outcomes_by_action(report) == {ACTION_FAILED: 1, ACTION_SENT: 1}
    assert report.stop_reason is None
    assert (
        rows_of(db, "SELECT state FROM send_intents WHERE target_ref = 'j1'")[0]["state"]
        == "failed"
    )


def test_unknown_outcome_leaves_intent_pending(db: Database) -> None:
    """UNKNOWN：intent 留 pending（B4 重启→unknown，只对账不重发）。"""
    config = make_config(dry_run=False)
    set_mode(db, dry=False)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1")], clock)
    adapter.send_outcome = SendOutcome.UNKNOWN
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert outcomes_by_action(report) == {ACTION_UNKNOWN: 1}
    assert rows_of(db, "SELECT state FROM send_intents")[0]["state"] == "pending"
    assert rows_of(db, "SELECT status FROM applications")[0]["status"] == "unknown"
    # 结果不确定 → 公司保守登记（宁可不重投 [FR3.6]）
    assert len(rows_of(db, "SELECT company_key FROM companies")) == 1


def test_risk_signal_pauses_all_via_m5(db: Database) -> None:
    """RiskDetectedError → rampup.handle_risk_signal：M5 全停 + 高级事件。"""
    config = make_config(dry_run=False)
    set_mode(db, dry=False)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1")], clock)
    adapter.search_errors = [RiskDetectedError(RiskSignal.CAPTCHA)]
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert report.stop_reason == "risk_pause"
    assert "RISK_PAUSE" in report.events_emitted
    assert events_of(db, "RISK_PAUSE")[0][0] == "high"
    assert runstate.get_state(db, "risk_pause", {}).get("awaiting_user") is True
    assert adapter.detail_calls == []  # 搜索步即停，未进详情


def test_login_required_leaves_intent_pending_and_stops(db: Database) -> None:
    """LoginRequiredError：intent 留 pending（结果不确定）+ LOGIN_EXPIRED（高）。"""
    config = make_config(dry_run=False)
    set_mode(db, dry=False)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1")], clock)
    adapter.send_errors = [LoginRequiredError("登录页")]
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert report.stop_reason == "login_expired"
    assert outcomes_by_action(report) == {ACTION_UNKNOWN: 1}
    assert rows_of(db, "SELECT state FROM send_intents")[0]["state"] == "pending"
    assert rows_of(db, "SELECT status FROM applications")[0]["status"] == "unknown"
    events = events_of(db, "LOGIN_EXPIRED")
    assert len(events) == 1 and events[0][0] == "high"


def test_adapter_broken_on_detail_stops_run(db: Database) -> None:
    """AdapterBrokenError（详情步）：ADAPTER_BROKEN（高）+ 停轮。"""
    config = make_config(dry_run=False)
    set_mode(db, dry=False)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1")], clock)
    adapter.detail_errors["j1"] = [AdapterBrokenError("连续 3 次定位失败")]
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert report.stop_reason == "adapter_broken"
    events = events_of(db, "ADAPTER_BROKEN")
    assert len(events) == 1 and events[0][0] == "high"
    assert rows_of(db, "SELECT id FROM send_intents") == []  # 未到达发送步


def test_out_of_schedule_no_search(db: Database) -> None:
    """周六（非工作日）：闸门拒绝，零适配器调用。"""
    config = make_config(dry_run=True)
    saturday = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)  # 北京周六 12:00
    clock = FakeClock(saturday)
    adapter = FakeAdapter([make_detail("j1")], clock)
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert report.stop_reason == "out_of_delivery_schedule"
    assert adapter.search_calls == 0


# ---- §7 步 1/3/10：浏览计数、岗位缓存与节流 ----


def test_ten_step_order_browse_counts_and_job_cache(db: Database) -> None:
    """单职位全链路调用序 + 翻页/详情浏览计数 [G3] + jobs 缓存。"""
    config = make_config(dry_run=False)
    set_mode(db, dry=False)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1")], clock)
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    report = pipeline.run_page()

    assert report.count(ACTION_SENT) == 1
    # 适配器调用序：搜索 → 详情 → 核验基准快照 → 发送 → 读回核验
    assert adapter.log[:4] == ["search:1", "detail:j1", "list_convs", "send:j1"]
    assert adapter.log[4:] == ["list_convs", "convjob:conv-j1", "messages:conv-j1"]
    # 浏览计数（不分模式 [G3]）：搜索 1 次、详情 1 次
    quota = Quota(db, config)
    used_search = quota.browse_used("search_page_hourly", clock.now)
    used_detail = quota.browse_used("detail_hourly", clock.now)
    assert (used_search, used_detail) == (1, 1)
    # jobs 缓存（UPSERT，首次写入）
    job = db.query_one("SELECT title, company_key, detail_json FROM jobs WHERE job_id = 'j1'")
    assert job is not None and job["company_key"] == "key-星辰科技"
    assert "jd_text" in json.loads(job["detail_json"])


def test_throttle_floor_at_least_min_interval(db: Database) -> None:
    """步 10：随机节流值小于 H2 下限时强制下限（30s）。"""
    config = make_config(dry_run=True, delay=(1, 2), min_detail=30)
    clock = FakeClock(T0)
    adapter = FakeAdapter([make_detail("j1"), make_detail("j2", company="望舒科技")], clock)
    pipeline, _ = make_pipeline(db, adapter, config, clock)

    pipeline.run_page()

    assert clock.slept
    # 首个节流发生在详情打开后（clock 未动）：floor=30s 恰好生效
    assert max(clock.slept) >= 29.9
    # 两次详情均被执行（节流未造成死等）
    assert adapter.detail_calls == ["j1", "j2"]
