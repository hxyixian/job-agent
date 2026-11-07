"""ai/greeting.py 单元测试（Stage3 S3c；AI 全 mock [TEST1]，离线可测）。

覆盖：
- generate_greeting_candidates（§5.5 I4 / §9.3）：恒 3 条、AI 失败上抛、
  空白剥离、数量防御、非法 kind；
- build_greeting_messages 的 prompt 契约：行为红线 + 结构化简历事实
  （render_resume 白名单渲染）+ 求职方向，数据块以 nonce 标记包裹 [B12]，
  主动/被动任务区分（含 R12 机制说明）；
- GreetingCandidates schema：候选数 != 3 / 空白条目被 Pydantic 拒绝
  （恒 3 条在 schema 层约束，由 call_json 的校验重试统一兜底 [B13]）；
- greeting_templates 写库路径（R6-3/F1）：版本链（每 kind 单 current、
  version 递增）、platform_synced_hash/at 列处理、重新定稿使同步确认
  失效 [FR5.3]。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from job_agent.ai import greeting
from job_agent.ai import prompt as prompt_mod
from job_agent.ai.greeting import (
    GreetingCandidates,
    build_greeting_messages,
    confirm_platform_synced,
    current_greeting,
    generate_greeting_candidates,
    greeting_content_hash,
    is_platform_synced,
    save_greeting_template,
)
from job_agent.db import open_database
from job_agent.models import JobDirection, ResumeSnapshot

# 固定 nonce：获得可断言的数据块标记字面值（ai/prompt 契约的测试入口）
NONCE = "0a1b2c3d"


def make_resume() -> ResumeSnapshot:
    """结构化简历夹具（含可断言的事实锚点）。"""
    return ResumeSnapshot(
        education=["华中理工大学 计算机科学与技术 本科（2018-2022）"],
        experience=["ACME 科技 Python 后端工程师（2022-2025）：负责订单系统微服务开发与运维"],
        skills=["Python", "FastAPI", "PostgreSQL", "Kubernetes"],
        projects=["高并发订单中台重构：峰值 QPS 8000，可用性 99.95%"],
    )


def make_directions() -> list[JobDirection]:
    return [
        JobDirection(
            keywords=["Python", "后端开发"], rationale="与简历经历最匹配", is_confirmed=True
        )
    ]


class RecordingAI:
    """fake ``call_json``：记录 (messages, schema)，返回预设结果或抛预设异常。"""

    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[list[dict[str, str]], type]] = []
        self.result = result
        self.error = error

    def __call__(self, messages: list[dict[str, str]], schema: type) -> object:
        self.calls.append((messages, schema))
        if self.error is not None:
            raise self.error
        return self.result


def ok_result() -> GreetingCandidates:
    return GreetingCandidates(candidates=["候选一", "候选二", "候选三"])


class GenerateCandidatesTest(unittest.TestCase):
    """generate_greeting_candidates（I4 契约）。"""

    def test_active_returns_exactly_three_stripped(self) -> None:
        fake = RecordingAI(
            result=GreetingCandidates(candidates=["  您好，我是张工  ", " 候选二", "候选三 "])
        )
        with patch.object(greeting._client_ref, "resolve_call_json", lambda: fake):
            got = generate_greeting_candidates("active", make_resume(), make_directions())
        self.assertEqual(got, ["您好，我是张工", "候选二", "候选三"])  # 恒 3 条 + 去空白
        self.assertEqual(len(fake.calls), 1)

    def test_ai_failure_propagates(self) -> None:
        """AI 层失败原样上抛（AIUnparseableError/AIBudgetExhausted 等），本层不吞。"""
        fake = RecordingAI(error=RuntimeError("AIUnparseableError"))
        with (
            patch.object(greeting._client_ref, "resolve_call_json", lambda: fake),
            self.assertRaises(RuntimeError),
        ):
            generate_greeting_candidates("active", make_resume(), make_directions())

    def test_defensive_wrong_count(self) -> None:
        """防御契约漂移（绕过 schema 返回错误数量）→ 明确报错，不返回劣质结果。"""
        bypassed = GreetingCandidates.model_construct(candidates=["a", "b"])
        with (
            patch.object(greeting._client_ref, "resolve_call_json", lambda: RecordingAI(bypassed)),
            self.assertRaises(ValueError),
        ):
            generate_greeting_candidates("active", make_resume(), make_directions())

    def test_invalid_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_greeting_messages("greet", make_resume(), make_directions())  # type: ignore[arg-type]


class GreetingMessagesTest(unittest.TestCase):
    """build_greeting_messages：§9.3 prompt 契约（固定 nonce 可断言）。"""

    def test_active_prompt_contract(self) -> None:
        messages = build_greeting_messages("active", make_resume(), make_directions(), NONCE)
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        system, user = messages[0]["content"], messages[1]["content"]
        # §9.3 行为红线 [FR6.2/NFR1]
        for phrase in ("不得编造", "薪资", "联系方式", "具体日期", "红线"):
            self.assertIn(phrase, system, f"system prompt 缺少红线关键词：{phrase}")
        # 数据块规则说明（nonce 标记字面值注入）
        self.assertIn(prompt_mod.format_data_block_instruction(NONCE), system)
        # 简历事实：render_resume 白名单渲染 + nonce 数据块包裹 [B12]
        begin, end = prompt_mod.data_block_markers(NONCE)
        self.assertIn(begin, system)
        self.assertIn(end, system)
        self.assertIn("【教育经历】", system)
        self.assertIn("华中理工大学", system)
        self.assertIn("ACME 科技", system)
        self.assertIn("高并发订单中台重构", system)
        # 求职方向数据块
        self.assertIn("后端开发", system)
        self.assertIn("与简历经历最匹配", system)
        # kind=active 任务说明（R12：定稿由用户粘贴到平台设置，平台自动发送）
        self.assertIn("主动招呼语", user)
        self.assertIn("立即沟通", user)
        self.assertIn("打招呼语", user)
        self.assertNotIn("被动招呼语", user)
        self.assertIn("恰好 3 条", user)

    def test_passive_task_differs(self) -> None:
        messages = build_greeting_messages("passive", make_resume(), make_directions(), NONCE)
        user = messages[1]["content"]
        self.assertIn("被动招呼语", user)
        self.assertIn("HR 主动来询", user)
        self.assertIn("直接发送", user)  # 被动招呼语由软件发送（R12）
        self.assertNotIn("主动招呼语", user)
        # system 契约与 kind 无关（红线/简历事实/方向固定包含）
        system = messages[0]["content"]
        self.assertIn("不得编造", system)
        self.assertIn(prompt_mod.data_block_markers(NONCE)[0], system)

    def test_empty_directions_and_resume(self) -> None:
        messages = build_greeting_messages("active", ResumeSnapshot(), [], NONCE)
        system = messages[0]["content"]
        self.assertIn("尚未确认职位方向", system)
        self.assertIn("简历事实为空", system)


class CandidatesSchemaTest(unittest.TestCase):
    """恒 3 条在 schema 层约束（call_json 校验失败的统一重试路径 [B13]）。"""

    def test_rejects_two(self) -> None:
        with self.assertRaises(ValidationError):
            GreetingCandidates(candidates=["一", "二"])

    def test_rejects_four(self) -> None:
        with self.assertRaises(ValidationError):
            GreetingCandidates(candidates=["一", "二", "三", "四"])

    def test_rejects_empty_item(self) -> None:
        with self.assertRaises(ValidationError):
            GreetingCandidates(candidates=["一", "  ", "三"])

    def test_accepts_exactly_three(self) -> None:
        result = GreetingCandidates(candidates=["一", "二", "三"])
        self.assertEqual(len(result.candidates), 3)


class GreetingTemplateStoreTest(unittest.TestCase):
    """greeting_templates 写库路径（R6-3 单 current + F1 同步确认列）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="job_agent_greeting_")
        self.addCleanup(self._tmp.cleanup)
        self.db = open_database(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.db.close)  # LIFO：先关连接再删目录（Windows 文件占用）

    def test_version_chain_single_current_per_kind(self) -> None:
        id1 = save_greeting_template(self.db, "active", "第一版内容")
        id2 = save_greeting_template(self.db, "active", "第二版内容")
        self.assertGreater(id2, id1)
        rows = self.db.query(
            "SELECT id, content, version, is_current, platform_synced_hash,"
            " platform_synced_at FROM greeting_templates WHERE kind='active' ORDER BY id"
        )
        self.assertEqual([r["version"] for r in rows], [1, 2])  # version 递增
        self.assertEqual([r["is_current"] for r in rows], [0, 1])  # 旧 current 已清
        # 新 current 未同步（R6-3 两列为 NULL）
        self.assertIsNone(rows[1]["platform_synced_hash"])
        self.assertIsNone(rows[1]["platform_synced_at"])

    def test_two_kinds_hold_independent_currents(self) -> None:
        """单 current 约束按 kind 分别生效：主动/被动可同时各有 current。"""
        save_greeting_template(self.db, "active", "主动模板")
        save_greeting_template(self.db, "passive", "被动模板")
        total = self.db.query_one("SELECT COUNT(*) AS n FROM greeting_templates WHERE is_current=1")
        self.assertEqual(total["n"], 2)
        self.assertEqual(current_greeting(self.db, "active")["content"], "主动模板")
        self.assertEqual(current_greeting(self.db, "passive")["content"], "被动模板")

    def test_platform_sync_lifecycle(self) -> None:
        """F1 [FR5.3/R6-3]：确认=内容哈希绑定；重新定稿（新版本行）→ 确认失效。"""
        tid1 = save_greeting_template(self.db, "active", "定稿内容")
        self.assertIsNone(current_greeting(self.db, "active")["platform_synced_hash"])
        confirm_platform_synced(self.db, tid1)
        row = current_greeting(self.db, "active")
        self.assertEqual(row["platform_synced_hash"], greeting_content_hash("定稿内容"))
        self.assertIsNotNone(row["platform_synced_at"])
        self.assertTrue(is_platform_synced(row["content"], row["platform_synced_hash"]))

        # 重新定稿：新 current 行两列为 NULL → 同步确认自动失效
        tid2 = save_greeting_template(self.db, "active", "修改后内容")
        row2 = current_greeting(self.db, "active")
        self.assertEqual(row2["id"], tid2)
        self.assertIsNone(row2["platform_synced_hash"])
        self.assertFalse(is_platform_synced(row2["content"], row2["platform_synced_hash"]))
        # 版本链不删历史：旧行保留确认痕迹
        old = self.db.query_one(
            "SELECT platform_synced_hash FROM greeting_templates WHERE id=?", (tid1,)
        )
        self.assertEqual(old["platform_synced_hash"], greeting_content_hash("定稿内容"))

        # 对新版本重新确认后恢复有效
        confirm_platform_synced(self.db, tid2)
        self.assertTrue(
            is_platform_synced(
                "修改后内容", current_greeting(self.db, "active")["platform_synced_hash"]
            )
        )

    def test_is_platform_synced_pure_rules(self) -> None:
        content = "模板正文"
        self.assertFalse(is_platform_synced(content, None))
        self.assertFalse(is_platform_synced(content, greeting_content_hash("其它内容")))
        self.assertTrue(is_platform_synced(content, greeting_content_hash(content)))
        # 内容修改（哈希随之变化）→ 旧确认失效
        self.assertFalse(is_platform_synced("模板正文2", greeting_content_hash(content)))

    def test_invalid_inputs_rejected(self) -> None:
        with self.assertRaises(ValueError):
            save_greeting_template(self.db, "greet", "内容")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            save_greeting_template(self.db, "active", "   ")
        with self.assertRaises(ValueError):
            confirm_platform_synced(self.db, 9999)
        with self.assertRaises(ValueError):
            current_greeting(self.db, "greet")  # type: ignore[arg-type]

    def test_current_greeting_none_when_empty(self) -> None:
        self.assertIsNone(current_greeting(self.db, "active"))
        self.assertIsNone(current_greeting(self.db, "passive"))


if __name__ == "__main__":
    unittest.main()
