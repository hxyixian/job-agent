"""resume/parser.py 单元测试（Stage3 S3c；AI 全 mock [TEST1]，离线可测）。

覆盖（任务书 S3c）：
- 扫描件报错：图片型/空白 PDF → ScannedPdfError（文案含 §9.5 权威短语），
  200 字符边界（199 抛 / 200 过）[B15]；
- 结构化夹具：AI mock 的 prompt 契约（nonce 数据块 + 联系方式禁令 [B12]）
  与四字段映射、AI 失败 → AIStructureError（不重试，单次调用）；
- 联系方式剔除：输入侧 redact_contact_lines / 输出侧 strip_contact_entries
  （保守整行/整条剔除，不误杀"微信公众号运营"类正常条目）[B12]；
- 版本链 [FR2.5/R6-3]：save_resume_snapshot 单 current 链、手动修正生成
  新版本（承袭 source_pdf_hash）、uq_resume_current 库层兜底、
  current_resume_snapshot round-trip；
- 离线全流程：parse（真实 pypdf 提取）→ structure（mock）→ 入库 → 读回。
"""

from __future__ import annotations

import hashlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_agent.ai import prompt as prompt_mod
from job_agent.db import open_database
from job_agent.models import ResumeSnapshot, ResumeText
from job_agent.resume import parser

# 固定 nonce：获得可断言的数据块标记字面值（ai/prompt 契约的测试入口）
NONCE = "0a1b2c3d"


# ---- 真实 PDF 夹具构造（pypdf 可正确提取的最小单页文本 PDF）----


def build_pdf_bytes(body: str) -> bytes:
    """构造单页 Helvetica 文本 PDF（xref 偏移按实际字节计算，pypdf 可直接解析）。"""

    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    stream = f"BT /F1 12 Tf 72 720 Td ({esc(body)}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_pos).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


