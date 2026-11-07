"""S2b 发送状态机与对账单元测试（设计文档 §5.5 I2 / §8.1 [F1] / §13）。

覆盖任务书 S2b 验收点：
- begin 的 dry_run 双分支（pending / 直接 skipped_dryrun）与 resolve 合法性；
- T4-1 崩溃恢复：残留 pending → unknown，**不重发**（模块无发送能力，
  恢复后 intent 总数不变、仅状态迁移）；
- reconcile 双路匹配 [F1]：greeting=岗位+时间窗（不做文本比对）、
  reply/resume=payload_digest+时间窗；匹配不上 → 人工消息（B3）；
- digest 归一化（去空白 / 截断 64 字符）；±10min 窗口边界。

对 models.py 的依赖：S2a 已交付，直接使用真实 ``Message``/``MsgType``。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from job_agent.core.state_machine import (
    MATCH_WINDOW,
    ReconcileReport,
    SendIntent,
    SendStateMachine,
    payload_digest,
)
from job_agent.db import Database, open_database
from job_agent.models import Message, MsgType

# 固定时基（UTC）：intent 创建于 T0
T0_STR = "2026-11-09T02:00:00.000Z"
T0 = datetime(2026, 11, 9, 2, 0, tzinfo=UTC)


def insert_intent(
    db: Database,
    *,
    kind: str = "reply",
    target: str = "c1",
    digest: str = "d0",
    state: str = "unknown",
    dry_run: int = 0,
    created_at: str = T0_STR,
) -> int:
    """绕过状态机直接插行（完全控制 created_at / state，保证确定性）。"""
    return db.execute_returning_id(
        "INSERT INTO send_intents (kind, target_ref, payload_digest, state, dry_run, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (kind, target, digest, state, dry_run, created_at),
    )


def mk_msg(
    mid: str,
    conv: str,
    *,
    text: str = "",
    when: datetime,
    direction: str = "out",
    sender: str = "me",
) -> Message:
    return Message(
        platform_msg_id=mid,
        conv_id=conv,
        direction=direction,
        sender=sender,
        msg_type=MsgType.TEXT,
        text=text,
        sent_at=when,
        is_ours_auto=None,
    )


def add_conv(db: Database, conv_id: str, job_id: str | None) -> None:
    db.execute(
        "INSERT INTO conversations (conv_id, peer_name, job_id) VALUES (?, ?, ?)",
        (conv_id, "HR", job_id),
    )


def all_intents(db: Database) -> list[SendIntent]:
    """全量 intent（含终态/skipped_dryrun——reconcile 的候选由调用方决定）。"""
    rows = db.query(
        "SELECT id, kind, target_ref, payload_digest, state, dry_run,"
        " created_at, resolved_at FROM send_intents ORDER BY id"
    )
    return [SendIntent.from_row(row) for row in rows]


class BeginAndResolveTests(unittest.TestCase):
    """I2 begin/resolve 契约（含 F2 dry_run 分支与 R6-3 CHECK 语义）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="job_agent_s2b_sm_")
        self.addCleanup(self._tmp.cleanup)
        self.db = open_database(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.db.close)  # LIFO：先关连接再删临时目录（Windows 文件占用）
        self.sm = SendStateMachine(self.db)

    def row(self, intent_id: int):  # 测试辅助（类型标注从简）
        return self.db.query_one("SELECT * FROM send_intents WHERE id = ?", (intent_id,))

    def test_begin_real_run_writes_pending(self) -> None:
        iid = self.sm.begin("greeting", "job_1", payload_digest("你好"), dry_run=False)
        row = self.row(iid)
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["dry_run"], 0)
        self.assertEqual(row["kind"], "greeting")
        self.assertEqual(row["target_ref"], "job_1")
        self.assertEqual(row["payload_digest"], payload_digest("你好"))
        self.assertIsNone(row["resolved_at"])

    def test_begin_dry_run_writes_skipped_dryrun_directly(self) -> None:
        """dry_run=True 直接写终态 skipped_dryrun（不写 pending）[F2/I2]。"""
        iid = self.sm.begin("reply", "c1", payload_digest("好的"), dry_run=True)
        row = self.row(iid)
        self.assertEqual(row["state"], "skipped_dryrun")
        self.assertEqual(row["dry_run"], 1)
        self.assertIsNone(row["resolved_at"])

    def test_begin_validates_arguments(self) -> None:
        with self.assertRaises(ValueError):
            self.sm.begin("chat", "c1", "d", dry_run=False)  # 非法 kind
        with self.assertRaises(ValueError):
            self.sm.begin("reply", "", "d", dry_run=False)  # 空 target_ref
        with self.assertRaises(ValueError):
            self.sm.begin("reply", "c1", "", dry_run=False)  # 空 digest

    def test_resolve_sent_and_failed(self) -> None:
        iid = self.sm.begin("greeting", "job_1", "d", dry_run=False)
        self.sm.resolve(iid, "sent")
        self.assertEqual(self.row(iid)["state"], "sent")
        self.assertIsNotNone(self.row(iid)["resolved_at"])

        iid2 = self.sm.begin("reply", "c1", "d", dry_run=False)
        self.sm.resolve(iid2, "failed")
        self.assertEqual(self.row(iid2)["state"], "failed")

    def test_resolve_rejects_dry_run_intent(self) -> None:
        """对 skipped_dryrun 调用 resolve 抛 ValueError（I2 明文）。"""
        iid = self.sm.begin("reply", "c1", "d", dry_run=True)
        with self.assertRaises(ValueError):
            self.sm.resolve(iid, "sent")
        # 状态保持终态，未被污染
        self.assertEqual(self.row(iid)["state"], "skipped_dryrun")

    def test_resolve_rejects_invalid_target(self) -> None:
        with self.assertRaises(ValueError):
            self.sm.resolve(999999, "sent")  # 不存在
        iid = self.sm.begin("reply", "c1", "d", dry_run=False)
        self.sm.resolve(iid, "sent")
        with self.assertRaises(ValueError):
            self.sm.resolve(iid, "failed")  # 已终态，不得覆盖
        with self.assertRaises(ValueError):
            self.sm.resolve(iid, "maybe")  # 非法 outcome

    def test_resolve_accepts_sendoutcome_enum(self) -> None:
        """容忍 S2a SendOutcome（StrEnum，小写值）成员与名称映射两种口径。"""
        from job_agent.models import SendOutcome

        iid = self.sm.begin("reply", "c1", "d", dry_run=False)
        self.sm.resolve(iid, SendOutcome.SENT)  # type: ignore[arg-type]
        self.assertEqual(self.row(iid)["state"], "sent")

        class NameOnly(StrEnum):
            SENT = "<SendOutcome.SENT>"  # 故意用非契约值：验证按 name 映射

        iid2 = self.sm.begin("reply", "c1", "d", dry_run=False)
        self.sm.resolve(iid2, NameOnly.SENT)  # type: ignore[arg-type]
        self.assertEqual(self.row(iid2)["state"], "sent")


