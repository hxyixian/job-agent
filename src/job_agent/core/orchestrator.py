"""编排器：单执行者优先级队列 + 引擎主循环（设计文档 §2 进程模型 / §5.3 [B5] /
§13 [B18/G11]；任务书 Stage4 文末「编排器」段，并入 S4d 交付）。

职责一览：

- **单执行者优先级队列** [B5]：所有站点操作进同一队列串行执行，
  优先级 ``检测 > 回复发送 > 投递 > 浏览采集``（:class:`Priority`，小值先执行；
  消息轮询/对账属 BROWSE 级读取）；同优先级 FIFO；积压超 30s 记 WARNING [§5.3]；
  回复类发送任务带拟人节流（与投递共用 ``action_delay_range_s`` 8-20s 随机节奏
  [§5.3]，``paced`` 标记）；
- **EngineCommand 消费**（§5.5 I6）：控制台线程经 ``queue.Queue[EngineCommand]``
  投递，本线程每 tick 消费；执行结果写 ``run_state``（``last_command`` /
  ``engine_state``），不回传线程对象；
- **熔断总开关**：数据源 = :func:`rampup.is_risk_paused`（M5 风控暂停，v16
  裁决）∪ 引擎 fatal（登录失效/适配器失效）→ 同步 ``Gate.set_circuit_breaker``
  并暂停全部站点任务（清空队列；已登记的发送 intent 保持 pending → 启动恢复
  unknown → 对账，绝不重发 [B4]）；M5 暂停无自动恢复，唯一路径
  :func:`rampup.clear_risk_pause`（用户决策，控制台触发），恢复后自动补一轮
  对账 [§13]；
- **主循环 tick 5s**：命令消费 → 熔断同步 → 时钟跳变检测 → 周期任务调度
  （轮询 3-5min 随机 / 对账每日+恢复后 / 投递闸门）→ 队列排空执行；
- **防休眠** [G11/Q21]：运行时段内 ``SetThreadExecutionState(ES_CONTINUOUS |
  ES_SYSTEM_REQUIRED)``，时段结束/退出释放（:func:`windows_keep_awake`，
  非 Windows 平台无操作）；
- **时钟跳变检测** [B18]：单调钟与墙钟偏差 > 5min → 恢复任务前先
  ``check_login_state + detect_risk``（强制检测通过才继续执行）；
- **dry_run 联动** [FR3.9]：发送类动作的 dry_run 分支在 messenger/delivery
  内实现（拟发送记录不触站点）；本模块提供 ``status()`` 快照（含 dry_run）
  供控制台总览呈现。

S5d 装配示例（详见各方法 docstring）::

    db = open_database(config.data.db_path)
    adapter = create_adapter(config.site.adapter, ...)
    orch = Orchestrator(db, adapter, config_loader=lambda: load_config())
    msgr = Messenger(db, adapter,
                     config_provider=lambda: orch.config,
                     gate_provider=lambda: orch.gate, ...)
    orch.bind_messenger(msgr)          # 自动绑定 REPLY 入队口
    thread = threading.Thread(target=orch.run_forever, daemon=True)

投递流水线（S4c）经 ``bind_delivery(fn)`` 注入；浏览器重建/登录模式经
``relaunch_hook`` / ``login_mode_hook`` 注入（S4b/S5d）。
"""

from __future__ import annotations

import heapq
import logging
import queue
import random
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum
from typing import Any, Literal, cast

from job_agent.config import Config, load_config
from job_agent.core import dryrun, events, quota, rampup, runstate
from job_agent.core.adapter_api import AdapterAPI, TaskFn, classify_adapter_error, risk_signal_of
from job_agent.core.messenger import Messenger, PollReport
from job_agent.core.scheduler import (
    Gate,
    LimitsView,
    QuotaAPI,
    effective_polling_schedule,
    utc_clock,
)
from job_agent.db import Database
from job_agent.models import LoginState, RiskSignal
from job_agent.notify.center import center_for
from job_agent.timeutil import beijing_date_str, utc_str

