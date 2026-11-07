"""resume/service.py 单元测试（Stage4 S4e；AI 全 mock [TEST1]，离线可测）。

覆盖（任务书 S4e）：
- 上传→版本链：首传建 current、重传替换（旧 current 清位 [R6-3]）、
  失败不落库（扫描件/AI 失败均零行）、非 .pdf 拒绝；
- 手动修正：新版本 + hash 承袭 + 成为事实来源、联系方式整条剔除 [B12]；
- 方向生成/确认/编辑：前置校验、AI 候选入库（is_confirmed=0）、
  已确认集不被生成触碰、prompt 契约（nonce 数据块 + 白名单简历事实）、
  AI 失败不动库、确认/编辑整体替换 + 候选清位 + 空组拒绝、
  手动入口无需先生成 [FR2.3]、关键词去空白去重；
- 换简历提示（FR2.3 s4 决策）：首传无提示；替换且存在旧简历产物
  （已确认方向/已定稿招呼语）→ regenerate_hint=True 且**不强制失效**
  （方向/招呼语行原样保留）；无产物替换无提示。
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_agent.ai import greeting as greeting_mod
from job_agent.ai import prompt as prompt_mod
from job_agent.db import open_database
from job_agent.models import JobDirection, ResumeSnapshot
from job_agent.resume import parser, service

# 固定 nonce：获得可断言的数据块标记字面值（ai/prompt 契约的测试入口）
NONCE = "0a1b2c3d"


# ---- 真实 PDF 夹具构造（与 test_parser 同法：pypdf 可正确提取的单页文本 PDF）----


def build_pdf_bytes(body: str) -> bytes:
    """构造单页 Helvetica 文本 PDF（xref 偏移按实际字节计算）。"""

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
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b" 0000000000 65535 f \n"
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
    """无文字层的"图片型/扫描件"替身（提取文本为空 → ScannedPdfError）。"""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class SchemaDispatchAI:
    """fake ``call_json``：按 schema 分派结果（结构化/方向生成共用一个桩）。

    记录全部调用 (messages, schema)；任一 schema 未配置即断言失败，
    保证桩行为显式、不静默吞掉新调用点。
    """

    def __init__(
        self,
        structured: object | None = None,
        candidates: service.DirectionCandidates | None = None,
        structured_error: Exception | None = None,
        candidates_error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[list[dict[str, str]], type]] = []
        self.structured = structured
        self.structured_error = structured_error
        self.candidates = candidates
        self.candidates_error = candidates_error

    def __call__(self, messages: list[dict[str, str]], schema: type) -> object:
        self.calls.append((messages, schema))
        if schema is parser.StructuredResume:
            if self.structured_error is not None:
                raise self.structured_error
            assert self.structured is not None, "桩未配置 StructuredResume 结果"
            return self.structured
        if schema is service.DirectionCandidates:
            if self.candidates_error is not None:
                raise self.candidates_error
            assert self.candidates is not None, "桩未配置 DirectionCandidates 结果"
            return self.candidates
        raise AssertionError(f"未预期的 AI 调用 schema：{schema}")

    @property
    def structure_calls(self) -> int:
        return sum(1 for _, s in self.calls if s is parser.StructuredResume)

    @property
    def direction_calls(self) -> int:
        return sum(1 for _, s in self.calls if s is service.DirectionCandidates)


def snapshot_v1() -> ResumeSnapshot:
    return ResumeSnapshot(
        education=["华中理工大学 计算机科学与技术 本科（2018-2022）"],
        experience=["ACME 科技 Python 后端工程师（2022-2025）：订单系统微服务开发"],
        skills=["Python", "FastAPI", "PostgreSQL"],
        projects=["高并发订单中台重构：峰值 QPS 8000"],
    )


def snapshot_v2() -> ResumeSnapshot:
    return ResumeSnapshot(
        education=["某某大学 软件工程 本科（2019-2023）"],
        experience=["某某数据 数据平台工程师：ETL 管线与数据治理"],
        skills=["Spark", "SQL", "Airflow"],
        projects=["实时数仓建设：Flink CDC 同步链路"],
    )


def structured_result(snapshot: ResumeSnapshot) -> parser.StructuredResume:
    return parser.StructuredResume(
        education=list(snapshot.education),
        experience=list(snapshot.experience),
        skills=list(snapshot.skills),
        projects=list(snapshot.projects),
    )


def candidate_result(*groups: tuple[list[str], str]) -> service.DirectionCandidates:
    return service.DirectionCandidates(
        directions=[
            service._DirectionItem(keywords=list(kws), rationale=why) for kws, why in groups
        ]
    )


class ServiceTestCase(unittest.TestCase):
    """公共环境：临时目录 + 迁移库 + AI 桩安装。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="job_agent_service_")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = open_database(self.dir / "t.db")
        self.addCleanup(self.db.close)  # LIFO：先关连接再删目录（Windows 文件占用）

    def install_ai(self, ai: SchemaDispatchAI) -> SchemaDispatchAI:
        """把分派桩接到 call_json 定位点（service 与 parser 共享同一模块）。"""
        patcher = patch.object(service._client_ref, "resolve_call_json", lambda: ai)
        patcher.start()
        self.addCleanup(patcher.stop)
        return ai

    def write_pdf(self, name: str, body: str) -> Path:
        path = self.dir / name
        path.write_bytes(build_pdf_bytes(body))
        return path

    def upload(self, body: str, snapshot: ResumeSnapshot) -> service.UploadResult:
        """便捷链：写 PDF + 安装一次性结构化桩 + 上传。"""
        pdf = self.write_pdf("resume.pdf", body)
        self.install_ai(SchemaDispatchAI(structured=structured_result(snapshot)))
        return service.upload_and_parse(self.db, pdf)