class StartupRecoverTests(unittest.TestCase):
    """T4-1 崩溃恢复：残留 pending → unknown，绝不重发 [B4/§13]。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="job_agent_s2b_rec_")
        self.addCleanup(self._tmp.cleanup)
        self.db = open_database(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.db.close)
        self.sm = SendStateMachine(self.db)

    def test_pending_becomes_unknown_without_resend(self) -> None:
        pending1 = insert_intent(self.db, state="pending", target="job_1", kind="greeting")
        pending2 = insert_intent(self.db, state="pending", target="c1")
        sent = insert_intent(self.db, state="sent")
        failed = insert_intent(self.db, state="failed")
        skipped = insert_intent(self.db, state="skipped_dryrun", dry_run=1)

        self.sm.startup_recover()

        # pending → unknown（两个残留全部迁移）
        self.assertEqual(
            self.db.query_one("SELECT state FROM send_intents WHERE id = ?", (pending1,))["state"],
            "unknown",
        )
        self.assertEqual(
            self.db.query_one("SELECT state FROM send_intents WHERE id = ?", (pending2,))["state"],
            "unknown",
        )
        # 其余状态不动
        self.assertEqual(
            self.db.query_one("SELECT state FROM send_intents WHERE id = ?", (sent,))["state"],
            "sent",
        )
        self.assertEqual(
            self.db.query_one("SELECT state FROM send_intents WHERE id = ?", (failed,))["state"],
            "failed",
        )
        self.assertEqual(
            self.db.query_one("SELECT state FROM send_intents WHERE id = ?", (skipped,))["state"],
            "skipped_dryrun",
        )
        # "不重发"：总行数不变、没有新增 pending（模块无任何发送/重建入口）
        self.assertEqual(self.db.query_one("SELECT COUNT(*) AS n FROM send_intents")["n"], 5)
        # 幂等：重复执行无副作用
        self.sm.startup_recover()
        self.assertEqual(
            self.db.query_one("SELECT COUNT(*) AS n FROM send_intents WHERE state='pending'")["n"],
            0,
        )

    def test_recovered_unknown_can_be_resolved_later(self) -> None:
        """崩溃恢复后若拿到确定结果（如适配器迟到的确认），允许补 resolve。"""
        iid = self.sm.begin("reply", "c1", "d", dry_run=False)
        self.sm.startup_recover()
        self.sm.resolve(iid, "sent")
        row = self.db.query_one("SELECT state, resolved_at FROM send_intents WHERE id = ?", (iid,))
        self.assertEqual(row["state"], "sent")
        self.assertIsNotNone(row["resolved_at"])


class ReconcileTests(unittest.TestCase):
    """reconcile 双路匹配 [F1] 与 B3 人工消息判定。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="job_agent_s2b_rc_")
        self.addCleanup(self._tmp.cleanup)
        self.db = open_database(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.db.close)
        self.sm = SendStateMachine(self.db)

    def state_of(self, iid: int) -> str:
        row = self.db.query_one("SELECT state FROM send_intents WHERE id = ?", (iid,))
        assert row is not None
        return str(row["state"])

    def test_greeting_matches_by_job_and_window_not_text(self) -> None:
        """greeting 按关联岗位 + 时间窗匹配；平台文本与模板不同仍命中 [F1]。"""
        add_conv(self.db, "c1", job_id="job_1")
        iid = insert_intent(self.db, kind="greeting", target="job_1", state="unknown")
        # 读回文本 = 平台设置的招呼语（与控制台模板完全不同）
        msg = mk_msg(
            "m1",
            "c1",
            text="您好，我看了这个职位很感兴趣（平台预设）",
            when=T0 + timedelta(minutes=2),
        )

        report = self.sm.reconcile([msg], self.sm.load_open_intents())

        self.assertEqual(len(report.matched), 1)
        self.assertEqual(report.matched[0].intent_id, iid)
        self.assertEqual(report.matched[0].platform_msg_id, "m1")
        self.assertEqual(report.matched[0].kind, "greeting")
        self.assertEqual(report.manual_messages, [])
        self.assertEqual(report.unmatched_intents, [])
        self.assertEqual(self.state_of(iid), "sent")  # unknown → sent 落库

    def test_greeting_no_match_other_job(self) -> None:
        add_conv(self.db, "c1", job_id="job_1")
        add_conv(self.db, "c2", job_id="job_2")
        iid = insert_intent(self.db, kind="greeting", target="job_1", state="unknown")
        msg = mk_msg("m1", "c2", text="您好", when=T0 + timedelta(minutes=2))

        report = self.sm.reconcile([msg], self.sm.load_open_intents())

        self.assertEqual(report.matched, [])
        self.assertEqual(report.unmatched_intents, [iid])
        self.assertEqual(report.manual_messages, ["m1"])
        self.assertEqual(self.state_of(iid), "unknown")  # 保持 unknown，不重发

    def test_greeting_no_match_missing_job_association(self) -> None:
        """会话查不到关联岗位（无行 / job_id 为 NULL）→ 无 greeting 候选。"""
        add_conv(self.db, "c1", job_id=None)
        iid = insert_intent(self.db, kind="greeting", target="job_1", state="unknown")
        msg = mk_msg("m1", "c1", text="您好", when=T0 + timedelta(minutes=2))

        report = self.sm.reconcile([msg], self.sm.load_open_intents())

        self.assertEqual(report.matched, [])
        self.assertEqual(report.unmatched_intents, [iid])
        self.assertEqual(report.manual_messages, ["m1"])

    def test_window_boundary_exactly_10min_matches(self) -> None:
        add_conv(self.db, "c1", job_id="job_1")
        iid = insert_intent(self.db, kind="greeting", target="job_1", state="unknown")
        at_edge = mk_msg("m_edge", "c1", when=T0 + MATCH_WINDOW)
        over_edge = mk_msg("m_over", "c2", when=T0 + MATCH_WINDOW + timedelta(seconds=1))
        add_conv(self.db, "c2", job_id="job_1")  # 同岗位：仅时间超窗

        report = self.sm.reconcile([at_edge, over_edge], self.sm.load_open_intents())

        self.assertEqual([m.platform_msg_id for m in report.matched], ["m_edge"])
        self.assertEqual(report.manual_messages, ["m_over"])
        self.assertEqual(self.state_of(iid), "sent")

    def test_time_window_overflow_no_match(self) -> None:
        """时间窗超出 ±10min（消息晚 11min）→ 不匹配：intent 保持 unknown、消息人工。"""
        add_conv(self.db, "c1", job_id="job_1")
        iid = insert_intent(self.db, kind="greeting", target="job_1", state="unknown")
        msg = mk_msg("m1", "c1", text="您好", when=T0 + timedelta(minutes=11))

        report = self.sm.reconcile([msg], self.sm.load_open_intents())

        self.assertEqual(report.matched, [])
        self.assertEqual(report.unmatched_intents, [iid])
        self.assertEqual(report.manual_messages, ["m1"])
        self.assertEqual(self.state_of(iid), "unknown")

    def test_reply_matches_by_digest_normalized(self) -> None:
        """reply 按 digest 匹配：空白差异无关、64 字符之后差异无关 [F1]。"""
        add_conv(self.db, "c1", job_id="job_1")
        text = "您好，我对这个职位很感兴趣，方便沟通一下吗？" * 10  # > 64 字符
        iid = insert_intent(
            self.db, kind="reply", target="c1", digest=payload_digest(text), state="unknown"
        )
        # 平台读回：插入了空白，且 64 字符后内容被改
        read_back = text[:64].replace("，", "， ", 1) + "（后续内容不同）"
        msg = mk_msg("m1", "c1", text=read_back, when=T0 + timedelta(minutes=3))

        report = self.sm.reconcile([msg], self.sm.load_open_intents())

        self.assertEqual([m.intent_id for m in report.matched], [iid])
        self.assertEqual(self.state_of(iid), "sent")

    def test_reply_digest_mismatch_goes_manual(self) -> None:
        add_conv(self.db, "c1", job_id=None)
        iid = insert_intent(
            self.db,
            kind="reply",
            target="c1",
            digest=payload_digest("自动回复原文"),
            state="unknown",
        )
        # 用户手回了不同内容 → 匹配不上 → 人工（B3 接管）
        msg = mk_msg("m1", "c1", text="我自己手打的内容", when=T0 + timedelta(minutes=1))

        report = self.sm.reconcile([msg], self.sm.load_open_intents())

        self.assertEqual(report.matched, [])
        self.assertEqual(report.manual_messages, ["m1"])
        self.assertEqual(report.unmatched_intents, [iid])

    def test_reply_requires_same_conversation(self) -> None:
        """digest 相同但会话不同（HR 转述等）→ 不匹配。"""
        add_conv(self.db, "c1", job_id=None)
        add_conv(self.db, "c2", job_id=None)
        digest = payload_digest("相同的文本")
        iid = insert_intent(self.db, kind="reply", target="c1", digest=digest, state="unknown")
        msg = mk_msg("m1", "c2", text="相同的文本", when=T0 + timedelta(minutes=1))

        report = self.sm.reconcile([msg], self.sm.load_open_intents())

        self.assertEqual(report.matched, [])
        self.assertEqual(report.unmatched_intents, [iid])
        self.assertEqual(report.manual_messages, ["m1"])

    def test_resume_matches_with_empty_text_digest(self) -> None:
        """附件简历无文本：digest 空串口径，靠 conv + 时间窗命中。"""
        add_conv(self.db, "c1", job_id=None)
        iid = insert_intent(
            self.db, kind="resume", target="c1", digest=payload_digest(""), state="unknown"
        )
        msg = mk_msg("m1", "c1", text="", when=T0 - timedelta(minutes=10))  # 恰在窗口端点

        report = self.sm.reconcile([msg], self.sm.load_open_intents())

        self.assertEqual([m.intent_id for m in report.matched], [iid])
        self.assertEqual(report.matched[0].kind, "resume")

    def test_dry_run_intent_never_matches(self) -> None:
        """dry_run intent 从未发送且 CHECK 禁止转 sent：不参与匹配 [R6-3/N2]。

        与其 digest 相同的真实消息 = 用户手发 → 人工。
        """
        add_conv(self.db, "c1", job_id=None)
        iid = insert_intent(
            self.db,
            kind="reply",
            target="c1",
            digest=payload_digest("文本"),
            state="skipped_dryrun",
            dry_run=1,
        )
        msg = mk_msg("m1", "c1", text="文本", when=T0 + timedelta(minutes=1))

        # 显式传入 skipped_dryrun intent：验证 reconcile 内部同样排除 [R6-3]
        report = self.sm.reconcile([msg], all_intents(self.db))

        self.assertEqual(report.matched, [])
        self.assertEqual(report.manual_messages, ["m1"])
        self.assertEqual(self.state_of(iid), "skipped_dryrun")  # 原样不动

    def test_incoming_messages_ignored(self) -> None:
        """direction != out 的消息不参与对账（也不算人工）。"""
        add_conv(self.db, "c1", job_id=None)
        insert_intent(
            self.db, kind="reply", target="c1", digest=payload_digest("x"), state="unknown"
        )
        incoming = mk_msg(
            "m1", "c1", text="x", when=T0 + timedelta(minutes=1), direction="in", sender="hr"
        )

        report = self.sm.reconcile([incoming], self.sm.load_open_intents())

        self.assertEqual(report.matched, [])
        self.assertEqual(report.manual_messages, [])

    def test_one_to_one_greedy_matching(self) -> None:
        """两条相同消息只配一个 intent；多余消息按人工处理。"""
        add_conv(self.db, "c1", job_id=None)
        iid = insert_intent(
            self.db, kind="reply", target="c1", digest=payload_digest("重试文本"), state="unknown"
        )
        m_first = mk_msg("m1", "c1", text="重试文本", when=T0 + timedelta(minutes=1))
        m_second = mk_msg("m2", "c1", text="重试文本", when=T0 + timedelta(minutes=2))

        report = self.sm.reconcile([m_second, m_first], self.sm.load_open_intents())

        self.assertEqual(len(report.matched), 1)
        self.assertEqual(report.matched[0].intent_id, iid)
        self.assertEqual(report.matched[0].platform_msg_id, "m1")  # 时间更近者
        self.assertEqual(report.manual_messages, ["m2"])

    def test_pending_intent_reconciled_to_sent(self) -> None:
        """引擎运行中（未恢复的 pending）同样可被对账确认。"""
        add_conv(self.db, "c1", job_id=None)
        iid = insert_intent(
            self.db, kind="reply", target="c1", digest=payload_digest("x"), state="pending"
        )
        msg = mk_msg("m1", "c1", text="x", when=T0 + timedelta(seconds=30))

        report = self.sm.reconcile([msg], self.sm.load_open_intents())

        self.assertEqual([m.intent_id for m in report.matched], [iid])
        self.assertEqual(self.state_of(iid), "sent")

    def test_terminal_intents_attributed_but_not_mutated(self) -> None:
        """sent/failed intent 命中只入报告不改库（failed 保守保持失败留痕）。"""
        add_conv(self.db, "c1", job_id=None)
        sent_iid = insert_intent(
            self.db, kind="reply", target="c1", digest=payload_digest("a"), state="sent"
        )
        failed_iid = insert_intent(
            self.db, kind="reply", target="c1", digest=payload_digest("b"), state="failed"
        )
        m1 = mk_msg("m1", "c1", text="a", when=T0 + timedelta(minutes=1))
        m2 = mk_msg("m2", "c1", text="b", when=T0 + timedelta(minutes=2))

        # 全量传入（load_open_intents 只取 pending/unknown，此处含终态）
        report = self.sm.reconcile([m1, m2], all_intents(self.db))

        self.assertEqual(len(report.matched), 2)  # 归属成立（is_ours_auto）
        self.assertEqual(report.manual_messages, [])
        self.assertEqual(report.unmatched_intents, [])
        self.assertEqual(self.state_of(sent_iid), "sent")
        self.assertEqual(self.state_of(failed_iid), "failed")

    def test_reconcile_creates_nothing(self) -> None:
        """对账绝不新建 intent / 消息（结构上只 UPDATE send_intents）。"""
        add_conv(self.db, "c1", job_id="job_1")
        insert_intent(self.db, kind="greeting", target="job_1", state="unknown")
        before = self.db.query_one("SELECT COUNT(*) AS n FROM send_intents")["n"]
        self.sm.reconcile([mk_msg("m1", "c1", when=T0)], self.sm.load_open_intents())
        after = self.db.query_one("SELECT COUNT(*) AS n FROM send_intents")["n"]
        self.assertEqual(before, after)

    def test_report_shape(self) -> None:
        """ReconcileReport 为纯数据结果（供 B3/通知层消费）。"""
        add_conv(self.db, "c1", job_id=None)
        insert_intent(
            self.db, kind="reply", target="c1", digest=payload_digest("x"), state="unknown"
        )
        report = self.sm.reconcile([], self.sm.load_open_intents())
        self.assertIsInstance(report, ReconcileReport)
        self.assertEqual(report.matched, [])
        self.assertEqual(report.unmatched_intents, [1])
        self.assertEqual(report.manual_messages, [])


class DigestHelperTests(unittest.TestCase):
    """payload_digest 归一化口径（去空白 / 截断 64 字符 / sha256）。"""

    def test_whitespace_insensitive(self) -> None:
        self.assertEqual(payload_digest("你 好\t世 界"), payload_digest("你好\n世界"))

    def test_truncate_64_chars(self) -> None:
        base = "字" * 100
        self.assertEqual(payload_digest(base), payload_digest("字" * 64 + "完全不同的尾巴"))

    def test_empty_text(self) -> None:
        self.assertEqual(payload_digest(""), payload_digest(" \n\t "))

    def test_deterministic_hex(self) -> None:
        d = payload_digest("文本")
        self.assertEqual(d, payload_digest("文本"))
        self.assertEqual(len(d), 64)  # sha256 hex
        self.assertNotEqual(d, payload_digest("别的文本"))


if __name__ == "__main__":
    unittest.main()