__all__ = [
    "BACKLOG_WARN_S",
    "CLOCK_JUMP_TOLERANCE",
    "COMMANDS",
    "KEY_ENGINE_STATE",
    "KEY_LAST_COMMAND",
    "KEY_PLATFORM_LIMIT_DATE",
    "TICK_S",
    "EngineCommand",
    "Orchestrator",
    "Priority",
    "make_command_queue",
    "windows_keep_awake",
]

logger = logging.getLogger(__name__)

# ---- 常量 ----

#: 主循环 tick 间隔（秒）[B5]
TICK_S = 5.0
#: 队列积压告警阈值（秒）[§5.3]
BACKLOG_WARN_S = 30.0
#: 时钟跳变容忍偏差（单调钟 vs 墙钟）[B18]
CLOCK_JUMP_TOLERANCE = timedelta(minutes=5)
#: 闸门未开时投递调度的复查间隔（秒）
_DELIVER_RETRY_S = 30.0

# run_state 键（S5 控制台可直读）
KEY_ENGINE_STATE = "engine_state"
KEY_LAST_COMMAND = "last_command"
#: 平台限额当日标记（北京日文本；S4c 投递闸门读取同键 [B6]）
KEY_PLATFORM_LIMIT_DATE = "platform_limit_date"

#: I6 指令枚举（EngineCommand.cmd 的合法值）。
#: ``clear_risk_pause`` 为 S5c 增补（M5 风控暂停的唯一用户恢复通道——控制台
#: 经队列触发，本线程调用 :func:`rampup.clear_risk_pause`；设计文档 §5.5 I6
#: 原表未列，集成时同步）。
COMMANDS: tuple[str, ...] = (
    "start",
    "stop",
    "pause",
    "resume",
    "relaunch_browser",
    "login_mode",
    "reload_config",
    "approve_outbox",
    "skip_outbox",
    "clear_risk_pause",
)

#: M5 风控类信号（→ rampup.handle_risk_pause 统一处置）
_M5_SIGNALS = frozenset(
    {RiskSignal.CAPTCHA, RiskSignal.RISK_PAGE, RiskSignal.EVICTED, RiskSignal.SECURITY_CHECK}
)


# ---- I6 指令对象（§5.5 权威字段：必填在前，默认值在后）----


@dataclass
class EngineCommand:
    """控制台 → 引擎指令（§5.5 I6；队列线程安全，执行结果写 DB 不回传）。"""

    cmd: Literal[
        "start",
        "stop",
        "pause",
        "resume",
        "relaunch_browser",
        "login_mode",
        "reload_config",
        "approve_outbox",
        "skip_outbox",
        "clear_risk_pause",
    ]
    req_id: str  # uuid4
    issued_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)  # 如 outbox_id / closed


def make_command_queue() -> queue.Queue[EngineCommand]:
    """构造指令队列（控制台线程 put，引擎线程每 tick 消费）。"""
    return queue.Queue()


# ---- 优先级（§5.3 [B5]：小值先执行）----


class Priority(IntEnum):
    """单执行者队列的四个优先级（§5.3：检测 > 回复 > 投递 > 浏览）。"""

    DETECT = 0  # 登录/风控检测
    REPLY = 1  # 回复发送（文本回复/附件简历/人工确认发送）
    DELIVER = 2  # 投递流水线（S4c）
    BROWSE = 3  # 浏览采集（消息轮询/对账等只读站点操作）


@dataclass
class _Task:
    """队列元素（fn 为无参任务体；结果一律落库 [I6]）。"""

    name: str
    fn: TaskFn
    paced: bool  # 发送类任务：执行前施加 8-20s 拟人节流 [§5.3]
    enqueued_at: datetime


def windows_keep_awake(active: bool) -> bool:
    """``SetThreadExecutionState`` 封装 [G11/Q21]。

    时段内 = ``ES_CONTINUOUS | ES_SYSTEM_REQUIRED``（阻止系统休眠；本机存在
    电源/显卡驱动故障史，为稳定性刚需），时段结束/退出 = 仅 ``ES_CONTINUOUS``
    （释放）。非 Windows 平台返回 False（无操作）。
    """
    if sys.platform != "win32":
        return False
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    flags = es_continuous | (es_system_required if active else 0)
    return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))


def _log_poll(report: PollReport) -> None:
    level = logging.WARNING if report.errors else logging.INFO
    logger.log(level, "[S4d] 轮询完成：%r", report)