RESUME_BODY_1 = (
    "Zhang Gong. CS bachelor since 2018. Python backend engineer at ACME. "
    "Order system microservices with FastAPI and PostgreSQL. "
    "Kubernetes deployment. High concurrency order platform rebuild. "
    "Peak QPS 8000 with availability 99.95 percent over three years."
)

RESUME_BODY_2 = (
    "Li Si. Software engineering bachelor since 2019. Data platform engineer "
    "at BeeData. ETL pipelines with Spark and Airflow. SQL modeling for the "
    "core warehouse. Realtime sync with Flink CDC. Data quality governance."
)


class UploadVersionChainTest(ServiceTestCase):
    """上传 → 版本链 [FR2.1/FR2.5，R6-3 单 current]。"""

    def test_first_upload_creates_current(self) -> None:
        pdf = self.write_pdf("resume.pdf", RESUME_BODY_1)
        ai = self.install_ai(SchemaDispatchAI(structured=structured_result(snapshot_v1())))
        result = service.upload_and_parse(self.db, pdf)

        self.assertEqual(ai.structure_calls, 1)
        self.assertFalse(result.replaced_previous)
        self.assertFalse(result.regenerate_hint)
        self.assertEqual(result.version.snapshot, snapshot_v1())
        self.assertEqual(
            result.version.source_pdf_hash, hashlib.sha256(pdf.read_bytes()).hexdigest()
        )
        self.assertEqual(service.get_current_snapshot(self.db), snapshot_v1())
        self.assertEqual(service.get_current_version(self.db), result.version)

    def test_reupload_replaces_current_single_current_chain(self) -> None:
        first = self.upload(RESUME_BODY_1, snapshot_v1())
        second = self.upload(RESUME_BODY_2, snapshot_v2())

        rows = self.db.query(
            "SELECT structured_json, source_pdf_hash, is_current FROM resume_snapshots ORDER BY id"
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["is_current"] for r in rows], [0, 1])  # 旧 current 清位 [R6-3]
        self.assertNotEqual(rows[0]["source_pdf_hash"], rows[1]["source_pdf_hash"])  # 新 PDF 溯源
        self.assertTrue(second.replaced_previous)
        self.assertEqual(first.version.id + 1, second.version.id)  # 版本链递增
        self.assertEqual(service.get_current_snapshot(self.db), snapshot_v2())

    def test_upload_failure_writes_nothing(self) -> None:
        """AI 结构化失败 → 不落库（失败的上传不产生半成品版本）。"""
        pdf = self.write_pdf("resume.pdf", RESUME_BODY_1)
        ai = self.install_ai(SchemaDispatchAI(structured_error=RuntimeError("boom")))
        with self.assertRaises(parser.AIStructureError):
            service.upload_and_parse(self.db, pdf)
        self.assertEqual(ai.structure_calls, 1)
        self.assertEqual(self.db.query_one("SELECT COUNT(*) AS n FROM resume_snapshots")["n"], 0)

    def test_scanned_pdf_rejected_writes_nothing(self) -> None:
        pdf = self.dir / "scanned.pdf"
        pdf.write_bytes(blank_pdf_bytes())
        self.install_ai(SchemaDispatchAI(structured=structured_result(snapshot_v1())))
        with self.assertRaises(parser.ScannedPdfError):
            service.upload_and_parse(self.db, pdf)
        self.assertEqual(self.db.query_one("SELECT COUNT(*) AS n FROM resume_snapshots")["n"], 0)

    def test_non_pdf_extension_rejected(self) -> None:
        docx = self.dir / "resume.docx"
        docx.write_bytes(b"obviously not a pdf")
        with self.assertRaises(parser.ResumeError) as ctx:
            service.upload_and_parse(self.db, docx)
        self.assertIn("仅支持 PDF", str(ctx.exception))
        self.assertEqual(self.db.query_one("SELECT COUNT(*) AS n FROM resume_snapshots")["n"], 0)

    def test_uppercase_pdf_suffix_accepted(self) -> None:
        """后缀校验大小写不敏感（.PDF 合法）。"""
        pdf = self.dir / "resume.PDF"
        pdf.write_bytes(build_pdf_bytes(RESUME_BODY_1))
        self.install_ai(SchemaDispatchAI(structured=structured_result(snapshot_v1())))
        result = service.upload_and_parse(self.db, pdf)
        self.assertEqual(result.version.snapshot, snapshot_v1())


