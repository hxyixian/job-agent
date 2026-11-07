"""通知中心（§11 路由管线的完整实现；I5 ``emit`` 的提供方）。

路由管线（每一跳的落点）::

    emit(event, payload, level_override)
      ① 全量落库 notification_events（永远——通知中心页面数据源，实时）
      ② 级别路由（level = 权威表 [I5] 或 level_override）
      ③ 免打扰 [B11]：
         - 窗口外 → ④；
         - 窗口内 + high + dnd.high_bypass → ④（"high 不受限，可配"）；
         - 窗口内 +（中/低，或 high 且 high_bypass=false）→ 延后：
           notify_dedup 按 event_type 计数（window_start = 当前免打扰窗口
           起点；跨窗口自动重新计数），delivered_json 记 {"dnd": "deferred"}；
      ④ 渠道投递：逐 Notifier 做 applies(level) 级别过滤 → send；
         未配置记 skip:not_configured，异常记 error:<异常名>（投递失败
         不影响事件落库，也不向上抛）；各渠道状态汇总写回 delivered_json。

免打扰合并推送：:meth:`NotificationCenter.flush_deferred` 在**窗口结束后**
由调度方调用（引擎每轮循环 tick 一次即可）：对 notify_dedup 中每个
event_type 生成**一条**合并消息（payload 带 count/window_start）按级别
过滤投递并清空计数；窗口未结束调用为无操作。软件在窗口结束时刻未运行时，
下次调用即补推合并结果（DAILY_DIGEST 的"摘要不补发"[B11] 指每日摘要本身
不补推，与本合并机制无关）。

线程安全 [I5]：emit/flush 在实例锁内串行；DB 访问另受 Database 自身
RLock 保护（双保险）。

与 core/events（S2d 占位协议）的对接：本模块维护 ``db 实例 → center``
注册表（:func:`register_center`/:func:`center_for`，WeakKeyDictionary）。
``NotificationCenter.__init__`` 自动注册；core.events.emit 查到注册中心时
转发到完整管线，查不到时退化为"仅落库"（占位协议行为，兼容无通知装配
的调用方与测试）。装配方（S4d/S5d）建库后创建一个 NotificationCenter 即
完成全进程接线。

待发箱超时合并 [s6/§8.3]：:func:`scan_outbox_timeout`——所有滞留超过
``messaging.outbox_timeout_min`` 的**未处理**条目（state ∈ pending/stale，
approved=已确认待发、skipped/expired=已处理不提醒）合并为**一条**
OUTBOX_TIMEOUT 高级告警，每条目只提醒一次（reminded_at 置位后不再入选）；
时段外到期的条目在下一轮扫描自然并入 [B10]。由 S4d 消息轮询循环每轮调用。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import weakref
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from typing import Any

from job_agent.config import Config, DndConfig
from job_agent.db import Database
from job_agent.notify.base import (
    EventRecord,
    EventType,
    Level,
    Notifier,
    event_name,
    level_of,
)
from job_agent.timeutil import CST, as_utc, utc_str, utcnow_str

__all__ = [
    "NotificationCenter",
    "center_for",
    "default_notifiers",
    "dnd_window_bounds",
    "list_events",
    "register_center",
    "scan_outbox_timeout",
    "store_event",
    "unregister_center",
]

logger = logging.getLogger(__name__)

# 渠道状态字面量（delivered_json 值；控制台通知中心按此展示）
_STATUS_OK = "ok"
_STATUS_SKIP_LEVEL = "skip:level"
_STATUS_SKIP_UNCONFIGURED = "skip:not_configured"


# ---- ① 全量落库（占位协议语义不变，单一实现点） ----


def store_event(
    db: Database,
    event_type: EventType | str,
    payload: dict[str, Any],
    level_override: Level | None = None,
    *,
    now: datetime | None = None,
) -> EventRecord:
    """写入 ``notification_events`` 并返回事件元组（无路由）。

    - 未知 event_type 抛 ``ValueError``（即使带 level_override 也先校验——
      与 S2d 占位协议一致，拼写错误尽早暴露）；
    - payload 序列化 ``ensure_ascii=False, sort_keys=True``（与占位协议
      逐字节同构，既有断言不受影响）；
    - ``delivered_json`` 置 NULL：投递状态由 :meth:`NotificationCenter.emit`
      在路由后回填。
    """
    name = event_name(event_type)  # 校验 + 统一为字符串
    level = level_override if level_override is not None else level_of(name)
    created_at = utcnow_str() if now is None else utc_str(as_utc(now))
    row_id = db.execute_returning_id(
        "INSERT INTO notification_events (event_type, level, payload_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (name, level, json.dumps(payload, ensure_ascii=False, sort_keys=True), created_at),
    )
    return EventRecord(
        event_type=name, level=level, payload=payload, id=row_id, created_at=created_at
    )


# ---- ③ 免打扰窗口几何（北京时间表述，支持跨午夜 [B11]） ----


def _secs_of_day(hhmm: str) -> int:
    """HH:MM → 当日秒数（格式已由配置 HHMM 校验）。"""
    return int(hhmm[0:2]) * 3600 + int(hhmm[3:5]) * 60


def dnd_window_bounds(dnd: DndConfig, now: datetime) -> tuple[datetime, datetime] | None:
    """``now`` 所在免打扰窗口的 (start, end)（UTC datetime）；窗口外返回 None。

    - ``enabled=false`` 或 ``start == end``（零长度）→ 恒 None；
    - ``start < end``：同日窗口 [start, end)；
    - ``start > end``：跨午夜窗口（如 23:00-07:30 = 今天 start ~ 次日 end；
      凌晨段回溯到昨天 start）；
    - now 以北京时间判定（naive 视为 UTC，timeutil 契约）。
    """
    if not dnd.enabled:
        return None
    b = as_utc(now).astimezone(CST)
    cur = b.hour * 3600 + b.minute * 60 + b.second
    start_s, end_s = _secs_of_day(dnd.start), _secs_of_day(dnd.end)
    if start_s == end_s:
        return None

    def at(day_offset: int, secs: int) -> datetime:
        day = (b + timedelta(days=day_offset)).date()
        moment = datetime.combine(day, time(secs // 3600, secs % 3600 // 60, secs % 60))
        return moment.replace(tzinfo=CST).astimezone(UTC)

    if start_s < end_s:
        if start_s <= cur < end_s:
            return at(0, start_s), at(0, end_s)
        return None
    # 跨午夜
    if cur >= start_s:  # 晚间段：今天 start ~ 明天 end
        return at(0, start_s), at(1, end_s)
    if cur < end_s:  # 凌晨段：昨天 start ~ 今天 end
        return at(-1, start_s), at(0, end_s)
    return None


# ---- 渠道默认装配 ----


def default_notifiers(config: Config) -> list[Notifier]:
    """按配置装配默认渠道（§4 notify 节）：飞书（levels 过滤）+ 邮件（仅 high）。

    渠道参数未配置时仍装配实例——由 ``configured`` 标志让管线记
    skip:not_configured（控制台可见"未配置"而非静默）。
    """
    from job_agent.notify.email import EmailNotifier
    from job_agent.notify.feishu import FeishuNotifier

    return [FeishuNotifier(config.notify.feishu), EmailNotifier(config.notify.email)]


# ---- db → center 注册表（core/events 转发依据） ----

_CentersMap = weakref.WeakKeyDictionary[Database, "NotificationCenter"]
_CENTERS: _CentersMap = weakref.WeakKeyDictionary()


def register_center(db: Database, center: NotificationCenter) -> None:
    """把 center 绑定到 db 实例（同一 db 重复注册以后注册者为准）。"""
    _CENTERS[db] = center


def unregister_center(db: Database) -> None:
    """解除 db 实例的 center 绑定（不存在时无操作）。"""
    _CENTERS.pop(db, None)


def center_for(db: Database) -> NotificationCenter | None:
    """查询绑定到该 db 实例的通知中心（无绑定返回 None）。"""
    return _CENTERS.get(db)


class NotificationCenter:
    """事件 → 落库 → 路由 → 免打扰/合并 → 渠道投递的管线中枢（§11）。

    构造即向 ``db`` 注册（core.events.emit 随即走完整管线）；
    :meth:`close` 解除注册并释放渠道资源。
    """

    def __init__(
        self,
        db: Database,
        config: Config,
        notifiers: Sequence[Notifier] | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._notifiers: list[Notifier] = (
            list(notifiers) if notifiers is not None else default_notifiers(config)
        )
        self._lock = threading.RLock()
        register_center(db, self)

    @property
    def notifiers(self) -> list[Notifier]:
        return list(self._notifiers)

    def close(self) -> None:
        """解除注册并关闭各渠道（幂等）。"""
        unregister_center(self._db)
        for notifier in self._notifiers:
            try:
                notifier.close()
            except Exception:  # noqa: BLE001（关闭钩子失败仅记日志）
                logger.warning("通知渠道 %s 关闭失败", notifier.name, exc_info=True)

    # ---- I5 emit：落库 + 路由（返回事件元组；None 返回值契约的超集） ----

    def emit(
        self,
        event_type: EventType | str,
        payload: dict[str, Any],
        level_override: Level | None = None,
        *,
        now: datetime | None = None,
    ) -> EventRecord:
        """落库 → 路由 → 投递；任何渠道失败都不影响落库与返回值。"""
        at = datetime.now(UTC) if now is None else as_utc(now)
        with self._lock:
            record = store_event(self._db, event_type, payload, level_override, now=at)
            status = self._route(record, at)
            assert record.id is not None  # store_event 必然返回行 id
            self._db.execute(
                "UPDATE notification_events SET delivered_json = ? WHERE id = ?",
                (json.dumps(status, ensure_ascii=False, sort_keys=True), record.id),
            )
            return record

    # ---- 路由核心 ----

    def _route(self, record: EventRecord, at: datetime) -> dict[str, Any]:
        """③ 免打扰判定 → ④ 渠道投递；返回 delivered_json 内容。"""
        dnd = self._config.notify.dnd
        window = dnd_window_bounds(dnd, at)
        if window is not None and not (record.level == "high" and dnd.high_bypass):
            count = self._defer(record.event_type, window)
            return {"dnd": "deferred", "count": count}
        return self._deliver(record)

    def _deliver(self, record: EventRecord) -> dict[str, Any]:
        """④ 级别过滤 → 各渠道 send；汇总每渠道状态（不做免打扰判定）。"""
        status: dict[str, Any] = {}
        for notifier in self._notifiers:
            if not notifier.configured:
                status[notifier.name] = _STATUS_SKIP_UNCONFIGURED
                continue
            if not notifier.applies(record.level):
                status[notifier.name] = _STATUS_SKIP_LEVEL
                continue
            try:
                notifier.send(record)
                status[notifier.name] = _STATUS_OK
            except Exception as exc:  # noqa: BLE001（渠道失败不阻断管线）
                status[notifier.name] = f"error:{type(exc).__name__}"
                logger.warning(
                    "通知渠道 %s 投递 %s 失败：%s",
                    notifier.name,
                    record.event_type,
                    exc,
                )
        return status

    def _defer(self, event_type: str, window: tuple[datetime, datetime]) -> int:
        """免打扰延后 [B11]：notify_dedup 按 event_type 计数，返回最新计数。

        window_start = 当前窗口起点的 UTC 文本：同一窗口内累加，
        新窗口（window_start 不同）自动从 1 重新计数。
        """
        window_start = utc_str(window[0])
        row = self._db.query_one(
            "SELECT window_start, count FROM notify_dedup WHERE event_type = ?", (event_type,)
        )
        same_window = row is not None and str(row["window_start"]) == window_start
        count = int(row["count"]) + 1 if same_window and row is not None else 1
        self._db.execute(
            "INSERT INTO notify_dedup (event_type, window_start, count) VALUES (?, ?, ?) "
            "ON CONFLICT(event_type) DO UPDATE SET count = excluded.count,"
            " window_start = excluded.window_start",
            (event_type, window_start, count),
        )
        return count

    def is_dnd_active(self, now: datetime | None = None) -> bool:
        """当前时刻是否处于免打扰窗口内（诊断/测试辅助）。"""
        at = datetime.now(UTC) if now is None else as_utc(now)
        return dnd_window_bounds(self._config.notify.dnd, at) is not None

    # ---- 免打扰合并推送（窗口结束后调用） ----

    def flush_deferred(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """把免打扰期间延后的中低级（含 bypass 关闭的 high）事件合并推送 [B11]。

        - 仍在免打扰窗口内 → 无操作（等窗口结束）；
        - 每个 event_type 一条合并消息（payload: merged/count/window_start），
          按**权威表级别**做渠道过滤后投递，随后清空该 dedup 行；
        - 返回本次实际推送的清单（event_type/level/count/delivered），
          供引擎日志与测试断言；无待推条目返回 []。
        """
        at = datetime.now(UTC) if now is None else as_utc(now)
        with self._lock:
            if dnd_window_bounds(self._config.notify.dnd, at) is not None:
                return []
            rows = self._db.query(
                "SELECT event_type, window_start, count FROM notify_dedup ORDER BY event_type"
            )
            pushed: list[dict[str, Any]] = []
            for row in rows:
                event_type = str(row["event_type"])
                count = int(row["count"])
                try:
                    level = level_of(event_type)
                except ValueError:
                    # 表内混入未知类型（只能来自手工改库）：清掉并告警，避免每次空转
                    logger.warning("notify_dedup 存在未知事件类型 %r，已清除", event_type)
                    self._db.execute("DELETE FROM notify_dedup WHERE event_type = ?", (event_type,))
                    continue
                record = EventRecord(
                    event_type=event_type,
                    level=level,
                    payload={
                        "merged": True,
                        "event_type": event_type,
                        "count": count,
                        "window_start": str(row["window_start"]),
                        "window_end": utc_str(at),
                        "message": f"免打扰期间合并推送：{event_type} ×{count}",
                    },
                    id=None,  # 合并推送是投递产物，不新增 notification_events 行
                    created_at=utc_str(at),
                )
                delivered = self._deliver(record)
                self._db.execute("DELETE FROM notify_dedup WHERE event_type = ?", (event_type,))
                pushed.append(
                    {
                        "event_type": event_type,
                        "level": level,
                        "count": count,
                        "delivered": delivered,
                    }
                )
            return pushed

    # ---- 通知中心查询（§10.2 GET /notifications 数据源；始终实时） ----

    def query_events(
        self,
        *,
        level: Level | None = None,
        event_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        """按级别/类型过滤的通知事件页（id 倒序，最新在前）。"""
        return list_events(self._db, level=level, event_type=event_type, limit=limit, offset=offset)


def list_events(
    db: Database,
    *,
    level: Level | None = None,
    event_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """通知中心查询（模块级入口：控制台无需装配 center 即可读取）。

    列 = notification_events 全字段（id/event_type/level/payload_json/
    created_at/delivered_json），id 倒序 + LIMIT/OFFSET 分页。
    """
    sql = "SELECT * FROM notification_events"
    conds: list[str] = []
    params: list[Any] = []
    if level is not None:
        conds.append("level = ?")
        params.append(level)
    if event_type is not None:
        conds.append("event_type = ?")
        params.append(event_type)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return db.query(sql, params)


# ---- 待发箱超时合并扫描 [s6/§8.3/FR6.4] ----

_OUTBOX_PENDING_STATES = ("pending", "stale")  # 未处理口径（见模块 docstring）


def scan_outbox_timeout(
    db: Database,
    config: Config,
    *,
    now: datetime | None = None,
) -> EventRecord | None:
    """扫描滞留超时的待发箱条目，合并为**一条** OUTBOX_TIMEOUT 高级告警 [s6]。

    - 未处理 = state ∈ {pending, stale} 且 ``reminded_at IS NULL``
      （每条目只提醒一次）；approved=用户已确认待发、skipped/expired=已处理，
      均不提醒；
    - 滞留判定：``created_at <= now - messaging.outbox_timeout_min``（UTC
      文本字典序比较）；时段外到期的条目在下一轮扫描自然并入 [B10]；
    - 先置 ``reminded_at`` 再发事件（崩溃窗口内宁可少提醒不重复提醒）；
    - 事件经注册的 NotificationCenter 走完整管线（未注册时仅落库，
      语义与 core.events.emit 一致）；
    - 无滞留条目返回 None。
    """
    at = datetime.now(UTC) if now is None else as_utc(now)
    timeout_min = config.messaging.outbox_timeout_min
    cutoff = utc_str(at - timedelta(minutes=timeout_min))
    placeholders = ", ".join("?" for _ in _OUTBOX_PENDING_STATES)
    rows = db.query(
        f"SELECT id, conv_id, created_at FROM outbox"
        f" WHERE state IN ({placeholders}) AND reminded_at IS NULL AND created_at <= ?",
        (*_OUTBOX_PENDING_STATES, cutoff),
    )
    if not rows:
        return None
    marked_at = utc_str(at)
    items: list[dict[str, Any]] = []
    for row in rows:
        db.execute("UPDATE outbox SET reminded_at = ? WHERE id = ?", (marked_at, row["id"]))
        items.append(
            {
                "id": int(row["id"]),
                "conv_id": str(row["conv_id"]),
                "created_at": str(row["created_at"]),
            }
        )
    payload: dict[str, Any] = {
        "count": len(items),
        "items": items,
        "timeout_min": timeout_min,
        "message": f"待发箱有 {len(items)} 条草稿滞留超过 {timeout_min} 分钟未处理，请及时确认",
    }
    center = center_for(db)
    if center is not None:
        return center.emit(EventType.OUTBOX_TIMEOUT, payload, now=at)
    return store_event(db, EventType.OUTBOX_TIMEOUT, payload, now=at)
