"""调度器：运行时段 / 轮询时段 / 组合闸门（设计文档 §5.5 I7；§7 步 0；S2c 任务书）。

纯逻辑、零 IO（验收标准 A-CORE-3/9：禁止在调度器内做任何 IO）——计数、
限额折算、最近操作时刻、时钟全部经构造参数注入：

- 时段判定：``RunSchedule.is_active(now)``（S2c 契约，实现在 config.py——
  RunSchedule 类的归属地）：weekday∈配置（ISO 1=周一…7=周日，"工作日=周一~五"
  是默认配置而非硬编码，不处理节假日 [s2]）、start≤now<end；轮询时段可独立
  （polling.schedule None=共用投递时段）[P1]；
- I1 频率与浏览配额（S2b quota.py 提供）：经 :class:`QuotaAPI` 注入
  ``used_quota`` / ``browse_used`` / ``current_limits`` / ``min_interval_s``
  ——S2b 已交付前三者中除 ``current_limits`` 外的模块级入口（签名与
  :class:`QuotaAPI` 字段一致，直接绑定即可）；``current_limits`` 由 S2b
  quota.py 显式推迟（依赖 S2d 的 I9 ``browse_tier`` 放量档位），交付后经
  同一注入位接入，本模块无需改动。
  :class:`LimitsView` 是 ``current_limits`` 返回值的结构视图（结构匹配即可，
  无需继承），字段建议与 config.delivery 同名：``hourly_limit`` /
  ``daily_limit``（投递；日限已含放量期折算）、``detail_hourly`` /
  ``detail_daily`` / ``search_page_hourly``（浏览；已含试运行放量档位 [H2]
  与 Chrome 降档 [V9] 折算）——调用方一律用折算值，禁止直读配置；
- 熔断标志 / 前置条件是布尔输入参数（引擎 S4d 注入；本 SubStage 不实现
  熔断本身）；
- 时间注入：``clock: Callable[[], datetime]``（测试传固定时间 [T4-7]）；
  三个闸门方法亦可显式传 ``now``（I7 签名，缺省取 clock()）。naive 时间
  一律视为 UTC（timeutil 契约）。

S4c / S4d 装配示例::

    from datetime import UTC, datetime

    from job_agent.core import quota  # S2b 交付（I1）
    from job_agent.core.scheduler import Gate, QuotaAPI

    quota_api = QuotaAPI(
        used_quota=quota.used_quota,       # 模块级函数直接注入
        browse_used=quota.browse_used,
        current_limits=quota.current_limits,
        min_interval_s=quota.min_interval_s,
    )
    gate = Gate(config.delivery, quota_api, clock=lambda: datetime.now(UTC))

    # 投递流水线闸门（§7 步 0）= 投递闸门 ∧ 浏览闸门
    now = datetime.now(UTC)
    if gate.can_deliver(now) and gate.can_browse("search", now):
        ...  # 搜索翻页；操作完成后 gate.record_action("search", now)
    # 拟人随机延时与最小间隔下限取大者（H2：随机节流值 < 30s 时仍强制 30s）：
    # wait_s = max(random.uniform(*cfg.action_delay_range_s),
    #              gate.min_interval_remaining("detail", now))

线程模型：按设计 §5.3 单执行者队列串行调用，不加锁。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from job_agent.config import DeliveryConfig, RunSchedule

__all__ = [
    "ActionKind",
    "BrowseCountKind",
    "Gate",
    "GateResult",
    "LimitsView",
    "QuotaAPI",
    "QuotaKind",
    "effective_polling_schedule",
    "utc_clock",
]

# 操作类型（I7 can_browse / I1 min_interval_s 的取值）
ActionKind = Literal["detail", "search"]
# 投递限额窗口（I1 used_quota 的取值；R6-4：daily=北京自然日、hourly=滑动 60 分钟）
QuotaKind = Literal["hourly", "daily"]
# 浏览计数窗口（I1 browse_used 的取值）
BrowseCountKind = Literal["detail_hourly", "detail_daily", "search_page_hourly"]


def utc_clock() -> datetime:
    """默认时钟：当前 UTC（tz-aware）。测试注入固定时间用（T4-7）。"""
    return datetime.now(UTC)


def _as_utc(dt: datetime) -> datetime:
    """naive 一律视为 UTC（timeutil 契约）；aware 原样返回（仅做间隔差运算）。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class LimitsView(Protocol):
    """I1 ``current_limits`` 返回值的结构视图（S2b quota.Limits 的最小依赖面）。

    五个属性 = config.delivery 同名项折算后的"当前生效上限"：
    投递 ``hourly_limit`` / ``daily_limit``（日限已含放量期 ramp_up 折算，
    期间=10 而非配置值）、浏览 ``detail_hourly`` / ``detail_daily`` /
    ``search_page_hourly``（已含试运行放量档位 [H2]：首日 15/60、无风控日
    翻倍至配置值、风控回首档；Chrome 主版本变化降档 [V9] 覆盖为 15/60）。
    S2b 的 Limits 具备这五个属性即被本模块接受（结构化匹配，无需继承）。
    """

    hourly_limit: int
    daily_limit: int
    detail_hourly: int
    detail_daily: int
    search_page_hourly: int


