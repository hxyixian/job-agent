"""会话状态 API（§5.5 I8 提供方；调用方 S4c/S4d）[F2 补全]。

- **计数口径 [A2/FR6.3]**：``auto_sent_count`` 覆盖主动/被动招呼语与自动
  回复消息（``kind="message"`` 统一表达，**不含附件简历**）；
  ``resume_sent_count`` 为附件简历独立计数（每会话 ≤1，不占消息额度）。
- **转人工 [B3/G6/F1]**：达 ``auto_reply_limit`` → ``manual_takeover=1`` +
  CONV_LIMIT_REACHED 中级事件（§8.1）；用户手回消息（I2 reconcile 匹配
  不上的我方消息）→ :meth:`mark_manual_takeover` 置位。**每日首投
  greeting 核验的读回消息不会走到 digest_miss 置位**——它按"岗位+
  ±10min 窗口"天然匹配 greeting intent（§5.5 I2/F1），匹配规则属 S2b
  reconcile，本模块无需特判；GREETING_MISMATCH 走独立事件、**不触发
  接管** [F1]。
- **试运行隔离 [N2]**：试运行模式下 :meth:`record_auto_send` 不计数
  （拟发送不进 FR6.3 会话自动发送计数的写侧保证；查询侧见 core/dryrun）。
- **基线 [B3]**：:meth:`snapshot_baseline` 写入基线快照（首次启动与关闭
  dry_run 重拍 [N2]，快照数据由 adapter 提供）；:meth:`baseline_filter`
  过滤 ``is_baseline=1`` 会话（不参与自动化）。
- **greeting 核验标记 [F1/§I8]**：每日首次 ``send_greeting`` 成功后的
  核验去重标记（"今天做过没有"），核验本身（读回消息与模板归一化比对）
  属 S4c 流水线。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Literal, NamedTuple

from job_agent.config import Config
from job_agent.db import Database
from job_agent.timeutil import utcnow_str

from . import dryrun, events, runstate

__all__ = [
    "KEY_GREETING_PROBE",
    "TAKEOVER_SOURCES",
    "BaselineEntry",
    "ConversationState",
]

SendKind = Literal["message", "resume"]

# run_state 键：每日 greeting 核验标记 {"last_date": "YYYY-MM-DD"}（北京日，timeutil）
KEY_GREETING_PROBE = "greeting_probe"

# I8 契约的接管来源枚举（digest_miss=对账匹配不上我方消息；user_flag=用户手动标记）
TAKEOVER_SOURCES = frozenset({"digest_miss", "user_flag"})

# 占位 peer_name：record_auto_send/mark_manual_takeover 先于会话列表同步到达时的建行值
_PEER_UNKNOWN = ""


class BaselineEntry(NamedTuple):
    """基线快照的一条会话（§6 conversations 最小建行列）。"""

    conv_id: str
    peer_name: str
    job_id: str | None


class ConversationState:
    """会话状态机（计数/转人工/基线/核验标记），状态落在 conversations/run_state 表。"""

    def __init__(self, db: Database, config: Config) -> None:
        self._db = db
        self._config = config

    # ---- I8：发送许可与计数 ----

    def can_auto_send(self, conv_id: str, kind: SendKind) -> tuple[bool, str]:
        """自动发送许可 [I8]：message=未接管∧count<limit；resume=未接管∧count<1。

        返回 (allowed, reason)；reason 取 "ok" / "manual_takeover" /
        "auto_reply_limit_reached" / "resume_limit_reached"（稳定字符串，
        供日志与测试断言）。无会话行的新会话不受限。
        """
        if kind not in ("message", "resume"):
            raise ValueError(f"未知 kind：{kind!r}（I8 契约为 message|resume）")
        row = self._db.query_one(
            "SELECT manual_takeover, auto_sent_count, resume_sent_count "
            "FROM conversations WHERE conv_id = ?",
            (conv_id,),
        )
        if row is None:
            return True, "ok"
        if row["manual_takeover"]:
            return False, "manual_takeover"
        if kind == "message":
            if row["auto_sent_count"] >= self._auto_reply_limit():
                return False, "auto_reply_limit_reached"
        elif row["resume_sent_count"] >= self._resume_limit():
            return False, "resume_limit_reached"
        return True, "ok"

    def record_auto_send(self, conv_id: str, kind: SendKind) -> None:
        """记录一次自动发送 [I8/§8.1]。

        - message：``auto_sent_count`` +1（含招呼语）；**达到 ``auto_reply_limit``
          → ``manual_takeover=1`` + CONV_LIMIT_REACHED 中级事件**（§8.1/A-CORE-6）；
        - resume：``resume_sent_count`` +1（每会话 ≤1 由 can_auto_send 前置把关）；
        - 试运行模式：不计数 [N2]（防御性隔离——dry_run 下调用方本就不实际
          发送；拟发送记录走 core/dryrun.record_planned_send）。
        """
        if kind not in ("message", "resume"):
            raise ValueError(f"未知 kind：{kind!r}（I8 契约为 message|resume）")
        if dryrun.is_dry_run(self._db, self._config):
            return
        takeover_now = False
        count_after = 0
        with self._db.transaction() as conn:
            if kind == "message":
                conn.execute(
                    "INSERT INTO conversations (conv_id, peer_name, auto_sent_count, updated_at)"
                    " VALUES (?, ?, 1, ?)"
                    " ON CONFLICT(conv_id) DO UPDATE SET"
                    " auto_sent_count = conversations.auto_sent_count + 1,"
                    " updated_at = excluded.updated_at",
                    (conv_id, _PEER_UNKNOWN, utcnow_str()),
                )
                row = conn.execute(
                    "SELECT auto_sent_count AS c, manual_takeover FROM conversations"
                    " WHERE conv_id = ?",
                    (conv_id,),
                ).fetchone()
            else:
                conn.execute(
                    "INSERT INTO conversations (conv_id, peer_name, resume_sent_count,"
                    " updated_at) VALUES (?, ?, 1, ?)"
                    " ON CONFLICT(conv_id) DO UPDATE SET"
                    " resume_sent_count = conversations.resume_sent_count + 1,"
                    " updated_at = excluded.updated_at",
                    (conv_id, _PEER_UNKNOWN, utcnow_str()),
                )
                row = conn.execute(
                    "SELECT resume_sent_count AS c, manual_takeover FROM conversations"
                    " WHERE conv_id = ?",
                    (conv_id,),
                ).fetchone()
            if row is None:  # UPSERT 之后必然存在（防御分支）
                raise RuntimeError(f"会话行写入后消失：{conv_id}")
            count_after = int(row["c"])
            takeover_now = (
                kind == "message"
                and not row["manual_takeover"]
                and count_after >= self._auto_reply_limit()
            )
            if takeover_now:
                conn.execute(
                    "UPDATE conversations SET manual_takeover = 1,"
                    " blocked_reason = ?, updated_at = ? WHERE conv_id = ?",
                    ("auto_reply_limit_reached", utcnow_str(), conv_id),
                )
        if takeover_now:
            events.emit(
                self._db,
                "CONV_LIMIT_REACHED",
                {
                    "conv_id": conv_id,
                    "auto_sent_count": count_after,
                    "auto_reply_limit": self._auto_reply_limit(),
                    "action": "manual_takeover",
                },
            )

    def mark_manual_takeover(self, conv_id: str, source: str) -> None:
        """置位转人工 [I8/B3]。source ∈ {"digest_miss", "user_flag"}，其余拒绝。

        幂等且**首因保留**：已置位时不再改写 blocked_reason（保留最早原因）；
        转人工在 v1 为单向（§8.3：AI 可继续草拟进待发箱，不再自动发送）。
        """
        if source not in TAKEOVER_SOURCES:
            raise ValueError(f"未知接管来源：{source!r}（I8 契约为 digest_miss|user_flag）")
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO conversations (conv_id, peer_name, manual_takeover, blocked_reason,"
                " updated_at) VALUES (?, ?, 1, ?, ?)"
                " ON CONFLICT(conv_id) DO UPDATE SET manual_takeover = 1,"
                " blocked_reason = CASE WHEN conversations.manual_takeover = 1"
                " THEN conversations.blocked_reason ELSE excluded.blocked_reason END,"
                " updated_at = excluded.updated_at",
                (conv_id, _PEER_UNKNOWN, f"manual_takeover:{source}", utcnow_str()),
            )

    # ---- I8：基线 [B3] ----

    def baseline_filter(self, conv_ids: list[str]) -> list[str]:
        """过滤掉 ``is_baseline=1`` 的会话 [I8]（基线会话不参与自动化）。

        未入库的 conv_id 原样通过（无状态 = 非基线）；保持输入顺序。
        """
        baseline: set[str] = set()
        for start in range(0, len(conv_ids), 500):  # sqlite 变量数上限，分批 IN
            chunk = conv_ids[start : start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._db.query(
                "SELECT conv_id FROM conversations"
                f" WHERE is_baseline = 1 AND conv_id IN ({placeholders})",
                chunk,
            )
            baseline.update(str(r["conv_id"]) for r in rows)
        return [c for c in conv_ids if c not in baseline]

    def snapshot_baseline(self, entries: Sequence[BaselineEntry]) -> int:
        """写入基线快照（首次启动 / 关闭 dry_run 重拍 [N2/B3]），返回写入条数。

        已有会话行只置 ``is_baseline=1``：计数/接管等既有状态不清空；
        peer_name 仅在快照提供非空值时更新（占位空串让位真名），
        job_id 仅在原值为空时补写。
        """
        now = utcnow_str()
        with self._db.transaction() as conn:
            for entry in entries:
                conn.execute(
                    "INSERT INTO conversations (conv_id, peer_name, job_id, is_baseline,"
                    " updated_at) VALUES (?, ?, ?, 1, ?)"
                    " ON CONFLICT(conv_id) DO UPDATE SET is_baseline = 1,"
                    " updated_at = excluded.updated_at,"
                    " peer_name = CASE WHEN excluded.peer_name != ''"
                    " THEN excluded.peer_name ELSE conversations.peer_name END,"
                    " job_id = CASE WHEN conversations.job_id IS NULL"
                    " THEN excluded.job_id ELSE conversations.job_id END",
                    (entry.conv_id, entry.peer_name, entry.job_id, now),
                )
        return len(entries)

    # ---- I8：每日 greeting 核验标记 [F1] ----

    def is_greeting_probe_done(self, today: date) -> bool:
        """当日 greeting 核验是否已完成 [I8/F1]（"今天是否已核验过"）。"""
        state = runstate.get_state(self._db, KEY_GREETING_PROBE)
        return state is not None and state.get("last_date") == today.isoformat()

    def mark_greeting_probe(self, today: date) -> None:
        """标记当日核验完成 [I8/F1]；跨日自动失效（只存最近一日）。"""
        runstate.set_state(self._db, KEY_GREETING_PROBE, {"last_date": today.isoformat()})

    # ---- 内部 ----

    def _auto_reply_limit(self) -> int:
        return self._config.messaging.auto_reply_limit

    def _resume_limit(self) -> int:
        # §4 messaging.resume_auto_send_limit_per_conv（0-1，默认 1）[A2/R6-1]
        return self._config.messaging.resume_auto_send_limit_per_conv