class ManualEditTest(ServiceTestCase):
    """手动修正读写 [FR2.5]。"""

    def test_manual_edit_creates_new_version_inheriting_hash(self) -> None:
        uploaded = self.upload(RESUME_BODY_1, snapshot_v1())
        corrected = ResumeSnapshot(
            education=["华中理工大学 计算机科学与技术 本科（修正：2018-2022）"],
            experience=list(snapshot_v1().experience),
            skills=[*list(snapshot_v1().skills), "Kubernetes"],
            projects=list(snapshot_v1().projects),
        )
        version = service.apply_manual_edit(self.db, corrected)

        self.assertEqual(version.id, uploaded.version.id + 1)  # 新版本
        self.assertEqual(version.source_pdf_hash, uploaded.version.source_pdf_hash)  # 溯源承袭
        self.assertEqual(service.get_current_snapshot(self.db), corrected)  # 事实来源切换 [FR2.4]
        rows = self.db.query("SELECT is_current FROM resume_snapshots ORDER BY id")
        self.assertEqual([r["is_current"] for r in rows], [0, 1])

    def test_manual_edit_strips_contact_entries(self) -> None:
        """B12 一致延伸：手动修正中的联系方式条目整条剔除后才入库。"""
        self.upload(RESUME_BODY_1, snapshot_v1())
        dirty = ResumeSnapshot(
            education=["计算机本科"],
            experience=["订单系统开发", "手机：13812345678"],
            skills=["Python"],
            projects=["订单中台"],
        )
        version = service.apply_manual_edit(self.db, dirty)
        self.assertEqual(version.snapshot.experience, ["订单系统开发"])
        self.assertEqual(
            self.db.query_one("SELECT structured_json FROM resume_snapshots WHERE is_current=1")[
                "structured_json"
            ].find("13812345678"),
            -1,
        )

    def test_manual_edit_without_history(self) -> None:
        """无任何历史版本：直接手写简历（hash 为 NULL）。"""
        version = service.apply_manual_edit(self.db, ResumeSnapshot(skills=["手写技能"]))
        self.assertIsNone(version.source_pdf_hash)
        self.assertEqual(service.get_current_snapshot(self.db), ResumeSnapshot(skills=["手写技能"]))

    def test_get_current_version_none_when_never_uploaded(self) -> None:
        self.assertIsNone(service.get_current_version(self.db))
        self.assertIsNone(service.get_current_snapshot(self.db))


