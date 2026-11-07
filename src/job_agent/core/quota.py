"""频率与浏览配额（设计文档 §5.5 I1 / §6 计数推算 [G1] / §7 浏览上限 [G3]；Stage2 S2b）。

口径（§6 权威约定）：
- 投递限额从 ``send_intents`` 推算：``state IN (pending, sent, unknown,
  skipped_dryrun)`` 且**不按 dry_run 过滤**——限额衡量账号暴露，与模式无关
  [G1]；``unknown`` 计入防止崩溃重启后突破上限；同日切换模式额度连续累计。
  ``failed`` 不计入（未发出 = 无暴露）。
- "hourly" = UTC **滑动 60 分钟窗口**（时间差，与自然日无关）；
- "daily" = **北京自然日**：起点 = :func:`timeutil.day_start_utc`（北京当天
  00:00 的 UTC 文本），与 ``created_at`` 字符串比较 [R6-4，禁止用 UTC 日期]。
- 浏览计数（详情/翻页）同理不分模式（浏览即暴露）[G3]；事件时间戳存
  ``run_state``（JSON 数组），每次记录时按窗口需要修剪。

模块级函数（:func:`used_quota` / :func:`browse_used` / :func:`current_limits`
/ :func:`min_interval_s`）为 I1 契约签名，经 :func:`configure` 装配后可用
（引擎启动时绑定 db 与配置快照）；测试与需要多实例的场景直接用
:class:`Quota`。

``current_limits`` 的折算来源（I1："调用方一律用本函数，禁止直读配置"）：
- 投递 ``hourly_limit``：配置值（放量期只折算日限，§13）；
- 投递 ``daily_limit``：I9 ``rampup.effective_daily_limit``（放量期 =
  ``ramp_up.daily_limit``，与配置日限取 min——只收紧不放大）；
- 浏览 ``detail_hourly`` / ``detail_daily``：I9 ``rampup.browse_tier``
  （优先级：Chrome 降档 [V9] > 试运行档位 [H2] > 配置值，S2d 实现）；
- 浏览 ``search_page_hourly``：配置值（翻页不参与档位折算）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from job_agent.config import Config
from job_agent.core import rampup
from job_agent.db import Database
from job_agent.timeutil import day_start_utc, utc_str

__all__ = [
    "COUNTED_STATES",
    "ActionKind",
    "BrowseWindow",
    "CountWindow",
    "Limits",
    "Quota",
    "browse_used",
    "configure",
    "current_limits",
    "min_interval_s",
    "record_browse",
    "used_quota",
]

CountWindow = Literal["hourly", "daily"]
BrowseWindow = Literal["detail_hourly", "detail_daily", "search_page_hourly"]
ActionKind = Literal["detail", "search"]

#: 计入投递限额的 intent 状态 [G1]（failed 不计：未发出 = 无账号暴露）
COUNTED_STATES: tuple[str, ...] = ("pending", "sent", "unknown", "skipped_dryrun")

# run_state 键：浏览事件时间戳（UTC 文本，JSON 数组；§6 run_state "已用限额"）
_DETAIL_KEY = "browse_events_detail"
_SEARCH_KEY = "browse_events_search"

_COUNT_SQL = (
    "SELECT COUNT(*) AS n FROM send_intents WHERE state IN (?, ?, ?, ?) AND created_at >= ?"
)


@dataclass(frozen=True)
class Limits:
    """当前生效上限（I1 ``current_limits`` 返回值；结构 = S2c LimitsView）。

    投递 ``hourly_limit`` / ``daily_limit``（日限已含放量期折算）、浏览
    ``detail_hourly`` / ``detail_daily`` / ``search_page_hourly``（已含试运行
    放量档位 [H2] 与 Chrome 降档 [V9] 折算）。
    """

    hourly_limit: int
    daily_limit: int
    detail_hourly: int
    detail_daily: int
    search_page_hourly: int


class Quota:
    """配额计算（持有 db 与配置快照；纯读库计算，不做任何 IO 副作用）。"""

    def __init__(self, db: Database, config: Config) -> None:
        self._db = db
        self._config = config

    # ---- I1：投递限额 ----

    def used(self, kind: CountWindow, now: datetime) -> int:
        """窗口内已占用的投递额度（send_intents 推算，不分模式 [G1]）。

        :param kind: ``"hourly"`` 滑动 60 分钟；``"daily"`` 北京自然日 [R6-4]。
        """
        if kind == "daily":
            start = day_start_utc(now)
        elif kind == "hourly":
            start = utc_str(now - timedelta(minutes=60))
        else:
            raise ValueError(f"未知窗口: {kind!r}（仅接受 hourly / daily）")
        row = self._db.query_one(_COUNT_SQL, (*COUNTED_STATES, start))
        return int(row["n"]) if row is not None else 0

    # ---- I1：浏览计数 ----

    def record_browse(self, kind: ActionKind, now: datetime) -> None:
        """记录一次浏览动作（详情打开 / 搜索翻页），供 :meth:`browse_used` 推算。

        事件以 UTC 时间戳文本存 ``run_state``（事务内读改写）；修剪保留
        ``min(当日起点, now-60min)`` 之后的事件——恰好覆盖日/小时两个窗口。
        """
        key = _event_key(kind)
        keep_from = min(day_start_utc(now), utc_str(now - timedelta(minutes=60)))
        with self._db.transaction() as conn:
            row = conn.execute("SELECT value_json FROM run_state WHERE key = ?", (key,)).fetchone()
            events: list[str] = json.loads(row["value_json"]) if row is not None else []
            events.append(utc_str(now))
            events = [e for e in events if e >= keep_from]
            conn.execute(
                "INSERT INTO run_state (key, value_json) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (key, json.dumps(events)),
            )

    def browse_used(self, kind: BrowseWindow, now: datetime) -> int:
        """窗口内浏览动作计数（详情小时/详情日/翻页小时；不分模式 [G3]）。"""
        if kind == "detail_daily":
            start, events = day_start_utc(now), self._load_events("detail")
        elif kind == "detail_hourly":
            start, events = utc_str(now - timedelta(minutes=60)), self._load_events("detail")
        elif kind == "search_page_hourly":
            start, events = utc_str(now - timedelta(minutes=60)), self._load_events("search")
        else:
            raise ValueError(f"未知浏览窗口: {kind!r}")
        return sum(1 for e in events if e >= start)

    # ---- I1：操作级最小间隔 ----

    def min_interval_s(self, action: ActionKind) -> float:
        """操作级最小间隔秒数 [H2]（配置校验已保证 ≥1 的硬下限 [R6-1]）。"""
        if action == "detail":
            return float(self._config.delivery.min_interval_s.detail)
        if action == "search":
            return float(self._config.delivery.min_interval_s.search)
        raise ValueError(f"未知操作: {action!r}（仅接受 detail / search）")

    # ---- I1：当前生效上限 ----

    def current_limits(self, now: datetime) -> Limits:
        """折算后的当前生效上限（放量档位/Chrome 降档/放量期日限，见模块 docstring）。

        折算逻辑本体在 S2d（I9 ``browse_tier`` / ``effective_daily_limit``），
        本方法只做组合——单一职责，避免两处实现折算规则。
        """
        tier = rampup.browse_tier(self._db, self._config, now)
        return Limits(
            hourly_limit=self._config.delivery.hourly_limit,
            daily_limit=rampup.effective_daily_limit(self._db, self._config, now),
            detail_hourly=tier.detail_hourly,
            detail_daily=tier.detail_daily,
            search_page_hourly=self._config.delivery.browse_limits.search_page_hourly,
        )

    # ---- 内部 ----

    def _load_events(self, kind: ActionKind) -> list[str]:
        row = self._db.query_one(
            "SELECT value_json FROM run_state WHERE key = ?", (_event_key(kind),)
        )
        if row is None:
            return []
        loaded = json.loads(row["value_json"])
        return [str(e) for e in loaded]


def _event_key(kind: ActionKind) -> str:
    return _DETAIL_KEY if kind == "detail" else _SEARCH_KEY


# ---- 模块级 I1 契约入口（装配后可用）----

_default: Quota | None = None


def configure(db: Database, config: Config) -> None:
    """装配模块级默认实例（引擎启动时调用一次；重复调用=替换快照）。"""
    global _default
    _default = Quota(db, config)


def _require_default() -> Quota:
    if _default is None:
        raise RuntimeError(
            "job_agent.core.quota 尚未装配：先调用 configure(db, config)（或直接使用 Quota 实例）"
        )
    return _default


def used_quota(kind: CountWindow, now: datetime) -> int:
    """I1 契约签名：窗口内已用投递额度（详见 :meth:`Quota.used`）。"""
    return _require_default().used(kind, now)


def browse_used(kind: BrowseWindow, now: datetime) -> int:
    """I1 契约签名：窗口内浏览计数（详见 :meth:`Quota.browse_used`）。"""
    return _require_default().browse_used(kind, now)


def min_interval_s(action: ActionKind) -> float:
    """I1 契约签名：操作级最小间隔（详见 :meth:`Quota.min_interval_s`）。"""
    return _require_default().min_interval_s(action)


def current_limits(now: datetime) -> Limits:
    """I1 契约签名：当前生效上限（详见 :meth:`Quota.current_limits`）。"""
    return _require_default().current_limits(now)


def record_browse(kind: ActionKind, now: datetime) -> None:
    """模块级便捷入口：记录一次浏览动作（详见 :meth:`Quota.record_browse`）。"""
    _require_default().record_browse(kind, now)
