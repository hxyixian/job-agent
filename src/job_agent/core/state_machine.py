"""发送状态机与对账（设计文档 §5.5 I2 / §8.1 匹配规则 [F1] / §13；Stage2 S2b）。

核心安全性质（任务书 S2b 验收）：**任何路径都不存在"自动重发"代码**——
本模块只写 ``send_intents`` 与读取 ``conversations``，不接触 SiteAdapter，
没有重发/重试入口；崩溃恢复仅把残留 ``pending`` 降级为 ``unknown`` [B4]。

时间窗口契约 [F1]：我方消息与 intent 的匹配窗口一律 ±10 分钟；
- greeting：按「会话关联岗位 job_id + 时间窗」匹配，**不做文本比对**
  （实际发出文本=平台设置的招呼语，与控制台模板可能不同）；
- reply / resume：按 ``payload_digest``（:func:`payload_digest` 归一化）+ 时间窗匹配。

对 models.py 的依赖（S2a 已交付）：``Message`` 直接导入用于类型标注；
匹配逻辑只读取 ``direction / conv_id / sent_at / text / platform_msg_id``。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from job_agent.db import Database
from job_agent.models import Message
from job_agent.timeutil import as_utc, utcnow_str

__all__ = [
    "KINDS",
    "MATCH_WINDOW",
    "ReconcileMatch",
    "ReconcileReport",
    "SendIntent",
    "SendStateMachine",
    "payload_digest",
]

SendKind = Literal["greeting", "reply", "resume"]
IntentState = Literal["pending", "sent", "failed", "unknown", "skipped_dryrun"]
ResolveOutcome = Literal["sent", "failed"]

#: 合法 kind 集合（与 send_intents CHECK 一致）
KINDS: tuple[str, ...] = ("greeting", "reply", "resume")
#: F1 匹配窗口：±10 分钟（含端点）
MATCH_WINDOW = timedelta(minutes=10)
#: digest 归一化截断长度（字符数，去空白之后）
_DIGEST_TRUNCATE = 64

# intent.created_at 的库内格式（0001 迁移约定：ISO-8601 UTC，毫秒精度 + Z）
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def payload_digest(text: str) -> str:
    """F1 digest 归一化：去全部空白 → 截断前 64 字符 → sha256 hex。

    调用方（S4c/S4d）调用 :meth:`SendStateMachine.begin` 时必须传
    ``payload_digest(实际发送文本)``；resume（附件，无文本）以空串为口径。
    """
    normalized = "".join(text.split())[:_DIGEST_TRUNCATE]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_utc(ts: str) -> datetime:
    """库内时间戳文本 → UTC datetime（格式见 0001 迁移约定）。"""
    return datetime.strptime(ts, _TS_FORMAT).replace(tzinfo=UTC)


def _coerce_outcome(outcome: str) -> str:
    """resolve 结果归一化为 'sent' / 'failed'。

    契约取值为字符串；同时容忍 ``job_agent.models.SendOutcome``（str Enum）
    成员——按成员名小写映射，与其枚举值写法无关。
    """
    name = getattr(outcome, "name", None)
    text = name.lower() if isinstance(name, str) else str(outcome).lower()
    if text not in ("sent", "failed"):
        raise ValueError(f"非法 outcome: {outcome!r}（仅接受 sent / failed）")
    return text


@dataclass(frozen=True)
class SendIntent:
    """``send_intents`` 行的内存表示（I2 契约引用类型，S2b 提供）。"""

    id: int
    kind: SendKind
    target_ref: str
    payload_digest: str
    state: IntentState
    dry_run: bool
    created_at: str
    resolved_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> SendIntent:
        """从查询行构造（列名 = §6 表字段）。"""
        return cls(
            id=int(row["id"]),
            kind=cast(SendKind, row["kind"]),
            target_ref=row["target_ref"],
            payload_digest=row["payload_digest"],
            state=cast(IntentState, row["state"]),
            dry_run=bool(row["dry_run"]),
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
        )


@dataclass(frozen=True)
class ReconcileMatch:
    """一条我方消息与一个 intent 的对账命中。"""

    intent_id: int
    platform_msg_id: str
    kind: SendKind


@dataclass(frozen=True)
class ReconcileReport:
    """:meth:`SendStateMachine.reconcile` 的结果（供 B3 接管与 §13 WARNING）。"""

    #: 命中（消息 ↔ intent）——intent 原为 pending/unknown 时已置 sent
    matched: list[ReconcileMatch]
    #: 传入的 pending/unknown intent 未找到对应消息（保持原状态，**不重发**）
    unmatched_intents: list[int]
    #: 匹配不上的我方消息 platform_msg_id → 按人工消息处理（B3 接管）
    manual_messages: list[str]


class SendStateMachine:
    """发送状态机（§5.5 I2；调用方：S4c 投递 / S4d 消息与对账任务）。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- I2 契约方法 ----

    def begin(
        self,
        kind: SendKind,
        target_ref: str,
        payload_digest: str,  # 参数名 = I2 契约（函数体内不调用同名模块函数）
        dry_run: bool,
    ) -> int:
        """发送前登记 intent，返回 intent_id。

        - dry_run=False：写 ``pending``，调用方执行发送后 :meth:`resolve`；
        - dry_run=True：**直接写终态 ``skipped_dryrun``**（不写 pending、
          不发送；后续不得对该条调用 resolve）[F2/I2]。
          ``target_ref``：greeting=job_id；reply/resume=conv_id [F1]。
        """
        if kind not in KINDS:
            raise ValueError(f"非法 kind: {kind!r}（仅接受 {KINDS}）")
        if not target_ref:
            raise ValueError("target_ref 不能为空")
        if not payload_digest:
            raise ValueError("payload_digest 不能为空")
        state = "skipped_dryrun" if dry_run else "pending"
        return self._db.execute_returning_id(
            "INSERT INTO send_intents"
            " (kind, target_ref, payload_digest, state, dry_run, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (kind, target_ref, payload_digest, state, int(dry_run), utcnow_str()),
        )

    def resolve(self, intent_id: int, outcome: ResolveOutcome) -> None:
        """记录发送结果（仅 dry_run=False 的 intent 合法）。

        仅 ``pending`` / ``unknown`` 状态可 resolve（后者覆盖崩溃恢复后
        才确认结果的场景）；对 ``skipped_dryrun``、已终态（sent/failed）、
        或不存在的 id 调用均抛 ``ValueError``。
        """
        normalized = _coerce_outcome(outcome)
        row = self._db.query_one(
            "SELECT state, dry_run FROM send_intents WHERE id = ?", (intent_id,)
        )
        if row is None:
            raise ValueError(f"intent {intent_id} 不存在")
        if row["dry_run"]:
            # 含 skipped_dryrun（CHECK: skipped_dryrun ≡ dry_run=1，[R6-3]）
            raise ValueError(f"intent {intent_id} 为 dry_run 记录，不得 resolve")
        if row["state"] not in ("pending", "unknown"):
            raise ValueError(f"intent {intent_id} 状态为 {row['state']}，不可 resolve")
        self._db.execute(
            "UPDATE send_intents SET state = ?, resolved_at = ? WHERE id = ?",
            (normalized, utcnow_str(), intent_id),
        )

    def startup_recover(self) -> None:
        """启动恢复：残留 ``pending`` → ``unknown``（幂等；**只对账不重发** [B4/§13]）。

        本模块没有任何发送能力，"不重发"由结构保证；对账确认见 :meth:`reconcile`。
        """
        self._db.execute("UPDATE send_intents SET state = 'unknown' WHERE state = 'pending'")

    def reconcile(
        self,
        out_messages: list[Message],
        intents: list[SendIntent],
    ) -> ReconcileReport:
        """对账（§13：低频任务，取回我方消息核对 intent，绝不重发）。

        匹配规则 [F1]：
        - greeting：会话关联岗位（``conversations.job_id``，经 DB 查询）==
          intent.target_ref 且 |Δt| ≤ 10min，**不比对文本**；
        - reply/resume：msg.conv_id == intent.target_ref 且
          ``payload_digest(msg.text) == intent.payload_digest`` 且 |Δt| ≤ 10min。

        语义：
        - 命中的 pending/unknown intent → 置 ``sent``（sent/failed 只计入报告，
          不改库——failed 保守保持失败留痕）；
        - dry_run intent（skipped_dryrun）**不参与匹配**：试运行从未发送，
          且 CHECK [R6-3] 禁止其转为 sent；与 dry_run intent 文本相同的真实
          消息属于用户手发 → manual；
        - 两种匹配均不上的我方消息 → manual_messages（B3 接管）。每日首次
          send_greeting 的核验读回消息天然按岗位+时间窗命中，不误判 [F1]；
          平台招呼语与模板不一致触发的 ``GREETING_MISMATCH`` 由投递层负责，
          不在本模块处理。

        :param out_messages: 会话取回的候选消息（内部再过滤 direction=="out"）。
        :param intents: 待核对 intent（通常 ``load_open_intents()`` 的结果）。
        """
        # 1. 过滤 + 按时间排序（确定性：先到先配）
        msgs = [m for m in out_messages if m.direction == "out"]
        msgs.sort(key=lambda m: (as_utc(m.sent_at), m.platform_msg_id))

        # 2. 会话 → 关联岗位（greeting 匹配依据；查不到 = 无 greeting 候选）
        conv_job: dict[str, str | None] = {}
        for conv_id in {m.conv_id for m in msgs}:
            row = self._db.query_one(
                "SELECT job_id FROM conversations WHERE conv_id = ?", (conv_id,)
            )
            conv_job[conv_id] = row["job_id"] if row is not None else None

        # 3. dry_run intent 不参与匹配（见 docstring）；其余贪心一对一配对
        candidates = [i for i in intents if not i.dry_run]
        matched_rows: dict[int, SendIntent] = {}
        matched: list[ReconcileMatch] = []
        manual: list[str] = []
        for msg in msgs:
            msg_time = as_utc(msg.sent_at)
            best_key: tuple[float, int] | None = None
            best_intent: SendIntent | None = None
            for intent in candidates:
                if intent.id in matched_rows:
                    continue
                if not self._intent_matches(intent, msg, msg_time, conv_job.get(msg.conv_id)):
                    continue
                delta = abs((msg_time - _parse_utc(intent.created_at)).total_seconds())
                key = (delta, intent.id)
                if best_key is None or key < best_key:
                    best_key, best_intent = key, intent
            if best_intent is None:
                manual.append(msg.platform_msg_id)
                continue
            matched_rows[best_intent.id] = best_intent
            matched.append(
                ReconcileMatch(
                    intent_id=best_intent.id,
                    platform_msg_id=msg.platform_msg_id,
                    kind=best_intent.kind,
                )
            )

        # 4. 命中的 pending/unknown → sent（事务内统一落库）
        to_settle = [i for i in matched_rows.values() if i.state in ("pending", "unknown")]
        if to_settle:
            now = utcnow_str()
            with self._db.transaction() as conn:
                for intent in to_settle:
                    conn.execute(
                        "UPDATE send_intents SET state = 'sent', resolved_at = ? WHERE id = ?",
                        (now, intent.id),
                    )

        unmatched = [
            i.id
            for i in candidates
            if i.id not in matched_rows and i.state in ("pending", "unknown")
        ]
        return ReconcileReport(
            matched=matched,
            unmatched_intents=unmatched,
            manual_messages=manual,
        )

    # ---- 供 S4d 对账任务使用的辅助查询 ----

    def load_open_intents(self) -> list[SendIntent]:
        """未闭合的 intent（pending/unknown），按创建时间升序。"""
        rows = self._db.query(
            "SELECT id, kind, target_ref, payload_digest, state, dry_run,"
            " created_at, resolved_at"
            " FROM send_intents WHERE state IN ('pending', 'unknown')"
            " ORDER BY created_at, id"
        )
        return [SendIntent.from_row(row) for row in rows]

    # ---- 内部 ----

    def _intent_matches(
        self,
        intent: SendIntent,
        msg: Message,
        msg_time: datetime,
        conv_job_id: str | None,
    ) -> bool:
        """F1 双路匹配：greeting=岗位+时间窗（不看文本）；reply/resume=digest+时间窗。"""
        delta = abs((msg_time - _parse_utc(intent.created_at)).total_seconds())
        if delta > MATCH_WINDOW.total_seconds():
            return False
        if intent.kind == "greeting":
            return conv_job_id is not None and conv_job_id == intent.target_ref
        return (
            msg.conv_id == intent.target_ref and payload_digest(msg.text) == intent.payload_digest
        )