class DirectionsGenerateTest(ServiceTestCase):
    """方向生成 [FR2.2]。"""

    def test_generate_requires_snapshot(self) -> None:
        with self.assertRaises(parser.ResumeError) as ctx:
            service.directions_generate(self.db)
        self.assertIn("尚未上传简历", str(ctx.exception))
        self.assertIn("上传", str(ctx.exception))  # 指引出路

    def test_generate_stores_candidates_confirmed_untouched(self) -> None:
        """候选入库 is_confirmed=0；已确认方向不被生成触碰（提示不强制失效）。"""
        self.upload(RESUME_BODY_1, snapshot_v1())
        confirmed = service.directions_confirm(
            self.db, [JobDirection(keywords=["Python 后端"], rationale="既有确认方向")]
        )
        self.assertEqual(confirmed[0].is_confirmed, True)

        candidates = candidate_result(
            (["Python", "后端开发"], "五年 Python 后端与微服务经验"),
            (["平台开发", "FastAPI"], "主导订单中台重构"),
        )
        ai = self.install_ai(SchemaDispatchAI(candidates=candidates))
        got = service.directions_generate(self.db)

        self.assertEqual(ai.direction_calls, 1)
        self.assertEqual(len(got), 2)
        self.assertTrue(all(not d.is_confirmed for d in got))  # 候选未确认
        self.assertEqual(got[0].keywords, ["Python", "后端开发"])
        self.assertEqual(got[1].rationale, "主导订单中台重构")
        # 库内：1 行已确认（原样）+ 2 行候选
        stored = service.get_directions(self.db)
        self.assertEqual([d.is_confirmed for d in stored], [True, False, False])
        self.assertEqual(stored[0].keywords, ["Python 后端"])
        self.assertEqual(service.confirmed_directions(self.db), confirmed)

    def test_generate_replaces_previous_candidates(self) -> None:
        """再次生成：旧候选清位、新候选入席；确认集仍不动。"""
        self.upload(RESUME_BODY_1, snapshot_v1())
        service.directions_confirm(self.db, [JobDirection(keywords=["数据开发"], rationale="确认")])
        first = candidate_result((["A1"], "依据一"))
        self.install_ai(SchemaDispatchAI(candidates=first))
        service.directions_generate(self.db)
        second = candidate_result((["B1"], "依据二"), (["B2"], "依据三"))
        self.install_ai(SchemaDispatchAI(candidates=second))
        service.directions_generate(self.db)

        stored = service.get_directions(self.db)
        self.assertEqual([d.is_confirmed for d in stored], [True, False, False])
        self.assertEqual([d.keywords for d in stored[1:]], [["B1"], ["B2"]])  # A1 已清位
        self.assertEqual(service.confirmed_directions(self.db)[0].keywords, ["数据开发"])

    def test_ai_failure_keeps_db_and_no_retry(self) -> None:
        self.upload(RESUME_BODY_1, snapshot_v1())
        service.directions_confirm(
            self.db, [JobDirection(keywords=["Python 后端"], rationale="确认")]
        )
        ai = self.install_ai(SchemaDispatchAI(candidates_error=RuntimeError("budget")))
        with self.assertRaises(service.AIDirectionError) as ctx:
            service.directions_generate(self.db)
        self.assertIn("AI 生成职位方向失败", str(ctx.exception))
        self.assertIn("手动编辑", str(ctx.exception))  # 指引 FR2.3 出路
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)
        self.assertEqual(ai.direction_calls, 1)  # 本层不重试（重试属 call_json [B13]）
        # 库内已确认集原样
        self.assertEqual(
            [d.keywords for d in service.confirmed_directions(self.db)], [["Python 后端"]]
        )

    def test_prompt_contract_with_fixed_nonce(self) -> None:
        """build_direction_messages：白名单简历事实进 nonce 数据块 + 规则约束。"""
        messages = service.build_direction_messages(snapshot_v1(), NONCE)
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        system, user = messages[0]["content"], messages[1]["content"]
        self.assertIn(prompt_mod.format_data_block_instruction(NONCE), system)
        begin, end = prompt_mod.data_block_markers(NONCE)
        self.assertIn(begin, system)
        self.assertIn(end, system)
        # 白名单四字段的简历事实在数据块内（render_resume）[B12]；
        # rindex 取**最后一个**标记对——指令文本中先出现过一次标记字面值
        facts = system[system.rindex(begin) : system.rindex(end)]
        for fact in ("Python", "FastAPI", "华中理工大学", "订单中台"):
            self.assertIn(fact, facts)
        # 规则：只依据事实 / 不含城市与联系方式 / JSON 输出
        for phrase in ("不得编造", "不含城市", "联系方式", "JSON"):
            self.assertIn(phrase, system)
        self.assertIn("directions", user)
        self.assertIn("keywords", user)

    def test_generate_sends_resume_facts_offline(self) -> None:
        """端到端（随机 nonce）：发出的 system 消息含简历事实、结构化原文不入 prompt。"""
        self.upload(RESUME_BODY_1, snapshot_v1())
        ai = self.install_ai(
            SchemaDispatchAI(candidates=candidate_result((["Python 后端"], "依据")))
        )
        service.directions_generate(self.db)
        messages, schema = ai.calls[-1]
        self.assertIs(schema, service.DirectionCandidates)
        system = messages[0]["content"]
        self.assertIn("FastAPI", system)
        # nonce 数据块标记存在（随机 nonce，按模式断言）
        self.assertRegex(system, r"<[0-9a-f]{8}:UNTRUSTED_DATA>")
        self.assertNotIn(RESUME_BODY_1, system)  # PDF 原文不进 prompt（只有结构化事实）


