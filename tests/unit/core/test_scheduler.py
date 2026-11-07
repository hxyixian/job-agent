"""S2c 调度器单元测试（全部离线、时钟注入固定时间；A-CORE-3/9/21）。

覆盖：

- ``RunSchedule.is_active`` 边界：start≤now<end、周五 16:01 不投递、周六不投、
  工作日集合来自配置（非硬编码）、naive=UTC、跨午夜不支持 [A-CORE-9/T4-7]；
- 轮询时段独立配置生效与 None 共用投递时段 [P1]；
- Gate 四级限额触发：投递小时（滑动 60 分钟）/ 日（放量期经 I1 折算）、
  详情小时/日、搜索翻页小时；一律使用 I1.current_limits 折算值（禁止直读
  配置）[A-CORE-3]；
- 最小间隔独立生效：随机节流值 < 30s 时仍强制 30s（值来自
  I1.min_interval_s）[H2/A-CORE-21]；
- 浏览放量档位翻倍与回落、Chrome 降档联动（经 I1 折算值执行；档位状态机
  属 S2d、折算属 S2b，此处按 §5.5.I1 签名以假实现注入）[H2/V9]；
- 熔断/前置布尔输入（S4d 注入）与时钟注入、now 透传（滑动窗口按调用时刻
  计算）。

S2b 未交付 quota.py：FakeQuota/FakeLimits 按 §5.5.I1 签名模拟（LimitsView
的结构实现），S2b 落地后本测试文件无需改动（引擎装配替换为真实实现即可）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from job_agent.config import DeliveryConfig, PollingConfig, RunSchedule
from job_agent.core.scheduler import (
    ActionKind,
    BrowseCountKind,
    Gate,
    GateResult,
    QuotaAPI,
    QuotaKind,
)
from job_agent.timeutil import CST

# 固定时刻基准（各用例内另有 isoweekday 断言防夹具漂移）：
# 2026-11-03=周二 / 2026-11-06=周五 / 2026-11-07=周六
_TUE = (2026, 11, 3)


def cst(*args: int) -> datetime:
    """构造北京时间的 tz-aware datetime（运行时段以北京时刻表述，R6-4）。"""
    return datetime(*args, tzinfo=CST)


# ---- I1 假实现（S2b quota.py 未交付，按 §5.5.I1 签名模拟）----


@dataclass
class FakeLimits:
    """LimitsView 的结构实现：模拟 current_limits 的折算结果（档位/降档）。"""

    hourly_limit: int = 25
    daily_limit: int = 100
    detail_hourly: int = 40
    detail_daily: int = 200
    search_page_hourly: int = 30


@dataclass
class FakeQuota:
    """I1 四函数的可编程假实现。

    ``*_used`` 模拟窗口计数（按调用方传入的 now 由 S2b 计算，此处直接给值）；
    ``limits`` 模拟折算后的生效上限（测试按档位场景切换）；``recorded_nows``
    记录透传的 now 供断言（滑动窗口必须按调用时刻计算）。
    """

    limits: FakeLimits = field(default_factory=FakeLimits)
    hourly_used: int = 0
    daily_used: int = 0
    detail_hourly_used: int = 0
    detail_daily_used: int = 0
    search_page_hourly_used: int = 0
    detail_interval_s: float = 30.0
    search_interval_s: float = 30.0
    recorded_nows: list[datetime] = field(default_factory=list)

    def used_quota(self, kind: QuotaKind, now: datetime) -> int:
        self.recorded_nows.append(now)
        return {"hourly": self.hourly_used, "daily": self.daily_used}[kind]

    def browse_used(self, kind: BrowseCountKind, now: datetime) -> int:
        self.recorded_nows.append(now)
        return {
            "detail_hourly": self.detail_hourly_used,
            "detail_daily": self.detail_daily_used,
            "search_page_hourly": self.search_page_hourly_used,
        }[kind]

    def current_limits(self, now: datetime) -> FakeLimits:
        self.recorded_nows.append(now)
        return self.limits

    def min_interval_s(self, action: ActionKind) -> float:
        return self.detail_interval_s if action == "detail" else self.search_interval_s

    def api(self) -> QuotaAPI:
        return QuotaAPI(self.used_quota, self.browse_used, self.current_limits, self.min_interval_s)


def make_gate(
    quota: FakeQuota,
    delivery: DeliveryConfig | None = None,
    clock: Callable[[], datetime] | None = None,
    **flags: bool,
) -> Gate:
    """装配 Gate：默认配置 + 注入假 I1 + 可选固定时钟/布尔标志（S4d 位）。"""
    return Gate(
        delivery or DeliveryConfig(),
        quota.api(),
        clock or (lambda: cst(*_TUE, 10, 0)),
        **flags,
    )


# ---- RunSchedule.is_active 边界（A-CORE-9）----


def test_schedule_boundary_start_inclusive_end_exclusive() -> None:
    """start≤now<end：起点含、终点不含（默认时段 周一~五 10:00-16:00）。"""
    assert cst(*_TUE, 10, 0).isoweekday() == 2  # 夹具防漂移：2026-11-03 是周二
    schedule = RunSchedule()
    assert schedule.is_active(cst(*_TUE, 9, 59, 59)) is False
    assert schedule.is_active(cst(*_TUE, 10, 0, 0)) is True
    assert schedule.is_active(cst(*_TUE, 15, 59, 59)) is True
    assert schedule.is_active(cst(*_TUE, 16, 0, 0)) is False
    assert schedule.is_active(cst(*_TUE, 16, 0, 30)) is False


def test_boundary_friday_after_end() -> None:
    """周五 16:01 不投递（工作日内但已过终点）。"""
    assert cst(2026, 11, 6, 16, 1).isoweekday() == 5  # 夹具防漂移：周五
    schedule = RunSchedule()
    assert schedule.is_active(cst(2026, 11, 6, 15, 59)) is True
    assert schedule.is_active(cst(2026, 11, 6, 16, 1)) is False


def test_boundary_saturday() -> None:
    """周六不投递（不在默认工作日集合 1-5）。"""
    assert cst(2026, 11, 7, 10, 0).isoweekday() == 6  # 夹具防漂移：周六
    assert RunSchedule().is_active(cst(2026, 11, 7, 10, 0)) is False


def test_weekdays_from_config_not_hardcoded() -> None:
    """工作日集合完全来自配置（如仅周六运行；不处理节假日/调休 [s2]）。"""
    weekend_only = RunSchedule(weekdays=[6])
    assert weekend_only.is_active(cst(2026, 11, 7, 10, 0)) is True  # 周六
    assert weekend_only.is_active(cst(*_TUE, 10, 0)) is False  # 周二


def test_schedule_naive_now_treated_as_utc() -> None:
    """naive 视为 UTC（timeutil 契约）：UTC 02:00 = 北京 10:00 在时段内。"""
    assert RunSchedule().is_active(datetime(*_TUE, 2, 0)) is True
    assert RunSchedule().is_active(datetime(*_TUE, 9, 0)) is False  # 北京 17:00


def test_schedule_cross_midnight_not_supported() -> None:
    """RunSchedule 不支持跨午夜（区别于 notify.dnd）：start≥end 恒 False。"""
    late = RunSchedule(start="22:00", end="06:00")
    assert late.is_active(cst(*_TUE, 23, 0)) is False
    assert late.is_active(cst(*_TUE, 2, 0)) is False


# ---- 轮询时段独立（P1 / A-CORE-9）----


def test_polling_none_inherits_delivery_schedule() -> None:
    """polling.schedule=None（默认）→ 轮询共用投递时段。"""
    gate = make_gate(FakeQuota())  # DeliveryConfig 默认：polling.schedule=None
    assert gate.can_poll(cst(*_TUE, 15, 59)) is True
    assert gate.can_poll(cst(*_TUE, 16, 0)) is False
    assert gate.can_poll(cst(2026, 11, 7, 10, 0)) is False  # 周六


def test_boundary_polling_independent_schedule() -> None:
    """轮询独立时段生效：可比投递时段更长、工作日集合也可不同。"""
    delivery = DeliveryConfig(
        polling=PollingConfig(
            schedule=RunSchedule(weekdays=[1, 2, 3, 4, 5], start="09:00", end="21:00")
        )
    )
    gate = make_gate(FakeQuota(), delivery=delivery)
    # 投递 16:00 结束、轮询独立到 21:00 → 20:00 可轮询不可投递
    assert gate.can_poll(cst(*_TUE, 20, 0)) is True
    assert gate.can_deliver(cst(*_TUE, 20, 0)).reason == "out_of_delivery_schedule"
    # 轮询同样受自己的时段约束：时段外/周六不轮询
    assert gate.can_poll(cst(*_TUE, 21, 0)) is False
    assert gate.can_poll(cst(2026, 11, 7, 10, 0)) is False
    # 轮询工作日集合独立于投递（仅周六轮询）
    saturday_polling = make_gate(
        FakeQuota(),
        delivery=DeliveryConfig(polling=PollingConfig(schedule=RunSchedule(weekdays=[6]))),
    )
    assert saturday_polling.can_poll(cst(2026, 11, 7, 10, 0)) is True
    assert saturday_polling.can_poll(cst(*_TUE, 10, 0)) is False


# ---- 时钟注入与 now 透传（T4-7 / R6-4）----


def test_clock_injection_used_when_now_omitted() -> None:
    """方法缺省 now 时取注入时钟（测试传固定时间，T4-7）。"""
    friday_evening = make_gate(FakeQuota(), clock=lambda: cst(2026, 11, 6, 16, 1))
    assert friday_evening.can_deliver().reason == "out_of_delivery_schedule"
    assert friday_evening.can_poll() is False
    tuesday = make_gate(FakeQuota(), clock=lambda: cst(*_TUE, 10, 0))
    assert tuesday.can_deliver().allowed is True
    assert tuesday.can_poll() is True


def test_now_forwarded_to_quota_calls_unchanged() -> None:
    """滑动窗口/北京自然日窗口按调用方传入的 now 计算（透传不改写）。"""
    quota = FakeQuota()
    gate = make_gate(quota)
    probe = cst(*_TUE, 12, 0)
    gate.can_deliver(probe)
    gate.can_browse("detail", probe)
    assert quota.recorded_nows
    assert all(t == probe for t in quota.recorded_nows)


def test_can_poll_is_pure_schedule_check() -> None:
    """I7：can_poll 仅判时段；熔断全停由编排器（S4d）统一执行，不在此重复。"""
    gate = make_gate(FakeQuota(), circuit_breaker=True)
    assert gate.can_poll(cst(*_TUE, 10, 0)) is True


# ---- 投递闸门：四级限额触发（A-CORE-3 / FR3.4）----


def test_deliver_allowed_when_all_clear() -> None:
    result = make_gate(FakeQuota()).can_deliver(cst(*_TUE, 10, 0))
    assert result.allowed is True
    assert result.reason == "ok"
    assert bool(result) is True  # GateResult 布尔用法


def test_deliver_hourly_limit_reached() -> None:
    """小时限额（滑动 60 分钟）达上限即拒。"""
    quota = FakeQuota(hourly_used=25)  # limits.hourly_limit=25
    gate = make_gate(quota)
    result = gate.can_deliver(cst(*_TUE, 10, 0))
    assert (result.allowed, result.reason) == (False, "hourly_limit_reached")
    quota.hourly_used = 24
    assert gate.can_deliver(cst(*_TUE, 10, 0)).allowed is True


def test_deliver_daily_limit_uses_folded_rampup_value() -> None:
    """日限额用 I1 折算值（放量期=10），不直读配置 daily_limit=100。"""
    quota = FakeQuota(daily_used=10, limits=FakeLimits(daily_limit=10))
    gate = make_gate(quota)
    result = gate.can_deliver(cst(*_TUE, 10, 0))
    assert (result.allowed, result.reason) == (False, "daily_limit_reached")
    quota.daily_used = 9
    assert gate.can_deliver(cst(*_TUE, 10, 0)).allowed is True


def test_sliding_window_boundary_reconsulted_per_call() -> None:
    """滑动窗口边界：限额 3；满 3 拒绝；窗口滑出旧记录后（61 分钟）放行。"""
    quota = FakeQuota(hourly_used=3, limits=FakeLimits(hourly_limit=3))
    gate = make_gate(quota)
    now0 = cst(*_TUE, 10, 0)
    assert gate.can_deliver(now0).reason == "hourly_limit_reached"
    # S2b 按传入的更晚 now 重算窗口：旧记录滑出 → 计数 2
    quota.hourly_used = 2
    assert gate.can_deliver(now0 + timedelta(minutes=61)).allowed is True


def test_circuit_breaker_blocks_delivery_and_browse() -> None:
    """熔断布尔输入（S4d 注入）：投递与浏览均拒绝；可运行期复位。"""
    gate = make_gate(FakeQuota(), circuit_breaker=True)
    now = cst(*_TUE, 10, 0)
    assert gate.can_deliver(now).reason == "circuit_breaker"
    assert gate.can_browse("detail", now).reason == "circuit_breaker"
    gate.set_circuit_breaker(False)
    assert gate.can_deliver(now).allowed is True


def test_prerequisites_block_delivery_only() -> None:
    """前置条件是投递启动闸门（A-CORE-18），不拦浏览。"""
    gate = make_gate(FakeQuota(), prerequisites_met=False)
    now = cst(*_TUE, 10, 0)
    assert gate.can_deliver(now).reason == "prerequisites_not_met"
    assert gate.can_browse("detail", now).allowed is True
    gate.set_prerequisites_met(True)
    assert gate.can_deliver(now).allowed is True


# ---- 浏览闸门：三级浏览上限（A-CORE-3 / G3）----


def test_browse_detail_hourly_and_daily_limits() -> None:
    quota = FakeQuota(detail_hourly_used=40)
    gate = make_gate(quota)
    now = cst(*_TUE, 10, 0)
    assert gate.can_browse("detail", now).reason == "browse_detail_hourly_limit"
    quota.detail_hourly_used = 0
    quota.detail_daily_used = 200
    assert gate.can_browse("detail", now).reason == "browse_detail_daily_limit"
    quota.detail_daily_used = 199
    assert gate.can_browse("detail", now).allowed is True


def test_browse_search_hourly_limit_only() -> None:
    """搜索翻页仅受小时上限约束（无日限字段）；详情日限计数不影响搜索。"""
    quota = FakeQuota(search_page_hourly_used=30)
    gate = make_gate(quota)
    now = cst(*_TUE, 10, 0)
    assert gate.can_browse("search", now).reason == "browse_search_hourly_limit"
    quota.search_page_hourly_used = 29
    assert gate.can_browse("search", now).allowed is True
    quota.detail_daily_used = 200  # 详情日限与搜索判定无关
    assert gate.can_browse("search", now).allowed is True


def test_browse_limits_via_current_limits_not_config() -> None:
    """浏览上限按 I1 折算值执行（配置默认 40/200/30，折算返回 15/60/30）。"""
    quota = FakeQuota(limits=FakeLimits(detail_hourly=15, detail_daily=60))
    gate = make_gate(quota)  # DeliveryConfig 默认 browse_limits=40/200/30
    now = cst(*_TUE, 10, 0)
    quota.detail_hourly_used = 14
    assert gate.can_browse("detail", now).allowed is True
    quota.detail_hourly_used = 15
    assert gate.can_browse("detail", now).reason == "browse_detail_hourly_limit"
    quota.detail_hourly_used = 0
    quota.detail_daily_used = 60
    assert gate.can_browse("detail", now).reason == "browse_detail_daily_limit"


# ---- 浏览放量档位翻倍与回落、Chrome 降档（H2 / V9 / A-CORE-21）----


def test_browse_rampup_tier_double_and_fallback() -> None:
    """档位折算值经 current_limits 生效：首日 15/60 → 翻倍 → 配置值封顶 → 回首档。

    档位状态机属 S2d（I9.browse_tier）、折算属 S2b（I1.current_limits）；
    本用例逐档注入折算值，验证闸门按折算值执行（配置 40/200 全程不变）。
    """
    quota = FakeQuota()
    gate = make_gate(quota)
    now = cst(*_TUE, 10, 0)

    # 第 1 档（试运行首日）：15/时·60/天
    quota.limits = FakeLimits(detail_hourly=15, detail_daily=60)
    quota.detail_hourly_used, quota.detail_daily_used = 14, 59
    assert gate.can_browse("detail", now).allowed is True
    quota.detail_hourly_used = 15
    assert gate.can_browse("detail", now).reason == "browse_detail_hourly_limit"
    quota.detail_hourly_used, quota.detail_daily_used = 0, 60
    assert gate.can_browse("detail", now).reason == "browse_detail_daily_limit"

    # 第 2 档（无风控日翻倍）：30/120——第 1 档会拦的量现在放行
    quota.limits = FakeLimits(detail_hourly=30, detail_daily=120)
    quota.detail_hourly_used, quota.detail_daily_used = 15, 60
    assert gate.can_browse("detail", now).allowed is True
    quota.detail_hourly_used = 30
    assert gate.can_browse("detail", now).reason == "browse_detail_hourly_limit"

    # 第 3 档（继续翻倍但不超过配置值 → 封顶 40/200）
    quota.limits = FakeLimits(detail_hourly=40, detail_daily=200)
    quota.detail_hourly_used = 30
    assert gate.can_browse("detail", now).allowed is True
    quota.detail_hourly_used = 40
    assert gate.can_browse("detail", now).reason == "browse_detail_hourly_limit"

    # 风控信号 → 回第 1 档（M5 的全停/等决策由引擎另行执行）
    quota.limits = FakeLimits(detail_hourly=15, detail_daily=60)
    quota.detail_hourly_used, quota.detail_daily_used = 15, 0
    assert gate.can_browse("detail", now).reason == "browse_detail_hourly_limit"
    quota.detail_hourly_used, quota.detail_daily_used = 0, 60
    assert gate.can_browse("detail", now).reason == "browse_detail_daily_limit"


def test_chrome_downgrade_tier_via_current_limits() -> None:
    """Chrome 主版本变化联动降档 [V9]：首次运行按第 1 档 15/60 执行；恢复后放行。"""
    quota = FakeQuota(limits=FakeLimits(detail_hourly=15, detail_daily=60))
    gate = make_gate(quota)  # 配置 browse_limits 默认 40/200
    now = cst(*_TUE, 10, 0)
    quota.detail_hourly_used = 15
    assert gate.can_browse("detail", now).reason == "browse_detail_hourly_limit"
    quota.detail_hourly_used = 14
    assert gate.can_browse("detail", now).allowed is True
    # 运行 30 分钟无风控信号 → S2b 恢复配置值 → 原计数不再触发
    quota.limits = FakeLimits(detail_hourly=40, detail_daily=200)
    quota.detail_hourly_used = 15
    assert gate.can_browse("detail", now).allowed is True


# ---- 操作级最小间隔独立生效（H2 / A-CORE-21）----


def test_min_interval_independent_of_random_throttle() -> None:
    """随机节流值 < 30s 时仍强制 30s：下限独立于 8-20s 随机延时之外 [H2]。"""
    quota = FakeQuota()  # detail_interval_s=30
    gate = make_gate(quota)
    t0 = cst(*_TUE, 10, 0)
    gate.record_action("detail", t0)
    # 拟人随机延时抽到 12s（8-20s 区间内）：12s 后仍不足 30s 下限 → 拒绝
    result = gate.can_browse("detail", t0 + timedelta(seconds=12))
    assert (result.allowed, result.reason) == (False, "min_interval")
    before_edge = gate.can_browse("detail", t0 + timedelta(seconds=29, microseconds=999999))
    assert before_edge.allowed is False
    # 恰好 30s（边界 ≥）→ 放行
    assert gate.can_browse("detail", t0 + timedelta(seconds=30)).allowed is True
    # 引擎侧有效等待 = max(随机值, 剩余下限)
    assert gate.min_interval_remaining("detail", t0 + timedelta(seconds=12)) == 18.0
    assert gate.min_interval_remaining("detail", t0 + timedelta(seconds=31)) == 0.0


def test_min_interval_per_action_kind_independent() -> None:
    """详情/搜索各自计时：search 的间隔不约束 detail。"""
    quota = FakeQuota()
    gate = make_gate(quota)
    t0 = cst(*_TUE, 10, 0)
    gate.record_action("search", t0)
    assert gate.can_browse("detail", t0 + timedelta(seconds=1)).allowed is True
    assert gate.can_browse("search", t0 + timedelta(seconds=1)).reason == "min_interval"
    assert gate.can_browse("search", t0 + timedelta(seconds=30)).allowed is True


def test_min_interval_value_from_quota_api_not_config() -> None:
    """下限值取 I1.min_interval_s（试运行校准后可调至 45），非配置默认 30。"""
    quota = FakeQuota(detail_interval_s=45.0, search_interval_s=45.0)
    gate = make_gate(quota)
    t0 = cst(*_TUE, 10, 0)
    gate.record_action("detail", t0)
    assert gate.can_browse("detail", t0 + timedelta(seconds=30)).reason == "min_interval"
    assert gate.can_browse("detail", t0 + timedelta(seconds=45)).allowed is True


def test_min_interval_with_injected_last_action_provider() -> None:
    """S4c 可注入持久化的最近操作时刻（重启不丢）；无记录不受间隔约束。"""

    def last_action_at(kind: ActionKind) -> datetime | None:
        return cst(*_TUE, 10, 0) if kind == "detail" else None

    t0 = cst(*_TUE, 10, 0)
    gate = Gate(
        DeliveryConfig(),
        FakeQuota().api(),
        lambda: t0,
        last_action_at=last_action_at,
    )
    assert gate.can_browse("detail", t0 + timedelta(seconds=10)).reason == "min_interval"
    assert gate.can_browse("detail", t0 + timedelta(seconds=30)).allowed is True
    assert gate.can_browse("search", t0 + timedelta(seconds=1)).allowed is True


# ---- GateResult 形状（I7 契约）----


def test_gate_result_contract() -> None:
    """GateResult：dataclass(allowed, reason)；布尔用法与 S2a DedupDecision 同约定。"""
    ok = GateResult(True, "ok")
    assert (ok.allowed, ok.reason) == (True, "ok")
    assert bool(ok) is True
    assert bool(GateResult(False, "circuit_breaker")) is False