def _log_reconcile(count: int) -> None:
    logger.info("[§13] 对账完成：manual=%d", count)


# ---- 编排器 ----


class Orchestrator:
    """引擎编排器（主线程循环体；站点操作全部经单执行者队列串行执行）。"""

    def __init__(
        self,
        db: Database,
        adapter: AdapterAPI,
        *,
        config_loader: Callable[[], Config] | None = None,
        messenger: Messenger | None = None,
        delivery: Callable[[datetime], None] | None = None,
        command_queue: queue.Queue[EngineCommand] | None = None,
        tick_s: float = TICK_S,
        clock: Callable[[], datetime] = utc_clock,
        mono: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[float, float], float] = random.uniform,
        awake_ctl: Callable[[bool], bool] = windows_keep_awake,
        relaunch_hook: Callable[[], bool] | None = None,
        login_mode_hook: Callable[[], None] | None = None,
        prerequisites_met: bool = True,
    ) -> None:
        self._db = db
        self._adapter = adapter
        self._config_loader = config_loader or load_config
        self._config = self._config_loader()
        self._messenger = messenger
        self._delivery = delivery
        self._queue = command_queue if command_queue is not None else make_command_queue()
        self._tick_s = tick_s
        self._clock = clock
        self._mono = mono
        self._sleep = sleep
        self._rng = rng
        self._awake_ctl = awake_ctl
        self._relaunch_hook = relaunch_hook
        self._login_mode_hook = login_mode_hook
        self._prereq_met = prerequisites_met

        # 引擎状态（须先于 _build_gate：后者经 _blocked() 读 _fatal/_state）
        self._state = "stopped"  # stopped | paused | running
        self._fatal: str | None = None  # login_expired | adapter_broken
        self._login_mode = False  # 登录窗口打开期间引擎互斥 [M3/G5]
        self._shutdown = False
        self._awake_active = False

        # 调度闸门（I7）：I1 配额经 quota 模块级装配绑定
        quota.configure(self._db, self._config)
        # 注：quota.Limits 为 frozen dataclass（只读属性），mypy 不认可其对
        # LimitsView（可写协议成员）的结构化子类型——运行期鸭子类型兼容，cast 消音
        self._quota_api = QuotaAPI(
            used_quota=quota.used_quota,
            browse_used=quota.browse_used,
            current_limits=cast("Callable[[datetime], LimitsView]", quota.current_limits),
            min_interval_s=quota.min_interval_s,
        )
        self._gate = self._build_gate()

        # 单执行者队列（submit 可能来自引擎线程；锁为防御性）
        self._heap: list[tuple[int, int, _Task]] = []
        self._seq = 0
        self._lock = threading.Lock()
        self._last_send_action_at: datetime | None = None

        # 周期调度游标
        self._next_poll_at = self._clock()  # 首轮立即轮询
        self._next_deliver_at = self._clock()
        self._last_reconcile_date: str | None = None
        self._reconcile_pending = False  # 熔断恢复后补一轮对账 [§13]

        # 检测与时钟
        self._last_detect_at: datetime | None = None
        self._clock_jumped = False
        self._last_wall: datetime | None = None
        self._last_mono: float | None = None
        self._risk_was_paused = False

        self._last_snapshot: tuple[Any, ...] | None = None

        if messenger is not None:
            self.bind_messenger(messenger)

    # ---- 属性 / 装配 ----

    @property
    def config(self) -> Config:
        """当前配置快照（reload_config 后整体替换）。"""
        return self._config

    @property
    def gate(self) -> Gate:
        """当前调度闸门（messenger 的 gate_provider 绑定本属性）。"""
        return self._gate

    @property
    def state(self) -> str:
        return self._state

    def bind_messenger(self, messenger: Messenger) -> None:
        """绑定消息引擎并自动接通 REPLY 入队口（节流标记 paced=True）。"""
        self._messenger = messenger
        messenger.set_reply_submitter(self.submit_reply)

    def bind_delivery(self, delivery: Callable[[datetime], None]) -> None:
        """绑定投递流水线（S4c；闸门与节奏由本模块 + 流水线自身共同把关）。"""
        self._delivery = delivery

    def set_prerequisites_met(self, met: bool) -> None:
        """更新启动前置条件标志（§4 清单；S5d 装配检测结果）。"""
        self._prereq_met = met
        self._gate.set_prerequisites_met(met)

    def submit(
        self, priority: Priority | int, name: str, fn: TaskFn, *, paced: bool = False
    ) -> None:
        """入队一个任务（优先级小值先执行；同优先级 FIFO）。"""
        with self._lock:
            self._seq += 1
            heapq.heappush(
                self._heap,
                (int(priority), self._seq, _Task(name, fn, paced, self._clock())),
            )

    def submit_reply(self, name: str, fn: TaskFn) -> None:
        """回复/简历/人工确认发送入队口（REPLY 优先级 + 拟人节流 [§5.3]）。"""
        self.submit(Priority.REPLY, name, fn, paced=True)

    def command_queue(self) -> queue.Queue[EngineCommand]:
        """指令队列（控制台线程持引用 put；引擎线程消费）。"""
        return self._queue

    def shutdown(self) -> None:
        """请求退出主循环（run_forever 在当前 tick 结束后返回）。"""
        self._shutdown = True

    # ---- 主循环 ----

    def run_forever(self) -> None:
        """主循环：tick（默认 5s）→ 消费命令 → 调度 → 排空队列执行。"""
        while not self._shutdown:
            try:
                self.tick()
            except Exception:
                logger.exception("tick 异常（记录后继续）")
            if self._shutdown:
                break
            self._sleep(self._tick_s)
        self._release_awake()
        logger.info("编排器主循环退出")

    def tick(self) -> None:
        """一个调度周期（测试可直接调用；时长 ≈ tick 间隔）。"""
        now = self._clock()
        self._consume_commands(now)
        self._track_risk_recovery()
        self._refresh_breaker()
        self._check_clock_jump(now)
        self._housekeeping(now)
        if self._state == "running" and not self._login_mode:
            if self._blocked():
                # 熔断期：fatal（登录失效/适配器失效）按 interval_min 探测恢复
                # （FR1.3：只探测零登录动作；M5 风控暂停不探测——唯一恢复路径
                # 是用户决策 clear_risk_pause，防 detect 重复触发告警）
                if self._fatal is not None:
                    self._probe_fatal_recovery(now)
                elif self._has_tasks():
                    self._clear_tasks("risk_paused")
            else:
                self._schedule_periodic(now)
                self._drain_tasks(now)
        elif self._has_tasks():
            self._clear_tasks("engine_inactive")
        # tick 内激活的熔断即时同步 Gate 与恢复探测标记（下一 tick 生效）
        self._track_risk_recovery()
        self._refresh_breaker()
        self._persist_state(now)

    def _probe_fatal_recovery(self, now: datetime) -> None:
        """fatal 期的恢复探测（interval_min 节流；探测健康即解除 fatal）。"""
        patrol = self._config.delivery.login_patrol
        if (
            self._last_detect_at is None
            or (now - self._last_detect_at).total_seconds() >= patrol.interval_min * 60
        ):
            self._detect(now)

    def status(self) -> dict[str, Any]:
        """引擎状态快照（控制台 GET /status 数据源；亦落 run_state.engine_state）。"""
        now = self._clock()
        return {
            "state": self._state,
            "fatal_reason": self._fatal,
            "risk_paused": self._risk_paused(),
            "login_mode": self._login_mode,
            "dry_run": dryrun.is_dry_run(self._db, self._config),
            "clock_jumped": self._clock_jumped,
            "last_detect_at": utc_str(self._last_detect_at) if self._last_detect_at else None,
            "queued_tasks": self._has_tasks(),
            "updated_at": utc_str(now),
        }

    # ---- 命令消费（I6）----

    def _consume_commands(self, now: datetime) -> None:
        while True:
            try:
                cmd = self._queue.get_nowait()
            except queue.Empty:
                return
            ok, detail = self._execute_command(cmd, now)
            runstate.set_state(
                self._db,
                KEY_LAST_COMMAND,
                {
                    "req_id": cmd.req_id,
                    "cmd": cmd.cmd,
                    "ok": ok,
                    "detail": detail,
                    "at": utc_str(now),
                },
            )
            logger.info("[I6] 命令 %s(%s) → ok=%s detail=%s", cmd.cmd, cmd.req_id, ok, detail)

    def _execute_command(self, cmd: EngineCommand, now: datetime) -> tuple[bool, str]:
        """执行单条指令；返回 (ok, detail)（落 run_state.last_command）。"""
        if cmd.cmd in ("start", "resume"):
            if self._login_mode:
                return False, "login_mode_open"  # 登录窗口期间引擎互斥 [M3/G5]
            previous = self._state
            self._state = "running"
            if cmd.cmd == "start":
                self._fatal = None  # 由本轮检测复核，失败会再次熔断
            return True, f"{previous}->running"
        if cmd.cmd == "stop":
            self._state = "stopped"
            self._fatal = None
            self._clear_tasks("stop")
            return True, "stopped"
        if cmd.cmd == "pause":
            self._state = "paused"
            self._clear_tasks("pause")
            return True, "paused"
        if cmd.cmd == "relaunch_browser":
            if self._relaunch_hook is None:
                return False, "no_hook"
            try:
                success = bool(self._relaunch_hook())
            except Exception as exc:
                return False, f"hook_error:{type(exc).__name__}"
            if success:
                if self._fatal == "adapter_broken":
                    self._fatal = None
                self.submit(Priority.DETECT, "detect:post_relaunch", self._detect_now_task)
            return True, f"relaunched={success}"
        if cmd.cmd == "login_mode":
            if cmd.payload.get("closed"):
                self._login_mode = False
                return True, "login_window_closed"
            if self._login_mode:
                return True, "already_open"
            if self._state == "running":
                return False, "engine_running"  # 先停引擎再开登录窗口
            if self._login_mode_hook is not None:
                try:
                    self._login_mode_hook()
                except Exception as exc:
                    return False, f"hook_error:{type(exc).__name__}"
            self._login_mode = True
            return True, "login_mode_open_engine_mutex"
        if cmd.cmd == "reload_config":
            self._reload_config()
            return True, "config_reloaded"
        if cmd.cmd == "approve_outbox":
            return self._approve_outbox(cmd, now)
        if cmd.cmd == "skip_outbox":
            detail = self._skip_outbox(cmd)
            return detail == "skipped", detail
        if cmd.cmd == "clear_risk_pause":
            # M5 风控暂停的唯一恢复入口（用户决策 → 控制台 → 队列 → 此处；
            # 幂等：未暂停时 no-op）。清除后 _track_risk_recovery 在下一 tick
            # 安排一轮对账 [§13]。
            was_paused = self._risk_paused()
            rampup.clear_risk_pause(
                self._db, decided_by=str(cmd.payload.get("decided_by", "console"))
            )
            return True, "risk_pause_cleared" if was_paused else "not_paused"
        return False, "unknown_command"

    def _approve_outbox(self, cmd: EngineCommand, now: datetime) -> tuple[bool, str]:
        entry_id = _entry_id(cmd)
        messenger = self._messenger
        if messenger is not None:
            detail = messenger.approve_outbox_entry(entry_id, now)
            return detail in ("approved_queued", "approved_dryrun"), detail
        row = self._db.query_one("SELECT state FROM outbox WHERE id = ?", (entry_id,))
        if row is None:
            return False, "not_found"
        if row["state"] not in ("pending", "stale"):
            return False, "invalid_state"
        self._db.execute("UPDATE outbox SET state = 'approved' WHERE id = ?", (entry_id,))
        return True, "approved_no_sender"

    def _skip_outbox(self, cmd: EngineCommand) -> str:
        entry_id = _entry_id(cmd)
        messenger = self._messenger
        if messenger is not None:
            return messenger.skip_outbox_entry(entry_id)
        row = self._db.query_one("SELECT state FROM outbox WHERE id = ?", (entry_id,))
        if row is None:
            return "not_found"
        if row["state"] not in ("pending", "stale"):
            return "invalid_state"
        self._db.execute("UPDATE outbox SET state = 'skipped' WHERE id = ?", (entry_id,))
        return "skipped"

    def _reload_config(self) -> None:
        """重载配置 [B14]：整体替换快照并重建 Gate；quota 重新绑定。"""
        self._config = self._config_loader()
        quota.configure(self._db, self._config)
        self._gate = self._build_gate()
        logger.info("配置已重载（下一轮调度循环生效）")

    def _build_gate(self) -> Gate:
        return Gate(
            self._config.delivery,
            self._quota_api,
            clock=self._clock,
            circuit_breaker=self._blocked(),
            prerequisites_met=self._prereq_met,
        )

    # ---- 熔断 / 检测 ----

    def _risk_paused(self) -> bool:
        return rampup.is_risk_paused(self._db)

    def _blocked(self) -> bool:
        """熔断总开关：M5 风控暂停 ∪ 引擎 fatal。"""
        return self._fatal is not None or self._risk_paused()

    def _refresh_breaker(self) -> None:
        """熔断状态 → Gate（v16 裁决：数据源 = is_risk_paused/risk_pause_info）。"""
        self._gate.set_circuit_breaker(self._blocked())

    def _track_risk_recovery(self) -> None:
        paused = self._risk_paused()
        if self._risk_was_paused and not paused:
            self._reconcile_pending = True  # 熔断恢复后对账一轮 [§13]
            logger.info("风控暂停已解除（用户决策），安排一轮对账")
        self._risk_was_paused = paused

    def _check_clock_jump(self, now: datetime) -> None:
        """时钟跳变检测 [B18]：单调钟与墙钟偏差 > 5min → 标记，恢复任务前先检测。"""
        mono = self._mono()
        if self._last_wall is not None and self._last_mono is not None:
            expected = self._last_wall + timedelta(seconds=mono - self._last_mono)
            if abs(now - expected) > CLOCK_JUMP_TOLERANCE:
                self._clock_jumped = True
                logger.warning(
                    "[B18] 检测到系统时钟跳变（墙钟偏差 > %s），恢复任务前先做登录/风控检测",
                    CLOCK_JUMP_TOLERANCE,
                )
        self._last_wall, self._last_mono = now, mono

    def _detect_if_due(self, now: datetime, *, force: bool = False) -> bool:
        """到期则执行检测；返回False=熔断中（调用方放弃本轮任务执行）。"""
        patrol = self._config.delivery.login_patrol
        due = (
            force
            or self._clock_jumped
            or patrol.every_loop  # 每任务循环前检测 [FR1.3]
            or self._last_detect_at is None
            or (now - self._last_detect_at).total_seconds() >= patrol.interval_min * 60
        )
        if not due:
            return True
        return self._detect(now)

    def _detect(self, now: datetime) -> bool:
        """登录态 + 风控检测（§5.2 错误表；返回False=熔断中）。"""
        login_ok = True
        try:
            login = self._adapter.check_login_state()
        except Exception as exc:
            login_ok = False
            self._handle_error("detect:login", exc, now)
        else:
            if login in (LoginState.LOGGED_OUT, LoginState.UNKNOWN):
                # S4a 契约：LOGGED_OUT/UNKNOWN 按登录失效熔断（零登录动作）
                login_ok = False
                self._set_fatal("login_expired", "LOGIN_EXPIRED", now, f"login_state={login.name}")
        try:
            risk = self._adapter.detect_risk()
        except Exception as exc:
            self._handle_error("detect:risk", exc, now)
        else:
            self._handle_risk_value(risk, now, login_ok=login_ok)
        self._last_detect_at = now
        self._clock_jumped = False
        return self._fatal is None and not self._risk_paused()

    def _detect_now_task(self) -> None:
        """DETECT 优先级的即时检测任务体（relaunch 后复核）。"""
        self._detect(self._clock())

    def _handle_risk_value(self, risk: RiskSignal, now: datetime, *, login_ok: bool) -> None:
        if risk in _M5_SIGNALS:
            rampup.handle_risk_signal(self._db, self._config, risk, now)  # M5：暂停+告警
            self._clear_tasks(f"risk:{risk.name}")
        elif risk == RiskSignal.LOGIN_EXPIRED:
            self._set_fatal("login_expired", "LOGIN_EXPIRED", now, f"risk_signal={risk.name}")
        elif risk == RiskSignal.ADAPTER_BROKEN:
            self._set_fatal("adapter_broken", "ADAPTER_BROKEN", now, f"risk_signal={risk.name}")
        elif risk == RiskSignal.PLATFORM_LIMIT:
            self._mark_platform_limit(now)
        elif login_ok and self._fatal is not None:
            # NONE 且本轮登录检查通过：fatal 自动解除（M5 risk_pause 不在此
            # 清除，唯一路径是 rampup.clear_risk_pause 用户决策）
            logger.info("检测恢复健康，解除 fatal=%s", self._fatal)
            self._fatal = None

    def _handle_error(self, context: str, exc: Exception, now: datetime) -> None:
        """任务/检测异常的 §5.2 错误表处置。"""
        kind = classify_adapter_error(exc)
        if kind == "login_required":
            self._set_fatal("login_expired", "LOGIN_EXPIRED", now, f"{context}:{exc}")
        elif kind == "risk_detected":
            signal = risk_signal_of(exc) or RiskSignal.RISK_PAGE
            rampup.handle_risk_signal(self._db, self._config, signal, now)
            self._clear_tasks(f"risk:{signal.name}")
        elif kind == "adapter_broken":
            self._set_fatal("adapter_broken", "ADAPTER_BROKEN", now, f"{context}:{exc}")
        elif kind == "platform_limit":
            self._mark_platform_limit(now)  # 当日停投，消息处理照常 [B6]
        else:
            logger.warning("[NFR2] %s 失败跳过（%s：%s）", context, type(exc).__name__, exc)

    def _set_fatal(self, reason: str, event: str, now: datetime, detail: str) -> None:
        """置引擎 fatal（全停）；同一原因不重复告警。"""
        if self._fatal == reason:
            return
        self._fatal = reason
        self._clear_tasks(reason)
        events.emit(
            self._db,
            event,
            {"reason": reason, "detail": detail[:200], "action": "stop_all", "at": utc_str(now)},
        )
        logger.error("引擎熔断（%s）：%s", reason, detail)

    def _mark_platform_limit(self, now: datetime) -> None:
        """平台限额 [B6]：当日停投（S4c 读 run_state 同键）；中级通知每日一次。"""
        today = beijing_date_str(now)
        if runstate.get_state(self._db, KEY_PLATFORM_LIMIT_DATE) != today:
            runstate.set_state(self._db, KEY_PLATFORM_LIMIT_DATE, today)
            events.emit(
                self._db, "PLATFORM_LIMIT", {"date": today, "action": "stop_delivery_today"}
            )

    # ---- 周期调度 ----

    def _schedule_periodic(self, now: datetime) -> None:
        messenger = self._messenger
        if messenger is not None and now >= self._next_poll_at:
            if self._gate.can_poll(now):  # 轮询时段（可独立于投递 [P1]）
                self.submit(Priority.BROWSE, "poll", lambda: _log_poll(messenger.poll_once()))
            lo, hi = self._config.delivery.polling.interval_range_min
            self._next_poll_at = now + timedelta(seconds=self._rng(float(lo), float(hi)) * 60)
        if messenger is not None and (
            self._last_reconcile_date != beijing_date_str(now) or self._reconcile_pending
        ):
            # 对账：每日一次 + 熔断恢复后 [§13]
            self.submit(
                Priority.BROWSE,
                "reconcile",
                lambda: _log_reconcile(_reconcile_count(messenger.reconcile_once())),
            )
            self._last_reconcile_date = beijing_date_str(now)
            self._reconcile_pending = False
        delivery = self._delivery
        if delivery is not None and now >= self._next_deliver_at:
            if self._gate.can_deliver(now).allowed and self._gate.can_browse("search", now).allowed:
                self.submit(Priority.DELIVER, "delivery", _bind_at(delivery, now))
                lo, hi = self._config.delivery.action_delay_range_s
                self._next_deliver_at = now + timedelta(seconds=self._rng(float(lo), float(hi)))
            else:
                self._next_deliver_at = now + timedelta(seconds=_DELIVER_RETRY_S)

    # ---- 队列执行 ----

    def _has_tasks(self) -> bool:
        with self._lock:
            return bool(self._heap)

    def _clear_tasks(self, reason: str) -> None:
        with self._lock:
            dropped = [task.name for _, _, task in self._heap]
            self._heap.clear()
        if dropped:
            logger.info("任务队列清空（%s）：%s", reason, dropped)

    def _pop_task(self) -> _Task | None:
        with self._lock:
            if not self._heap:
                return None
            _priority, _seq, task = heapq.heappop(self._heap)
            return task

    def _drain_tasks(self, now: datetime) -> None:
        """排空队列（按优先级）；检测先行（every_loop/到期/时钟跳变）。"""
        if not self._has_tasks():
            return
        if not self._detect_if_due(now):
            self._clear_tasks("breaker_active")
            return
        while True:
            if self._state != "running" or self._login_mode or self._blocked():
                self._clear_tasks("paused_or_breaker")
                return
            if self._clock_jumped and not self._detect_if_due(self._clock(), force=True):
                self._clear_tasks("clock_jump_detect_failed")
                return
            task = self._pop_task()
            if task is None:
                return
            self._note_backlog(task)
            if task.paced:
                self._pace()
            self._run_task(task)
            if task.paced:
                self._last_send_action_at = self._clock()
            self._maybe_redetect_mid_batch(self._clock())

    def _maybe_redetect_mid_batch(self, now: datetime) -> None:
        """长批次中的周期检测（interval_min，FR1.3 循环中每 N 分钟）。"""
        patrol = self._config.delivery.login_patrol
        if (
            self._last_detect_at is not None
            and (now - self._last_detect_at).total_seconds() >= patrol.interval_min * 60
        ):
            self._detect(now)

    def _run_task(self, task: _Task) -> None:
        try:
            task.fn()
        except Exception as exc:
            self._handle_error(f"task:{task.name}", exc, self._clock())

    def _note_backlog(self, task: _Task) -> None:
        age = (self._clock() - task.enqueued_at).total_seconds()
        if age > BACKLOG_WARN_S:
            logger.warning("[B5] 任务 %s 积压 %.0fs（> %.0fs）", task.name, age, BACKLOG_WARN_S)

    def _pace(self) -> None:
        """发送类任务的拟人节流 [§5.3]：与投递共用 8-20s 随机节奏参数。"""
        lo, hi = self._config.delivery.action_delay_range_s
        wait = self._rng(float(lo), float(hi))
        last = self._last_send_action_at
        if last is not None:
            elapsed = (self._clock() - last).total_seconds()
            if elapsed < wait:
                self._sleep(wait - elapsed)

    # ---- 杂项 ----

    def _housekeeping(self, now: datetime) -> None:
        self._update_awake(now)
        center = center_for(self._db)
        if center is not None:
            center.flush_deferred(now=now)  # 免打扰合并推送 [B11]（窗口结束才动作）

    def _update_awake(self, now: datetime) -> None:
        delivery = self._config.delivery
        in_window = delivery.run_schedule.is_active(now) or effective_polling_schedule(
            delivery
        ).is_active(now)
        active = self._state == "running" and in_window and not self._shutdown
        if active != self._awake_active:
            try:
                self._awake_ctl(active)
            except Exception:
                logger.warning("SetThreadExecutionState 调用失败", exc_info=True)
            self._awake_active = active

    def _release_awake(self) -> None:
        if self._awake_active:
            try:
                self._awake_ctl(False)
            except Exception:
                logger.warning("释放防休眠状态失败", exc_info=True)
            self._awake_active = False

    def _persist_state(self, now: datetime) -> None:
        snapshot = self.status()
        stable = (
            snapshot["state"],
            snapshot["fatal_reason"],
            snapshot["risk_paused"],
            snapshot["login_mode"],
            snapshot["dry_run"],
        )
        if stable == self._last_snapshot:
            return
        self._last_snapshot = stable
        runstate.set_state(self._db, KEY_ENGINE_STATE, snapshot)


def _entry_id(cmd: EngineCommand) -> int:
    """取 payload.outbox_id（int；缺失/非法 → 0 → not_found）。"""
    try:
        return int(cmd.payload.get("outbox_id", 0))
    except (TypeError, ValueError):
        return 0


def _bind_at(fn: Callable[[datetime], None], at: datetime) -> TaskFn:
    def run() -> None:
        fn(at)

    return run


def _reconcile_count(report: Any) -> int:
    """对账报告 → manual 消息数（None/无报告 → 0，日志用）。"""
    if report is None:
        return 0
    return len(getattr(report, "manual_messages", ()))
