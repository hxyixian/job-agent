"""S4d 编排器单元测试（§5.3 [B5] 优先级队列 / I6 命令消费 / 熔断总开关 /
§13 [B18] 时钟跳变 / [G11] 防休眠 / 主循环）。

全部离线（FakeAdapter + 注入时钟/睡眠/rng/防休眠记录器）；tick 直接调用，
run_forever 单独成测。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest

from job_agent.core import rampup, runstate
from job_agent.core.messenger import Messenger
from job_agent.core.orchestrator import (
    KEY_ENGINE_STATE,
    KEY_LAST_COMMAND,
    KEY_PLATFORM_LIMIT_DATE,
    TICK_S,
    EngineCommand,
    Orchestrator,
    Priority,
    make_command_queue,
)
from job_agent.core.session import ConversationState
from job_agent.core.state_machine import SendStateMachine
from job_agent.db import open_database
from job_agent.models import RiskSignal
from job_agent.timeutil import beijing_date_str

from .s4d_support import (
    BASE_TIME,
    FakeAdapter,
    FakeClock,
    LoginRequiredError,
    RiskDetectedError,
    closed_config,
    conv_row,
    end_rampup,
    events_of,
    mk_conv,
    mk_msg,
    seed_conv_row,
    seed_outbox,
    wide_config,
)

RNG_1S = lambda a, b: 1.0  # noqa: E731
RNG_5S = lambda a, b: 5.0  # noqa: E731


@pytest.fixture()
def db(tmp_path: Any) -> Any:
    database = open_database(tmp_path / "test.db")
    yield database
    database.close()


class OrcHarness:
    """编排器装配束：注入时钟/mono/睡眠/rng/防休眠记录器 + 可选 messenger。"""

    def __init__(
        self,
        db: Any,
        *,
        config: Any = None,
        with_messenger: bool = True,
        rng: Any = RNG_1S,
        classify: str = "low_risk",
    ) -> None:
        self.db = db
        self.adapter = FakeAdapter()
        self.clock = FakeClock()
        self.holder: dict[str, Any] = {"config": config or wide_config()}
        end_rampup(db, self.holder["config"])
        self.sleeps: list[float] = []
        self.awake: list[bool] = []
        self.delivery_calls: list[Any] = []
        self.reconcile_calls: list[int] = []
        self.mono_offset = 0.0
        self._req_seq = 0
        self.orch = Orchestrator(
            db,
            self.adapter,
            config_loader=lambda: self.holder["config"],
            clock=self.clock,
            mono=self._mono,
            sleep=self.sleeps.append,
            rng=rng,
            awake_ctl=self.awake.append,
            tick_s=TICK_S,
        )
        if with_messenger:
            self._build_messenger(classify)

    def _mono(self) -> float:
        return (self.clock.now - BASE_TIME).total_seconds() + self.mono_offset

    def _build_messenger(self, classify: str) -> None:
        from job_agent.ai.classify import ClassifyResult

        classify_result = ClassifyResult(classify, "测试注入")
        messenger = Messenger(
            self.db,
            self.adapter,
            config_provider=lambda: self.holder["config"],
            gate_provider=lambda: self.orch.gate,
            session=ConversationState(self.db, self.holder["config"]),
            intents=SendStateMachine(self.db),
            classify_fn=lambda msgs: classify_result,
            draft_fn=lambda ctx, category: "自动回复内容",
            clock=self.clock,
            sleep=lambda _s: None,
        )
        original = messenger.reconcile_once

        def counting_reconcile(now: Any = None) -> Any:
            self.reconcile_calls.append(1)
            return original(now)

        messenger.reconcile_once = counting_reconcile  # type: ignore[method-assign]
        self.orch.bind_messenger(messenger)
        self.messenger = messenger

    # ---- 便捷操作 ----

    def cmd(self, cmd: str, payload: dict[str, Any] | None = None) -> None:
        self._req_seq += 1
        self.orch.command_queue().put(
            EngineCommand(
                cmd=cmd,
                req_id=f"r{self._req_seq}",  # type: ignore[arg-type]
                issued_at=self.clock.now,
                payload=payload or {},
            )
        )

    def start_engine(self) -> None:
        self.cmd("start")
        self.orch.tick()

    def last_command(self) -> dict[str, Any]:
        value = runstate.get_state(self.db, KEY_LAST_COMMAND)
        assert isinstance(value, dict)
        return value

    def engine_state(self) -> dict[str, Any]:
        value = runstate.get_state(self.db, KEY_ENGINE_STATE)
        assert isinstance(value, dict)
        return value


# ---- 单执行者优先级队列 [B5] ----


def test_priority_order_detect_reply_deliver_browse(db: Any) -> None:
    harness = OrcHarness(db, with_messenger=False)
    order: list[str] = []
    harness.orch.submit(Priority.BROWSE, "browse", lambda: order.append("browse"))
    harness.orch.submit(Priority.DELIVER, "deliver", lambda: order.append("deliver"))
    harness.orch.submit(Priority.REPLY, "reply", lambda: order.append("reply"))
    harness.orch.submit(Priority.DETECT, "detect_extra", lambda: order.append("detect"))
    harness.start_engine()
    assert order == ["detect", "reply", "deliver", "browse"]


def test_same_priority_fifo(db: Any) -> None:
    harness = OrcHarness(db, with_messenger=False)
    order: list[str] = []
    for name in ("r1", "r2", "r3"):
        harness.orch.submit(Priority.REPLY, name, lambda n=name: order.append(n))
    harness.start_engine()
    assert order == ["r1", "r2", "r3"]


def test_full_pipeline_poll_to_reply_in_one_tick(db: Any) -> None:
    harness = OrcHarness(db, classify="low_risk")
    harness.adapter.convs["C1"] = mk_conv("C1", BASE_TIME + timedelta(minutes=1))
    harness.adapter.add_msg(mk_msg("M1", "C1", text="在吗", at=BASE_TIME))
    harness.start_engine()
    # 同一 tick：BROWSE poll → 路由 → REPLY 入队 → 立即按优先级执行
    assert harness.adapter.sent_replies == [("C1", "自动回复内容")]
    assert conv_row(db, "C1")["auto_sent_count"] == 1
    row = db.query_one("SELECT state FROM send_intents WHERE kind = 'reply'")
    assert row is not None and row["state"] == "sent"
    assert harness.orch.gate.can_poll(harness.clock.now) is True


def test_backlog_warning_after_30s(db: Any, caplog: Any) -> None:
    harness = OrcHarness(db, with_messenger=False)
    ran: list[str] = []
    harness.orch.submit(Priority.REPLY, "slow", lambda: ran.append("x"))
    harness.clock.advance(timedelta(seconds=31))
    with caplog.at_level(logging.WARNING):
        harness.start_engine()
    assert ran == ["x"]
    assert any("积压" in r.message for r in caplog.records)


def test_reply_tasks_are_paced_with_shared_rhythm(db: Any) -> None:
    harness = OrcHarness(db, with_messenger=False, rng=RNG_5S)
    harness.orch.submit_reply("a", lambda: None)
    harness.orch.submit_reply("b", lambda: None)
    harness.start_engine()
    # 第一个发送任务无前序记录不等待；第二个按 8-20s 节流参数补足等待 [§5.3]
    assert harness.sleeps == [5.0]


# ---- 熔断总开关（v16：is_risk_paused/risk_pause_info）----


def test_risk_signal_pauses_all_and_syncs_gate(db: Any) -> None:
    harness = OrcHarness(db)
    harness.adapter.risk_signal = RiskSignal.CAPTCHA
    harness.start_engine()
    assert rampup.is_risk_paused(db) is True
    assert harness.orch.status()["risk_paused"] is True
    assert harness.orch.gate.can_deliver(harness.clock.now).reason == "circuit_breaker"
    levels = events_of(db, "RISK_PAUSE")
    assert levels[0][0] == "high"
    assert harness.adapter.list_calls == 0  # 任务全停（poll 未执行）
    # 熔断期不轮询
    harness.clock.advance(timedelta(minutes=6))
    harness.orch.tick()
    assert harness.adapter.list_calls == 0


def test_risk_recovery_schedules_reconcile(db: Any) -> None:
    harness = OrcHarness(db)
    harness.adapter.risk_signal = RiskSignal.CAPTCHA
    harness.start_engine()
    # 用户决策清除（唯一恢复路径）→ 下一 tick 恢复 + 补一轮对账 [§13]
    rampup.clear_risk_pause(db)
    harness.adapter.risk_signal = RiskSignal.NONE  # 用户已在浏览器处理验证码
    harness.clock.advance(timedelta(minutes=6))
    harness.orch.tick()
    assert harness.reconcile_calls == [1]
    assert harness.adapter.list_calls == 1  # 恢复轮询
    assert harness.orch.status()["risk_paused"] is False


def test_risk_error_from_task_triggers_m5(db: Any) -> None:
    harness = OrcHarness(db)

    def boom() -> None:
        raise RiskDetectedError(RiskSignal.SECURITY_CHECK)

    harness.orch.submit(Priority.REPLY, "risky", boom)
    harness.start_engine()
    assert rampup.is_risk_paused(db) is True
    assert events_of(db, "SECURITY_CHECK")[0][0] == "high"


def test_login_expired_fatal_and_probe_recovery(db: Any) -> None:
    from job_agent.models import LoginState

    harness = OrcHarness(db)
    harness.adapter.login_state = LoginState.LOGGED_OUT
    harness.start_engine()
    assert harness.orch.status()["fatal_reason"] == "login_expired"
    assert events_of(db, "LOGIN_EXPIRED")[0][0] == "high"
    assert harness.adapter.list_calls == 0
    # 熔断期不重复告警；interval_min(10min) 后探测恢复
    harness.orch.tick()
    assert len(events_of(db, "LOGIN_EXPIRED")) == 1
    harness.adapter.login_state = LoginState.LOGGED_IN
    harness.clock.advance(timedelta(minutes=11))
    harness.orch.tick()
    assert harness.orch.status()["fatal_reason"] is None
    # 恢复后的下一 tick 恢复调度
    harness.clock.advance(timedelta(minutes=2))
    harness.orch.tick()
    assert harness.adapter.list_calls == 1


def test_adapter_broken_fatal_from_task(db: Any) -> None:
    class AdapterBrokenError(Exception):
        pass

    harness = OrcHarness(db, with_messenger=False)

    def boom() -> None:
        raise AdapterBrokenError()

    harness.orch.submit(Priority.REPLY, "broken", boom)
    harness.start_engine()
    assert harness.orch.status()["fatal_reason"] == "adapter_broken"
    assert events_of(db, "ADAPTER_BROKEN")[0][0] == "high"


def test_unclassified_task_error_skipped_without_fatal(db: Any, caplog: Any) -> None:
    harness = OrcHarness(db, with_messenger=False)

    def boom() -> None:
        raise RuntimeError("bug")

    harness.orch.submit(Priority.REPLY, "buggy", boom)
    with caplog.at_level(logging.WARNING):
        harness.start_engine()
    assert harness.orch.status()["fatal_reason"] is None
    assert any("失败跳过" in r.message for r in caplog.records)


def test_platform_limit_marks_day_not_fatal(db: Any) -> None:
    harness = OrcHarness(db)
    harness.adapter.risk_signal = RiskSignal.PLATFORM_LIMIT
    harness.start_engine()
    assert runstate.get_state(db, KEY_PLATFORM_LIMIT_DATE) == beijing_date_str(harness.clock.now)
    assert events_of(db, "PLATFORM_LIMIT")[0][0] == "medium"
    assert harness.orch.status()["fatal_reason"] is None
    assert harness.adapter.list_calls == 1  # 消息处理照常 [B6]
    harness.orch.tick()  # 同日不重复通知
    assert len(events_of(db, "PLATFORM_LIMIT")) == 1


# ---- EngineCommand 消费（I6）----


def test_engine_commands_lifecycle(db: Any) -> None:
    harness = OrcHarness(db, with_messenger=False)
    assert harness.orch.state == "stopped"
    harness.cmd("start")
    harness.orch.tick()
    assert harness.orch.state == "running"
    assert harness.last_command()["ok"] is True
    harness.cmd("pause")
    harness.orch.tick()
    assert harness.orch.state == "paused"
    harness.cmd("resume")
    harness.orch.tick()
    assert harness.orch.state == "running"
    harness.cmd("stop")
    harness.orch.tick()
    assert harness.orch.state == "stopped"
    # stop 清空队列
    harness.orch.submit(Priority.REPLY, "x", lambda: None)
    harness.cmd("pause")
    harness.orch.tick()
    assert harness.orch.status()["queued_tasks"] is False


def test_unknown_command_acked_error(db: Any) -> None:
    harness = OrcHarness(db, with_messenger=False)
    harness.cmd("bogus")
    harness.orch.tick()
    ack = harness.last_command()
    assert ack["ok"] is False and ack["detail"] == "unknown_command"


def test_login_mode_mutex(db: Any) -> None:
    opened: list[int] = []
    harness = OrcHarness(
        db,
        with_messenger=False,
        config=wide_config(),
    )
    harness.orch._login_mode_hook = lambda: opened.append(1)  # type: ignore[attr-defined]
    harness.cmd("login_mode")
    harness.orch.tick()
    assert opened == [1]
    assert harness.last_command()["ok"] is True
    harness.cmd("start")  # 登录窗口期间引擎启动被拒绝 [M3/G5]
    harness.orch.tick()
    assert harness.orch.state == "stopped"
    assert harness.last_command()["detail"] == "login_mode_open"
    harness.cmd("login_mode", {"closed": True})  # 窗口关闭通知
    harness.orch.tick()
    harness.cmd("start")
    harness.orch.tick()
    assert harness.orch.state == "running"


def test_reload_config_takes_effect(db: Any) -> None:
    harness = OrcHarness(db)
    harness.start_engine()  # 宽时段：首轮 poll 已执行
    assert harness.adapter.list_calls == 1
    harness.holder["config"] = closed_config(dry_run=False)
    harness.cmd("reload_config")
    harness.orch.tick()
    assert harness.last_command()["detail"] == "config_reloaded"
    harness.clock.advance(timedelta(minutes=3))  # 越过下一轮轮询时刻
    harness.orch.tick()
    assert harness.adapter.list_calls == 1  # 时段关：不再轮询
    assert harness.orch.gate.can_poll(harness.clock.now) is False


def test_approve_outbox_command_sends_without_auto_count(db: Any) -> None:
    harness = OrcHarness(db)
    seed_conv_row(db, "C1")
    entry_id = seed_outbox(db, "C1", draft="人工确认", created_at=BASE_TIME)
    harness.cmd("start")
    harness.cmd("approve_outbox", {"outbox_id": entry_id})
    harness.orch.tick()
    row = db.query_one("SELECT state FROM outbox WHERE id = ?", (entry_id,))
    assert row is not None and row["state"] == "approved"
    assert harness.adapter.sent_replies == [("C1", "人工确认")]
    assert conv_row(db, "C1")["auto_sent_count"] == 0  # 人工确认不计自动数 [B10]
    intent = db.query_one("SELECT state FROM send_intents WHERE kind = 'reply'")
    assert intent is not None and intent["state"] == "sent"


def test_skip_outbox_command(db: Any) -> None:
    harness = OrcHarness(db)
    seed_conv_row(db, "C1")
    entry_id = seed_outbox(db, "C1", created_at=BASE_TIME)
    harness.cmd("skip_outbox", {"outbox_id": entry_id})
    harness.orch.tick()
    row = db.query_one("SELECT state FROM outbox WHERE id = ?", (entry_id,))
    assert row is not None and row["state"] == "skipped"
    assert harness.last_command()["ok"] is True


def test_command_result_persisted_to_run_state(db: Any) -> None:
    harness = OrcHarness(db, with_messenger=False)
    harness.cmd("start")
    harness.orch.tick()
    ack = harness.last_command()
    assert ack["cmd"] == "start" and ack["req_id"] == "r1"
    state = harness.engine_state()
    assert state["state"] == "running" and state["dry_run"] is False


# ---- 时钟跳变 [B18] ----


def test_clock_jump_forces_detect_before_tasks(db: Any) -> None:
    harness = OrcHarness(db, with_messenger=False)
    ran: list[str] = []
    harness.orch.submit(Priority.BROWSE, "browse", lambda: ran.append("x"))
    harness.start_engine()
    assert ran == ["x"]
    detects_before = harness.adapter.detect_calls
    # 墙钟 +10min、单调钟仅 +5s → 偏差 > 5min
    harness.clock.advance(timedelta(minutes=10))
    harness.mono_offset -= timedelta(minutes=10).total_seconds() - 5.0
    harness.orch.submit(Priority.BROWSE, "browse2", lambda: ran.append("y"))
    harness.orch.tick()
    assert harness.adapter.detect_calls > detects_before  # 恢复任务前先检测
    assert ran == ["x", "y"]
    # 检测通过后标志清除
    assert harness.orch.status()["clock_jumped"] is False


# ---- 防休眠 [G11/Q21] ----


def test_keep_awake_within_and_outside_window(db: Any) -> None:
    harness = OrcHarness(db)
    harness.start_engine()
    assert harness.awake == [True]  # 时段内：ES_SYSTEM_REQUIRED
    harness.holder["config"] = closed_config(dry_run=False)
    harness.cmd("reload_config")
    harness.orch.tick()
    assert harness.awake == [True, False]  # 时段外：释放
    harness.cmd("stop")
    harness.orch.tick()
    assert harness.awake == [True, False]  # 已释放，无重复调用


def test_stopped_engine_does_not_keep_awake(db: Any) -> None:
    harness = OrcHarness(db)
    harness.orch.tick()  # stopped 状态 tick
    assert harness.awake == []


# ---- 主循环 [B5 tick 5s] ----


def test_run_forever_ticks_until_shutdown(db: Any) -> None:
    harness = OrcHarness(db, with_messenger=False)
    harness.cmd("start")
    harness.orch.tick()
    ticks: list[int] = []

    def fake_sleep(_seconds: float) -> None:
        ticks.append(1)
        harness.clock.advance(timedelta(seconds=5))
        if len(ticks) >= 3:
            harness.orch.shutdown()

    harness.orch._sleep = fake_sleep  # type: ignore[attr-defined]
    harness.orch.run_forever()
    assert len(ticks) >= 3
    assert harness.awake == [True, False]  # 退出时释放防休眠


def test_make_command_queue_and_command_defaults() -> None:
    q = make_command_queue()
    q.put(EngineCommand(cmd="pause", req_id="u1", issued_at=BASE_TIME))
    cmd = q.get_nowait()
    assert cmd.payload == {} and cmd.req_id == "u1"
    assert q.empty()


# ---- 编排器与 messenger 联动细节 ----


def test_login_error_from_poll_task_sets_fatal(db: Any) -> None:
    harness = OrcHarness(db)
    harness.adapter.convs["C1"] = mk_conv("C1", BASE_TIME)
    harness.adapter.get_messages_error = LoginRequiredError()
    harness.start_engine()
    assert harness.orch.status()["fatal_reason"] == "login_expired"
    assert events_of(db, "LOGIN_EXPIRED")[0][0] == "high"


def test_messenger_reply_submitter_bound_to_reply_priority(db: Any) -> None:
    harness = OrcHarness(db, classify="low_risk")
    names: list[str] = []
    original = harness.messenger.set_reply_submitter(
        lambda name, fn: (names.append(name), harness.orch.submit_reply(name, fn))
    )
    assert original is None
    harness.adapter.convs["C1"] = mk_conv("C1", BASE_TIME + timedelta(minutes=1))
    harness.adapter.add_msg(mk_msg("M1", "C1", at=BASE_TIME))
    harness.start_engine()
    assert any(n.startswith("reply:C1") for n in names)
    assert harness.adapter.sent_replies != []


def test_paused_engine_consumes_commands_but_no_site_ops(db: Any) -> None:
    harness = OrcHarness(db)
    harness.cmd("pause")
    harness.orch.tick()
    harness.clock.advance(timedelta(minutes=6))
    harness.orch.tick()
    assert harness.adapter.list_calls == 0
    assert harness.orch.state == "paused"


def test_daily_reconcile_scheduled_once_per_day(db: Any) -> None:
    harness = OrcHarness(db)
    harness.start_engine()  # 首 tick：启动对账（每日一次）+ poll
    assert harness.reconcile_calls == [1]
    harness.clock.advance(timedelta(minutes=6))
    harness.orch.tick()  # 同日不再对账
    assert harness.reconcile_calls == [1]
    harness.clock.advance(timedelta(hours=20))
    harness.orch.tick()  # 跨北京日：再对账
    assert len(harness.reconcile_calls) == 2