@dataclass(frozen=True)
class QuotaAPI:
    """I1（S2b quota.py）四函数的注入束——调度器禁 IO，计数一律经此获取。

    S2b 落地后按模块级函数直接装配（见模块 docstring 示例）；
    单元测试以可编程假实现替代（A-CORE-3/21 的"限额已用 N"由此注入）。
    """

    used_quota: Callable[[QuotaKind, datetime], int]
    browse_used: Callable[[BrowseCountKind, datetime], int]
    current_limits: Callable[[datetime], LimitsView]
    min_interval_s: Callable[[ActionKind], float]


@dataclass(frozen=True)
class GateResult:
    """闸门判定结果（§5.5 I7：dataclass(allowed, reason)）。

    ``reason`` 为稳定 snake_case 码，供日志与"为何停"面板映射文案
    （与 core.rules.DedupDecision 同一约定）。取值：

    - ``ok``：放行；
    - ``circuit_breaker``：熔断中（登录失效/风控信号，全部任务暂停）；
    - ``prerequisites_not_met``：启动前置条件未满足（A-CORE-18 五前置）；
    - ``out_of_delivery_schedule``：不在投递运行时段（含非工作日）；
    - ``hourly_limit_reached`` / ``daily_limit_reached``：投递小时（滑动
      60 分钟）/ 日（北京自然日，含放量期折算）限额已达；
    - ``browse_detail_hourly_limit`` / ``browse_detail_daily_limit`` /
      ``browse_search_hourly_limit``：浏览上限已达（经 I1 折算后的档位值）；
    - ``min_interval``：操作级最小间隔未满 [H2]。
    """

    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        """兼容布尔用法（``if gate.can_deliver(now)``）：真值即放行。"""
        return self.allowed


def effective_polling_schedule(delivery: DeliveryConfig) -> RunSchedule:
    """轮询生效时段：``polling.schedule`` 独立配置，None=共用投递时段 [P1]。"""
    schedule = delivery.polling.schedule
    return delivery.run_schedule if schedule is None else schedule