class DirectionsConfirmEditTest(ServiceTestCase):
    """方向确认/编辑 [FR2.2 确认/编辑/增删 + FR2.3 手动入口]。"""

    def setUp(self) -> None:
        super().setUp()
        self.upload(RESUME_BODY_1, snapshot_v1())

    def test_confirm_replaces_set_and_clears_candidates(self) -> None:
        self.install_ai(SchemaDispatchAI(candidates=candidate_result((["A"], "甲"), (["B"], "乙"))))
        service.directions_generate(self.db)
        final = service.directions_confirm(
            self.db,
            [
                JobDirection(keywords=["A", "后端"], rationale="保留并补充"),
                JobDirection(keywords=["C"], rationale="新增"),
            ],
        )
        self.assertTrue(all(d.is_confirmed for d in final))
        stored = service.get_directions(self.db)
        self.assertEqual([d.keywords for d in stored], [["A", "后端"], ["C"]])  # B 已删、候选清位
        self.assertTrue(all(d.is_confirmed for d in stored))
        self.assertEqual(service.confirmed_directions(self.db), final)

    def test_confirm_rejects_empty_keyword_group_atomically(self) -> None:
        service.directions_confirm(self.db, [JobDirection(keywords=["既有"], rationale="r")])
        with self.assertRaises(ValueError):
            service.directions_confirm(
                self.db,
                [
                    JobDirection(keywords=["合法"], rationale="r"),
                    JobDirection(keywords=["  "], rationale="空组"),
                ],
            )
        # 写库前失败：已确认集原样
        self.assertEqual([d.keywords for d in service.confirmed_directions(self.db)], [["既有"]])

    def test_confirm_empty_list_clears_all(self) -> None:
        service.directions_confirm(self.db, [JobDirection(keywords=["A"], rationale="r")])
        service.directions_confirm(self.db, [])
        self.assertEqual(service.get_directions(self.db), [])
        self.assertEqual(service.confirmed_directions(self.db), [])

    def test_edit_is_manual_entry_without_generate(self) -> None:
        """FR2.3：生成不是唯一入口——未生成候选可直接编辑已确认集。"""
        got = service.directions_edit(
            self.db,
            [JobDirection(keywords=["数据开发", "Spark"], rationale="转数据方向")],
        )
        self.assertEqual(got[0].keywords, ["数据开发", "Spark"])
        self.assertEqual(service.confirmed_directions(self.db), got)

    def test_keywords_trimmed_and_deduped(self) -> None:
        got = service.directions_edit(
            self.db,
            [JobDirection(keywords=[" Python ", "Python", " 后端 "], rationale="  去空白去重  ")],
        )
        self.assertEqual(got[0].keywords, ["Python", "后端"])  # 保序去重
        self.assertEqual(got[0].rationale, "去空白去重")

    def test_confirmed_directions_empty_before_any_confirm(self) -> None:
        self.assertEqual(service.confirmed_directions(self.db), [])


