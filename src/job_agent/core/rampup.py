"""放量与试运行状态机（§5.5 I9 提供方 rampup 部分；状态机语义见 §13）。

覆盖（Stage2 S2d 任务书第 2 条）：
- **首次实投逐步放量 [N1/G10]**：``dry_run`` 由用户关闭 → ``remaining_days``
  = ``ramp_up.enabled_days``（默认 3）。每达成一个**有效运行日**（当日发生
  投递动作，即当日存在 ``dry_run=0`` 的 greeting send_intent；被限额/浏览
  上限拦住而全天无投递的当日**不计** [Q20]）递减；期间日限 =
  ``ramp_up.daily_limit``、低风险自动回复关、FR3.7/3.8 关（期间标志 =
  :func:`is_rampup_active`）；期满自动恢复配置值 + RAMPUP_DONE 事件；
  用户可随时提前结束 / 延长；
- **试运行浏览放量档位 [H2]**：第 1 个试运行日详情上限 = ``first_day``
  （默认 15/时·60/天），其后每个无风控信号的试运行日翻倍、逐项封顶配置
  值；任何风控信号 → 回第 1 档；
- **Chrome 版本联动降档 [V9]**：``run_state`` 的 ``chrome_downgrade`` 键
  由浏览器层（S4b）在检测到主版本变化时写入，本模块折算档位：激活期用
  第 1 档值，30 分钟后自动恢复配置值（V9 的自动恢复与 M5 无关——M5 针对
  风控暂停）；
- **M5 风控处置**：放量/试运行期间任何风控信号（CAPTCHA/RISK_PAGE/
  EVICTED/SECURITY_CHECK）→ **暂停全部任务 + 高级告警 + 等用户决策**；
  无 fallback_semi_auto、无自动恢复——恢复唯一路径是用户经控制台触发
  :func:`clear_risk_pause`。本模块不提供任何自动清除风险暂停的代码路径。

I9 签名适配说明：契约函数为模块级无状态签名，本实现按依赖注入前置
``(db, config)`` 参数（纯逻辑层无全局态、可离线测试），函数名与语义保持
I9 不变；S4c/S4d/S5c 装配时可用 ``functools.partial`` 绑定。

事件经 ``core/events`` 占位协议落 ``notification_events``（S3d 完成后由
MainAgent 对接替换，见该模块 docstring）。

run_state 键（S4/S5 可直读，JSON 值）：
- ``ramp_up``：``{"state": inactive|active|done, "enabled_days": int,
  "effective_days": ["YYYY-MM-DD", ...], "started_at": ts}``
- ``risk_pause``：``{"signal": ..., "at": ts, "awaiting_user": bool,
  "m5": bool, "resolved_by": str?}``
- ``browse_rampup``：``{"tier": int, "current_day": "YYYY-MM-DD"|null,
  "current_day_risk": bool}``
- ``chrome_downgrade``（S4b 写入）：``{"active": bool, "since": ts,
  "chrome_major": int?}``
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from job_agent.config import Config
from job_agent.db import Database
from job_agent.models import RiskSignal
from job_agent.timeutil import as_utc, beijing_date_str, day_start_utc, utc_str

from . import dryrun, events, runstate

__all__ = [
    "KEY_BROWSE_TIER",
    "KEY_CHROME_DOWNGRADE",
    "KEY_RAMPUP",
    "KEY_RISK_PAUSE",
    "BrowseTier",
    "browse_tier",
    "clear_risk_pause",
    "effective_daily_limit",
    "end_rampup_early",
    "extend_rampup",
    "handle_dry_run_disabled",
    "handle_risk_signal",
    "is_rampup_active",
    "is_risk_paused",
    "rampup_days_left",
    "rampup_state",
    "record_rampup_day_if_delivered",
    "risk_pause_info",
    "risk_pause_reason",
]

# ---- run_state 键 ----
KEY_RAMPUP = "ramp_up"
KEY_RISK_PAUSE = "risk_pause"
KEY_BROWSE_TIER = "browse_rampup"
KEY_CHROME_DOWNGRADE = "chrome_downgrade"

# 风控类信号（§5.2 RiskDetectedError 一族）→ I5 高级事件 [G2/H1/M5]
_RISK_EVENT: dict[RiskSignal, str] = {
    RiskSignal.CAPTCHA: "RISK_PAUSE",
    RiskSignal.RISK_PAGE: "RISK_PAUSE",
    RiskSignal.EVICTED: "PAGE_EVICTED",
    RiskSignal.SECURITY_CHECK: "SECURITY_CHECK",
}

# V9：Chrome 降档自动恢复时长（运行 30 分钟无风控信号恢复配置值）
_CHROME_DOWNGRADE_MIN = timedelta(minutes=30)

TierSource = Literal["dryrun_rampup", "config", "chrome_downgrade"]


@dataclass(frozen=True)
class BrowseTier:
    """当前浏览档位（I9 browse_tier 返回值）。

    ``tier``：1 起计的试运行档位；0 = 档位机制未生效（配置值直读或
    browse_rampup.enabled=false）。``source`` 说明取值来路，供日志与
    "为何停"面板。
    """

    tier: int
    detail_hourly: int
    detail_daily: int
    source: TierSource


# ---- 放量期状态机 [N1/G10/§13] ----


def handle_dry_run_disabled(db: Database, config: Config) -> None:
    """dry_run 关闭 → 启动放量期（§13；由 dryrun.set_dry_run 调用，幂等）。

    仅"从未开始"（inactive）会激活：放量期一旦结束（done）不会因再次
    关闭试运行而重开——重开需走控制台明确操作（extend_rampup 的
    done→active 路径）。也涵盖"配置文件预置 dry_run=false、从未经
    set_dry_run 切换"的路径（_ensure_rampup 的惰性激活）。
    """
    _ensure_rampup(db, config, datetime.now(UTC))


def _activate(config: Config, now: datetime) -> dict[str, Any]:
    return {
        "state": "active",
        "enabled_days": config.delivery.ramp_up.enabled_days,
        "effective_days": [],
        "started_at": utc_str(now),
    }


def _ensure_rampup(db: Database, config: Config, now: datetime) -> dict[str, Any]:
    """读取/初始化放量状态；inactive 且试运行已关 → 惰性激活（幂等）。"""
    raw = runstate.get_state(db, KEY_RAMPUP)
    state: dict[str, Any] | None = raw if isinstance(raw, dict) else None
    if state is not None and state.get("state") in ("active", "done"):
        return state
    if state is not None and state.get("state") == "inactive":
        if dryrun.is_dry_run(db, config):
            return state
        state = _activate(config, now)
        runstate.set_state(db, KEY_RAMPUP, state)
        return state
    # 键缺失（或结构不符）：初始化（试运行中 → inactive；已关 → 直接激活）
    if dryrun.is_dry_run(db, config):
        state = {
            "state": "inactive",
            "enabled_days": config.delivery.ramp_up.enabled_days,
            "effective_days": [],
            "started_at": None,
        }
    else:
        state = _activate(config, now)
    runstate.set_state(db, KEY_RAMPUP, state)
    return state


def rampup_state(db: Database, config: Config) -> dict[str, Any]:
    """放量状态快照（控制台/装配只读访问）。"""
    return dict(_ensure_rampup(db, config, datetime.now(UTC)))


def is_rampup_active(db: Database, config: Config) -> bool:
    """放量期间标志 [§13]：True 时低风险自动回复关、FR3.7/3.8 关。"""
    return bool(_ensure_rampup(db, config, datetime.now(UTC))["state"] == "active")


def rampup_days_left(db: Database, config: Config) -> int:
    """剩余有效运行日 [I9]；非 active（未开始/已期满）返回 0。"""
    state = _ensure_rampup(db, config, datetime.now(UTC))
    if state["state"] != "active":
        return 0
    return max(0, int(state["enabled_days"]) - len(state["effective_days"]))


def effective_daily_limit(db: Database, config: Config, now: datetime) -> int:
    """当前生效的每日投递上限 [I9]：放量期 = ramp_up.daily_limit，否则配置值。

    与配置日限取 min——放量参数只应收紧、不得放大配置上限。
    （now 为 I9 契约参数；当前口径仅由放量状态决定，与时刻无关。）
    """
    if is_rampup_active(db, config):
        return min(config.delivery.ramp_up.daily_limit, config.delivery.daily_limit)
    return config.delivery.daily_limit


def record_rampup_day_if_delivered(
    db: Database, config: Config, now: datetime
) -> events.EventRecord | None:
    """有效运行日判定与递减 [I9/G10]（幂等，S4c 投递成功路径调用）。

    当日存在 ``dry_run=0`` 的 greeting send_intent（投递动作，含 pending/
    unknown/failed 的"已尝试"口径）且尚未记录 → 记一日；有效日数达
    ``enabled_days`` 且 ``auto_recover=true`` → 期满（done）并 emit
    RAMPUP_DONE（低风险自动回复/FR3.7/3.8/日限随之恢复）。auto_recover=
    false 时期满保持 active（日限维持放量值），等用户 end_rampup_early。

    被限额/浏览上限拦截的当日不会有投递动作 → 天然不计 [Q20]；
    北京自然日口径 [R6-4]：窗口起点 = day_start_utc(now)。
    """
    state = _ensure_rampup(db, config, now)
    if state["state"] != "active":
        return None
    today = beijing_date_str(now)
    effective = list(state["effective_days"])
    if today in effective:
        return None
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM send_intents"
        " WHERE kind = 'greeting' AND dry_run = 0 AND created_at >= ?",
        (day_start_utc(now),),
    )
    if row is None or int(row["n"]) == 0:
        return None  # 当日无投递动作 → 无效运行日，不计 [G10/Q20]
    effective.append(today)
    enabled = int(state["enabled_days"])
    reached_end = len(effective) >= enabled and config.delivery.ramp_up.auto_recover
    new_state: dict[str, Any] = {
        "state": "done" if reached_end else "active",
        "enabled_days": enabled,
        "effective_days": effective,
        "started_at": state["started_at"],
    }
    runstate.set_state(db, KEY_RAMPUP, new_state)
    if reached_end:
        return events.emit(
            db,
            "RAMPUP_DONE",
            {
                "effective_days": effective,
                "enabled_days": enabled,
                "restored_daily_limit": config.delivery.daily_limit,
                "source": "auto_recover",
            },
        )
    return None


def end_rampup_early(db: Database, config: Config) -> events.EventRecord | None:
    """手动提前结束放量期 [FR3.9]（控制台触发）；inactive/done 时幂等返回 None。"""
    state = _ensure_rampup(db, config, datetime.now(UTC))
    if state["state"] != "active":
        return None
    new_state = {**state, "state": "done"}
    runstate.set_state(db, KEY_RAMPUP, new_state)
    return events.emit(
        db,
        "RAMPUP_DONE",
        {
            "effective_days": state["effective_days"],
            "enabled_days": state["enabled_days"],
            "restored_daily_limit": config.delivery.daily_limit,
            "source": "manual_early",
        },
    )


def extend_rampup(db: Database, config: Config, extra_days: int) -> None:
    """手动延长放量期 [FR3.9]：enabled_days += extra（≥1）。

    已期满（done）时延长会把状态拉回 active（剩余 = 新上限 − 已计日数）；
    尚未开始（inactive）时拒绝——先经 set_dry_run(False) 进入放量期。
    """
    if extra_days < 1:
        raise ValueError("延长天数必须 ≥ 1")
    state = _ensure_rampup(db, config, datetime.now(UTC))
    if state["state"] == "inactive":
        raise ValueError("放量期尚未开始（试运行关闭时自动启动），无需延长")
    new_state = {
        **state,
        "state": "active",
        "enabled_days": int(state["enabled_days"]) + extra_days,
    }
    runstate.set_state(db, KEY_RAMPUP, new_state)


# ---- 试运行浏览档位 [H2/V9] ----


def browse_tier(db: Database, config: Config, now: datetime) -> BrowseTier:
    """当前浏览档位 [I9]（详情 15/60 起步 → 无风控日翻倍 → 配置值封顶）。

    优先级：Chrome 降档覆盖 [V9] > 试运行档位 [H2] > 配置值。
    档位推进为**按北京日惰性结算**：跨日首次调用时，若前一试运行日无
    风控信号则档位 +1（翻倍）；非试运行日（正式模式）不推进也不重置，
    直接返回配置值。
    """
    limits = config.delivery.browse_limits
    first = config.delivery.browse_rampup.first_day

    # [V9] Chrome 主版本变化 → 第 1 档；运行 30 分钟（无风控信号）自动恢复配置值
    chrome = runstate.get_state(db, KEY_CHROME_DOWNGRADE)
    if isinstance(chrome, dict) and chrome.get("active"):
        since = _parse_ts(chrome.get("since"))
        expired = since is not None and as_utc(now) >= since + _CHROME_DOWNGRADE_MIN
        if expired:
            chrome = {**chrome, "active": False, "expired_at": utc_str(now)}
            runstate.set_state(db, KEY_CHROME_DOWNGRADE, chrome)
        else:
            return BrowseTier(1, first.detail_hourly, first.detail_daily, "chrome_downgrade")

    in_dry_run = config.delivery.browse_rampup.enabled and dryrun.is_dry_run(db, config)
    if not in_dry_run:
        return BrowseTier(0, limits.detail_hourly, limits.detail_daily, "config")

    state = runstate.get_state(db, KEY_BROWSE_TIER)
    if not isinstance(state, dict):
        state = {"tier": 1, "current_day": None, "current_day_risk": False}
    today = beijing_date_str(now)
    if state.get("current_day") != today:
        tier = int(state.get("tier", 1))
        if state.get("current_day") is not None and not state.get("current_day_risk", False):
            tier += 1  # 前一个试运行日无风控信号 → 档位翻倍 [H2]
        state = {"tier": tier, "current_day": today, "current_day_risk": False}
        runstate.set_state(db, KEY_BROWSE_TIER, state)
    tier = int(state["tier"])
    hourly = min(first.detail_hourly * 2 ** (tier - 1), limits.detail_hourly)
    daily = min(first.detail_daily * 2 ** (tier - 1), limits.detail_daily)
    return BrowseTier(tier, hourly, daily, "dryrun_rampup")


# ---- M5 风控处置 [M5/G2/H1] ----


def handle_risk_signal(
    db: Database, config: Config, signal: RiskSignal | str, now: datetime
) -> events.EventRecord:
    """风控信号统一处置：暂停全部任务 + 高级告警 + 等用户决策（M5）。

    - 信号 ∈ {CAPTCHA, RISK_PAGE, EVICTED, SECURITY_CHECK}（风控类，
      §5.2 RiskDetectedError 一族）；LOGIN_EXPIRED/ADAPTER_BROKEN/
      PLATFORM_LIMIT/NONE 走各自路径（§5.2 错误表），传入即拒绝；
    - 置 ``risk_pause``（awaiting_user=true）——**不存在任何自动清除路径**
      （M5：无自动恢复、无 fallback_semi_auto），恢复唯一入口 =
      :func:`clear_risk_pause`（用户经控制台决策后由编排器调用）；
    - 浏览档位回第 1 档 [H2]，当日标记为风控日（不参与翻倍结算）；
    - 放量/试运行期间 payload 带 ``m5=true``。引擎侧"暂停全部任务"由
      S4d 编排器依据 ``is_risk_paused`` 执行（S2c Gate 熔断标志同源）。
    """
    sig = _coerce_signal(signal)
    if sig not in _RISK_EVENT:
        raise ValueError(f"非风控类信号 {sig.name!r} 不走 M5 处置（见 §5.2 错误表各自路径）")
    m5 = is_rampup_active(db, config) or dryrun.is_dry_run(db, config)
    runstate.set_state(
        db,
        KEY_RISK_PAUSE,
        {"signal": sig.name, "at": utc_str(now), "awaiting_user": True, "m5": m5},
    )
    today = beijing_date_str(now)
    runstate.set_state(
        db, KEY_BROWSE_TIER, {"tier": 1, "current_day": today, "current_day_risk": True}
    )
    return events.emit(
        db,
        _RISK_EVENT[sig],
        {
            "signal": sig.name,
            "m5": m5,
            "action": "pause_all_awaiting_user",
            "browse_tier_reset": True,
            "message": (
                "风控信号：暂停全部任务并等待用户决策（M5：无自动恢复、无半自动回退）"
                if m5
                else "风控信号：暂停全部任务，等待用户处理"
            ),
        },
    )


def is_risk_paused(db: Database) -> bool:
    """风控熔断是否生效 [I7 Gate 熔断标志数据源 / S4d 编排暂停依据]。"""
    info = risk_pause_info(db)
    return bool(info and info.get("awaiting_user"))


def risk_pause_info(db: Database) -> dict[str, Any] | None:
    """风控暂停详情（signal/at/m5/resolved_by），无记录返回 None。"""
    info = runstate.get_state(db, KEY_RISK_PAUSE)
    return info if isinstance(info, dict) else None


def risk_pause_reason(db: Database) -> str | None:
    """ "为何停"面板用的可读原因；未暂停返回 None。"""
    info = risk_pause_info(db)
    if not info or not info.get("awaiting_user"):
        return None
    suffix = "（M5：等用户决策，无自动恢复）" if info.get("m5") else "（等用户处理）"
    return f"风控信号 {info.get('signal')} @ {info.get('at')}{suffix}"


def clear_risk_pause(db: Database, decided_by: str = "user") -> None:
    """清除风控暂停（**唯一恢复路径**：用户决策后由编排器/控制台调用）。

    幂等：未暂停时无操作。本函数之外没有任何代码会把 awaiting_user 置
    False——M5"无自动恢复"由此保证（可 grep 复核）。
    """
    info = runstate.get_state(db, KEY_RISK_PAUSE)
    if not isinstance(info, dict) or not info.get("awaiting_user"):
        return
    resolved = {**info, "awaiting_user": False, "resolved_by": decided_by}
    runstate.set_state(db, KEY_RISK_PAUSE, resolved)


# ---- 内部 ----


def _coerce_signal(signal: RiskSignal | str) -> RiskSignal:
    """接受 RiskSignal 枚举或其 name/value 字符串，未知值抛 ValueError。"""
    if isinstance(signal, RiskSignal):
        return signal
    if isinstance(signal, str):
        try:
            return RiskSignal(signal)
        except ValueError:
            pass
        try:
            return RiskSignal[signal.upper()]
        except KeyError:
            pass
    raise ValueError(f"未知风控信号：{signal!r}")


def _parse_ts(value: Any) -> datetime | None:
    """ISO-8601 文本 → UTC datetime（非法/缺失返回 None，调用方保守处理）。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return as_utc(parsed)