class Gate:
    """组合闸门（§5.5 I7）。纯逻辑、零 IO，全部外部事实经构造参数注入。

    - :meth:`can_deliver`：熔断 ∧ 前置条件 ∧ 投递时段 ∧ 投递小时限额 ∧
      投递日限额（放量期日限折算经 ``current_limits().daily_limit``）；
    - :meth:`can_browse`：熔断 ∧ 浏览上限（一律经 I1.current_limits 综合
      放量档位 [H2] / Chrome 降档 [V9] 折算，禁止直读配置）∧ 操作级最小间隔
      （I1.min_interval_s：详情/搜索 ≥30s，独立于 8-20s 随机节流之外的下限
      [H2]）；
    - :meth:`can_poll`：轮询时段判定（可独立于投递时段 [P1]；I7 返回 bool，
      熔断全停由编排器统一执行，不在此重复）。

    设计 §7 的流水线闸门 = ``can_deliver(now) ∧ can_browse(kind, now)``
    （投递必须同时有对应浏览余量），由 S4c 组合调用。

    熔断/前置是布尔输入（S4d 注入），运行期经 :meth:`set_circuit_breaker` /
    :meth:`set_prerequisites_met` 更新——Gate 设计为长生命周期对象，内置的
    最小间隔跟踪依赖它（也可注入 ``last_action_at`` 提供方接持久化记录）。
    """

    def __init__(
        self,
        delivery: DeliveryConfig,
        quota: QuotaAPI,
        clock: Callable[[], datetime] = utc_clock,
        *,
        circuit_breaker: bool = False,
        prerequisites_met: bool = True,
        last_action_at: Callable[[ActionKind], datetime | None] | None = None,
    ) -> None:
        self._delivery = delivery
        self._quota = quota
        self._clock = clock
        self._circuit_breaker = circuit_breaker
        self._prerequisites_met = prerequisites_met
        self._last_action_at = last_action_at
        self._recent_action: dict[ActionKind, datetime] = {}

    # ---- 可变布尔输入（S4d 注入/更新；本模块不实现熔断本身）----

    def set_circuit_breaker(self, active: bool) -> None:
        """更新熔断标志（登录失效/风控信号置位；恢复由引擎/人工决策驱动）。"""
        self._circuit_breaker = active

    def set_prerequisites_met(self, met: bool) -> None:
        """更新启动前置条件标志（AI 连通/简历/方向/招呼语/登录态，A-CORE-18）。"""
        self._prerequisites_met = met

    # ---- 操作级最小间隔 [H2] ----

    def record_action(self, kind: ActionKind, at: datetime | None = None) -> None:
        """记录一次 ``kind`` 操作的发生时刻（缺省取 clock()）。

        供 :meth:`can_browse` 的最小间隔判定与 :meth:`min_interval_remaining`
        使用。注入了 ``last_action_at`` 提供方（S4c 接持久化，重启不丢）时
        以提供方为准，本方法仅更新内置副本。
        """
        self._recent_action[kind] = _as_utc(at if at is not None else self._clock())

    def _last_at(self, kind: ActionKind) -> datetime | None:
        """最近一次 ``kind`` 操作时刻：优先外部提供方，否则内置跟踪。"""
        if self._last_action_at is not None:
            last = self._last_action_at(kind)
            return None if last is None else _as_utc(last)
        return self._recent_action.get(kind)

    def min_interval_remaining(self, kind: ActionKind, now: datetime | None = None) -> float:
        """距 ``kind`` 下一次允许操作还需等待的秒数（0 = 已满足）[H2]。

        引擎侧拟人随机延时与它取大者：``wait = max(random.uniform(8, 20),
        min_interval_remaining(kind))``——随机节流值 < 30s 时仍强制 30s。
        无记录（如重启后且未注入提供方）返回 0，不构成额外约束。
        """
        last = self._last_at(kind)
        if last is None:
            return 0.0
        elapsed = (self._now(now) - last).total_seconds()
        return max(0.0, self._quota.min_interval_s(kind) - elapsed)

    # ---- I7 三方法 ----

    def can_deliver(self, now: datetime | None = None) -> GateResult:
        """投递闸门（§5.5 I7）：熔断 ∧ 前置 ∧ 时段 ∧ 小时限额 ∧ 日限额。"""
        current = self._now(now)
        if self._circuit_breaker:
            return GateResult(False, "circuit_breaker")
        if not self._prerequisites_met:
            return GateResult(False, "prerequisites_not_met")
        if not self._delivery.run_schedule.is_active(current):
            return GateResult(False, "out_of_delivery_schedule")
        limits = self._quota.current_limits(current)
        if self._quota.used_quota("hourly", current) >= limits.hourly_limit:
            return GateResult(False, "hourly_limit_reached")
        if self._quota.used_quota("daily", current) >= limits.daily_limit:
            return GateResult(False, "daily_limit_reached")
        return GateResult(True, "ok")

    def can_browse(self, kind: ActionKind, now: datetime | None = None) -> GateResult:
        """浏览闸门（§5.5 I7）：熔断 ∧ 浏览上限（经 I1 折算）∧ 最小间隔 [H2]。

        ``detail`` 受详情小时/日双上限约束；``search`` 仅受翻页小时上限约束
        （配置与 I1 均无搜索日限字段）。时段/前置不在此判定：浏览发生在
        投递循环（已过 can_deliver）或轮询循环（已过 can_poll）内，
        本方法只做浏览自身的限额与节奏检查。
        """
        current = self._now(now)
        if self._circuit_breaker:
            return GateResult(False, "circuit_breaker")
        limits = self._quota.current_limits(current)
        if kind == "detail":
            if self._quota.browse_used("detail_hourly", current) >= limits.detail_hourly:
                return GateResult(False, "browse_detail_hourly_limit")
            if self._quota.browse_used("detail_daily", current) >= limits.detail_daily:
                return GateResult(False, "browse_detail_daily_limit")
        elif self._quota.browse_used("search_page_hourly", current) >= limits.search_page_hourly:
            return GateResult(False, "browse_search_hourly_limit")
        if self.min_interval_remaining(kind, current) > 0:
            return GateResult(False, "min_interval")
        return GateResult(True, "ok")

    def can_poll(self, now: datetime | None = None) -> bool:
        """轮询时段判定（§5.5 I7）：polling.schedule 独立，None=共用投递 [P1]。"""
        return effective_polling_schedule(self._delivery).is_active(self._now(now))

    # ---- 内部 ----

    def _now(self, now: datetime | None) -> datetime:
        """显式 now 优先（I7 签名），缺省取注入时钟 [T4-7]；naive 视为 UTC。"""
        if now is not None:
            return _as_utc(now)
        return _as_utc(self._clock())