def blank_pdf_bytes() -> bytes:
    """无文字层的"图片型/扫描件"替身：pypdf 空白页（提取文本为空）。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


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


def structured_result() -> parser.StructuredResume:
    return parser.StructuredResume(
        education=["华中理工大学 计算机科学与技术 本科（2018-2022）"],
        experience=["ACME 科技 Python 后端工程师（2022-2025）：负责订单系统微服务开发"],
        skills=["Python", "FastAPI", "PostgreSQL"],
        projects=["高并发订单中台重构：峰值 QPS 8000"],
    )


class ParseResumeTest(unittest.TestCase):
    """parse_resume：pypdf 提取 + 扫描件判定 [B15]。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="job_agent_resume_")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write(self, name: str, data: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_text_pdf_extracted_with_hash(self) -> None:
        body = "Zhang San. Bachelor of CS. Python backend engineer with FastAPI. " * 4
        path = self._write("ok.pdf", build_pdf_bytes(body))
        got = parser.parse_resume(path)
        self.assertIn(body.strip(), got.text)  # pypdf 原样提取
        self.assertGreaterEqual(len(got.text), parser.MIN_TEXT_CHARS)
        self.assertEqual(got.source_pdf_hash, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_scanned_pdf_rejected_with_clear_message(self) -> None:
        path = self._write("scanned.pdf", blank_pdf_bytes())
        with self.assertRaises(parser.ScannedPdfError) as ctx:
            parser.parse_resume(path)
        message = str(ctx.exception)
        self.assertIn("图片型 PDF 不支持，请提供文字版", message)  # §9.5 权威文案
        self.assertIn("200", message)  # 阈值可见
        self.assertIn("0", message)  # 实际提取字符数可见

    def test_boundary_200_ok_199_rejected(self) -> None:
        ok = self._write("b200.pdf", build_pdf_bytes("A" * 200))
        got = parser.parse_resume(ok)
        self.assertEqual(len(got.text), 200)
        bad = self._write("b199.pdf", build_pdf_bytes("B" * 199))
        with self.assertRaises(parser.ScannedPdfError):
            parser.parse_resume(bad)

    def test_missing_file(self) -> None:
        with self.assertRaises(parser.ResumeError) as ctx:
            parser.parse_resume(self.dir / "nope.pdf")
        self.assertIn("不存在", str(ctx.exception))

    def test_garbage_file(self) -> None:
        path = self._write("bad.pdf", b"this is not a pdf at all")
        with self.assertRaises(parser.ResumeError) as ctx:
            parser.parse_resume(path)
        self.assertIn("读取失败", str(ctx.exception))


class ContactStrippingTest(unittest.TestCase):
    """联系方式剔除 [B12]：输入侧整行 / 输出侧整条（保守，宁多勿漏）。"""

    def test_redact_contact_lines(self) -> None:
        text = "\n".join(
            [
                "张工 / 计算机科学与技术 本科",
                "手机：13812345678",
                "Email: zhangsan@example.com",
                "微信号：resume_wx123",
                "QQ：123456789",
                "电话：010-12345678",
                "Python 后端工程师，负责订单系统开发",
            ]
        )
        redacted = parser.redact_contact_lines(text)
        leaked_any = (
            "13812345678",
            "zhangsan@example.com",
            "resume_wx123",
            "123456789",
            "010-12345678",
        )
        for leaked in leaked_any:
            self.assertNotIn(leaked, redacted)
        self.assertIn("计算机科学与技术", redacted)
        self.assertIn("订单系统开发", redacted)

    def test_strip_contact_entries(self) -> None:
        entries = [
            "华中理工大学 计算机本科",
            "电话：13812345678",
            "邮箱：zhangsan@example.com",
            "微信号：resume_wx123",
            "QQ：123456789",
            "  ",  # 空白条目一并过滤
            "负责微信公众号运营",  # 不误杀：无账号模式
            "熟悉 wxpython 桌面开发",  # 不误杀：wx 简写不参与值匹配
        ]
        kept = parser.strip_contact_entries(entries)
        self.assertEqual(
            kept, ["华中理工大学 计算机本科", "负责微信公众号运营", "熟悉 wxpython 桌面开发"]
        )

    def test_inline_contact_in_entry_dropped(self) -> None:
        """条目内嵌联系方式（非独立字段行）同样整条剔除——保守优先。"""
        kept = parser.strip_contact_entries(["主导客服系统重构（联系电话 138 1234 5678）"])
        self.assertEqual(kept, [])


class StructureResumeTest(unittest.TestCase):
    """structure_resume：AI mock 的 prompt 契约 + 双侧剔除 + 失败包装。"""

    def _text(self) -> ResumeText:
        body = "\n".join(
            [
                "张工，计算机科学与技术本科，五年 Python 后端经验。",
                "手机：13812345678",
                "ACME 科技：负责订单系统微服务开发与运维。",
                "Email: zhangsan@example.com",
                "技能：Python、FastAPI、PostgreSQL、Kubernetes。",
                "项目：高并发订单中台重构，峰值 QPS 8000。",
            ]
        )
        return ResumeText(text=body, source_pdf_hash="a" * 64)

    def test_happy_path_maps_fields(self) -> None:
        fake = RecordingAI(result=structured_result())
        with patch.object(parser._client_ref, "resolve_call_json", lambda: fake):
            got = parser.structure_resume(self._text())
        self.assertEqual(got.education, ["华中理工大学 计算机科学与技术 本科（2018-2022）"])
        self.assertEqual(got.skills, ["Python", "FastAPI", "PostgreSQL"])
        self.assertEqual(got.projects, ["高并发订单中台重构：峰值 QPS 8000"])
        self.assertEqual(len(fake.calls), 1)

    def test_prompt_contract_and_input_redaction(self) -> None:
        """build_structure_messages（固定 nonce）：数据块包裹 + 联系方式禁令 + 输入侧剔除。"""
        messages = parser.build_structure_messages(self._text(), NONCE)
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        system, user = messages[0]["content"], messages[1]["content"]
        # 指令侧：四字段说明 + 联系方式禁令 + 数据块规则说明 [B12]
        for phrase in ("education", "experience", "skills", "projects", "联系方式", "不得编造"):
            self.assertIn(phrase, system)
        self.assertIn(prompt_mod.format_data_block_instruction(NONCE), system)
        # 简历全文以 nonce 数据块包裹（已剔除联系方式行）
        begin, end = prompt_mod.data_block_markers(NONCE)
        self.assertIn(begin, user)
        self.assertIn(end, user)
        for leaked in ("13812345678", "zhangsan@example.com"):
            self.assertNotIn(leaked, user)  # §9.5：给 AI 的副本去除联系方式
        self.assertIn("订单系统微服务开发", user)  # 正文保留

    def test_structure_resume_sends_redacted_copy(self) -> None:
        """端到端（随机 nonce）：结构化调用发出的 user 消息不含联系方式原文。"""
        fake = RecordingAI(result=structured_result())
        with patch.object(parser._client_ref, "resolve_call_json", lambda: fake):
            parser.structure_resume(self._text())
        messages, schema = fake.calls[0]
        self.assertIs(schema, parser.StructuredResume)
        user = messages[1]["content"]
        self.assertNotIn("13812345678", user)
        self.assertNotIn("zhangsan@example.com", user)
        self.assertIn("订单系统微服务开发", user)

    def test_output_side_stripping(self) -> None:
        """AI 违规夹带联系方式 → 输出侧规则整条剔除（确定性防御，不依赖 AI 自觉）。"""
        dirty = parser.StructuredResume(
            education=["计算机本科", "邮箱：zhangsan@example.com"],
            experience=["订单系统开发", "手机：13812345678"],
            skills=["Python", "微信号：resume_wx123"],
            projects=["订单中台"],
        )
        with patch.object(parser._client_ref, "resolve_call_json", lambda: RecordingAI(dirty)):
            got = parser.structure_resume(self._text())
        self.assertEqual(got.education, ["计算机本科"])
        self.assertEqual(got.experience, ["订单系统开发"])
        self.assertEqual(got.skills, ["Python"])
        self.assertEqual(got.projects, ["订单中台"])

    def test_ai_failure_wrapped_no_retry(self) -> None:
        fake = RecordingAI(error=RuntimeError("AIUnparseableError"))
        with (
            patch.object(parser._client_ref, "resolve_call_json", lambda: fake),
            self.assertRaises(parser.AIStructureError) as ctx,
        ):
            parser.structure_resume(self._text())
        self.assertIn("AI 简历结构化失败", str(ctx.exception))
        self.assertIn("手动修正", str(ctx.exception))  # 指引 FR2.5 出路
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)
        self.assertEqual(len(fake.calls), 1)  # 本层不重试（重试属 call_json [B13]）

    def test_error_hierarchy(self) -> None:
        self.assertTrue(issubclass(parser.ScannedPdfError, parser.ResumeError))
        self.assertTrue(issubclass(parser.AIStructureError, parser.ResumeError))


class SnapshotVersioningTest(unittest.TestCase):
    """版本化入库 [FR2.5/R6-3]：单 current 链 + 手动修正新版本 + 溯源承袭。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="job_agent_snapshot_")
        self.addCleanup(self._tmp.cleanup)
        self.db = open_database(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.db.close)  # LIFO：先关连接再删目录（Windows 文件占用）

    def test_save_chain_single_current(self) -> None:
        text1 = ResumeText(text="x" * 250, source_pdf_hash="a" * 64)
        text2 = ResumeText(text="y" * 250, source_pdf_hash="b" * 64)
        parser.save_resume_snapshot(self.db, text1, ResumeSnapshot(skills=["v1 技能"]))
        parser.save_resume_snapshot(self.db, text2, ResumeSnapshot(skills=["v2 技能"]))
        rows = self.db.query("SELECT source_pdf_hash, is_current FROM resume_snapshots ORDER BY id")
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["is_current"] for r in rows], [0, 1])  # 旧 current 已清
        self.assertEqual([r["source_pdf_hash"] for r in rows], ["a" * 64, "b" * 64])
        self.assertEqual(
            parser.current_resume_snapshot(self.db), ResumeSnapshot(skills=["v2 技能"])
        )

    def test_multi_current_rejected_by_db(self) -> None:
        """R6-3 库层兜底：uq_resume_current 拒绝第二个 current（应用层 bug 也不破约）。"""
        parser.save_resume_snapshot(
            self.db, ResumeText(text="x" * 250, source_pdf_hash=None), ResumeSnapshot()
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO resume_snapshots (structured_json, is_current) VALUES ('{}', 1)"
            )

    def test_manual_correction_creates_new_version_inheriting_hash(self) -> None:
        text = ResumeText(text="z" * 250, source_pdf_hash="c" * 64)
        parser.save_resume_snapshot(self.db, text, ResumeSnapshot(skills=["AI 原稿"]))
        corrected = ResumeSnapshot(skills=["人工修正后技能"], education=["本科（修正）"])
        parser.save_manual_correction(self.db, corrected)
        rows = self.db.query(
            "SELECT structured_json, source_pdf_hash, is_current FROM resume_snapshots ORDER BY id"
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["is_current"] for r in rows], [0, 1])  # 修正是新版本（新 current）
        self.assertEqual(rows[1]["source_pdf_hash"], "c" * 64)  # 溯源承袭
        self.assertEqual(parser.snapshot_from_json(rows[1]["structured_json"]), corrected)

    def test_manual_correction_without_history(self) -> None:
        parser.save_manual_correction(self.db, ResumeSnapshot(skills=["手写"]))
        row = self.db.query_one(
            "SELECT source_pdf_hash, is_current FROM resume_snapshots WHERE is_current=1"
        )
        self.assertEqual(row["is_current"], 1)
        self.assertIsNone(row["source_pdf_hash"])

    def test_current_snapshot_none_when_never_parsed(self) -> None:
        self.assertIsNone(parser.current_resume_snapshot(self.db))

    def test_json_round_trip(self) -> None:
        snapshot = ResumeSnapshot(
            education=["本科"], experience=["后端工程师"], skills=["Python"], projects=["订单中台"]
        )
        self.assertEqual(parser.snapshot_from_json(parser.snapshot_to_json(snapshot)), snapshot)


class OfflinePipelineTest(unittest.TestCase):
    """离线全流程：真实 pypdf 提取 → mock AI 结构化 → 版本化入库 → 读回。"""

    def test_parse_structure_save_load(self) -> None:
        with tempfile.TemporaryDirectory(prefix="job_agent_pipeline_") as tmp:
            pdf = Path(tmp) / "resume.pdf"
            body = (
                "Zhang Gong. CS bachelor since 2018. Python backend engineer at ACME. "
                "Order system microservices with FastAPI and PostgreSQL. "
                "Kubernetes deployment. High concurrency order platform rebuild. "
                "Peak QPS 8000 with availability 99.95 percent over three years."
            )
            assert len(body) >= parser.MIN_TEXT_CHARS
            pdf.write_bytes(build_pdf_bytes(body))

            parsed = parser.parse_resume(pdf)
            fake = RecordingAI(result=structured_result())
            with patch.object(parser._client_ref, "resolve_call_json", lambda: fake):
                snapshot = parser.structure_resume(parsed)

            db = open_database(Path(tmp) / "t.db")
            try:
                parser.save_resume_snapshot(db, parsed, snapshot)
                self.assertEqual(parser.current_resume_snapshot(db), snapshot)
                row = db.query_one(
                    "SELECT source_pdf_hash FROM resume_snapshots WHERE is_current=1"
                )
                self.assertEqual(row["source_pdf_hash"], parsed.source_pdf_hash)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