class ResumeSwitchHintTest(ServiceTestCase):
    """换简历联动提示（FR2.3 s4 决策：提示不强制失效）。"""

    def _confirm_directions(self) -> None:
        service.directions_confirm(
            self.db, [JobDirection(keywords=["Python 后端"], rationale="基于简历一")]
        )

    def test_first_upload_no_hint(self) -> None:
        result = self.upload(RESUME_BODY_1, snapshot_v1())
        self.assertFalse(result.replaced_previous)
        self.assertFalse(result.regenerate_hint)  # 首传无"重新生成"语义

    def test_reupload_without_dependents_no_hint(self) -> None:
        """替换简历但无任何旧简历产物（无方向/无招呼语）→ 无提示。"""
        self.upload(RESUME_BODY_1, snapshot_v1())
        result = self.upload(RESUME_BODY_2, snapshot_v2())
        self.assertTrue(result.replaced_previous)
        self.assertFalse(result.regenerate_hint)

    def test_reupload_with_directions_sets_hint_keeps_artifacts(self) -> None:
        """旧简历产物=已确认方向：替换 → 提示，且方向行原样（不强制失效）。"""
        self.upload(RESUME_BODY_1, snapshot_v1())
        self._confirm_directions()
        result = self.upload(RESUME_BODY_2, snapshot_v2())

        self.assertTrue(result.replaced_previous)
        self.assertTrue(result.regenerate_hint)
        directions = service.confirmed_directions(self.db)  # 方向未被清除/降级
        self.assertEqual(directions[0].keywords, ["Python 后端"])
        self.assertEqual(directions[0].is_confirmed, True)

    def test_reupload_with_greeting_sets_hint_keeps_artifacts(self) -> None:
        """旧简历产物=已定稿招呼语：替换 → 提示，模板仍是 current。"""
        self.upload(RESUME_BODY_1, snapshot_v1())
        greeting_mod.save_greeting_template(self.db, "active", "您好，我是 Python 后端工程师……")
        result = self.upload(RESUME_BODY_2, snapshot_v2())

        self.assertTrue(result.regenerate_hint)
        row = greeting_mod.current_greeting(self.db, "active")
        assert row is not None
        self.assertEqual(row["is_current"], 1)  # 招呼语未被失效 [FR2.3]
        self.assertEqual(row["content"], "您好，我是 Python 后端工程师……")

    def test_reupload_after_confirm_and_finalize_hint_true(self) -> None:
        """方向+招呼语齐备的完整链路：生成→确认→定稿→换简历→提示。"""
        self.upload(RESUME_BODY_1, snapshot_v1())
        self.install_ai(SchemaDispatchAI(candidates=candidate_result((["Python 后端"], "AI 候选"))))
        candidates = service.directions_generate(self.db)
        confirmed = service.directions_confirm(self.db, candidates)  # 确认流 [FR2.2]
        greeting_mod.save_greeting_template(self.db, "passive", "您好，感谢来询……")
        self.assertTrue(all(d.is_confirmed for d in confirmed))

        result = self.upload(RESUME_BODY_2, snapshot_v2())
        self.assertTrue(result.regenerate_hint)
        # 两类产物均原样保留（提示不强制失效）
        self.assertEqual(len(service.confirmed_directions(self.db)), 1)
        assert greeting_mod.current_greeting(self.db, "passive") is not None
        # 新简历成为事实来源
        self.assertEqual(service.get_current_snapshot(self.db), snapshot_v2())


if __name__ == "__main__":
    unittest.main()
