"""简历与方向页路由测试（S5b；§10.2「简历与方向」行）。

覆盖：上传→S4e 链（成功/非 pdf/扫描件/AI 失败不落库/换简历提示不强制失效）、
结构化行内编辑往返（FR2.5 + 联系方式剔除 [B12] + 未知字段 422）、
方向流（生成前置/AI 失败不动库/候选入库/确认与编辑整体替换/空组 422/
已确认集不被重新生成触碰）。
"""

from __future__ import annotations

import unittest

from job_agent.ai.errors import AIBudgetExhausted
from job_agent.resume import parser, service

from .support import (
    RESUME_BODY,
    ConsoleApiTestCase,
    DispatchAI,
    blank_pdf_bytes,
    build_pdf_bytes,
    snapshot_v1,
    snapshot_v2,
    structured_result,
)


class ResumeUploadApiTest(ConsoleApiTestCase):
    """POST /api/resume/upload：上传链与错误映射。"""

    def test_first_upload_ok(self) -> None:
        data = self.upload_resume(snapshot_v1())
        self.assertFalse(data["replaced_previous"])
        self.assertFalse(data["regenerate_hint"])
        self.assertNotIn("regenerate_hint_text", data)
        self.assertEqual(data["structured"]["skills"], snapshot_v1().skills)
        self.assertTrue(data["version"]["id"] > 0)
        self.assertIsNotNone(data["version"]["source_pdf_hash"])

    def test_non_pdf_rejected(self) -> None:
        response = self.client.post(
            "/api/resume/upload",
            files={"file": ("resume.docx", b"not a pdf", "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("仅支持 PDF", response.json()["detail"])

    def test_scanned_pdf_rejected_nothing_stored(self) -> None:
        stub = DispatchAI()
        stub.route(parser.StructuredResume, structured_result(snapshot_v1()))
        self.install_ai(stub)
        response = self.client.post(
            "/api/resume/upload",
            files={"file": ("resume.pdf", blank_pdf_bytes(), "application/pdf")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("图片型 PDF 不支持", response.json()["detail"])
        self.assertEqual(self.db.query_one("SELECT COUNT(*) AS n FROM resume_snapshots")["n"], 0)

    def test_ai_structure_failure_400_nothing_stored(self) -> None:
        stub = DispatchAI()
        stub.route(parser.StructuredResume, error=RuntimeError("boom"))
        self.install_ai(stub)
        response = self.client.post(
            "/api/resume/upload",
            files={"file": ("resume.pdf", build_pdf_bytes(RESUME_BODY), "application/pdf")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("AI", response.json()["detail"])
        self.assertEqual(self.db.query_one("SELECT COUNT(*) AS n FROM resume_snapshots")["n"], 0)

    def test_replacement_sets_regenerate_hint_without_invalidation(self) -> None:
        """换简历提示 [FR2.3]：存在已确认方向 + 定稿招呼语 → hint=True 且产物原样保留。"""
        first = self.upload_resume(snapshot_v1())
        self.confirm_directions(["Python", "后端"])
        self.finalize_greeting("active", "第一版主动招呼语")
        data = self.upload_resume(snapshot_v2())

        self.assertTrue(data["replaced_previous"])
        self.assertTrue(data["regenerate_hint"])
        self.assertIn("建议重新生成", data["regenerate_hint_text"])
        # 提示不强制失效：方向/招呼语行不被删除或修改
        self.assertEqual(len(service.confirmed_directions(self.db)), 1)
        self.assertIsNotNone(service.get_current_version(self.db))
        row = self.db.query_one(
            "SELECT content FROM greeting_templates WHERE kind='active' AND is_current=1"
        )
        self.assertEqual(row["content"], "第一版主动招呼语")
        self.assertGreater(data["version"]["id"], first["version"]["id"])


class StructuredEditApiTest(ConsoleApiTestCase):
    """GET/PUT /api/resume/structured：行内编辑往返 [FR2.5]。"""

    def test_get_before_upload_is_null(self) -> None:
        response = self.client.get("/api/resume/structured")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["version"])
        self.assertIsNone(response.json()["structured"])

    def test_put_creates_new_version_and_strips_contacts(self) -> None:
        first = self.upload_resume(snapshot_v1())
        response = self.client.put(
            "/api/resume/structured",
            json={
                "education": ["某某大学 软件工程 本科"],
                "experience": ["电话：138-1234-5678", "后端工程师：订单系统开发"],
                "skills": ["Python", "邮箱：a@b.com"],
                "projects": ["数据平台建设"],
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreater(data["version"]["id"], first["version"]["id"])
        # 联系方式条目整条剔除 [B12]；剔除结果即回显结果
        self.assertEqual(data["structured"]["experience"], ["后端工程师：订单系统开发"])
        self.assertEqual(data["structured"]["skills"], ["Python"])
        # 新版本成为事实来源
        follow = self.client.get("/api/resume/structured").json()
        self.assertEqual(follow["version"]["id"], data["version"]["id"])

    def test_put_unknown_field_422(self) -> None:
        self.upload_resume(snapshot_v1())
        response = self.client.put(
            "/api/resume/structured", json={"education": ["x"], "unknown": 1}
        )
        self.assertEqual(response.status_code, 422)


class DirectionsApiTest(ConsoleApiTestCase):
    """GET/POST/PUT /api/directions：方向流 [FR2.2/FR2.3]。"""

    def test_generate_requires_resume(self) -> None:
        response = self.client.post("/api/directions/generate")
        self.assertEqual(response.status_code, 400)
        self.assertIn("尚未上传简历", response.json()["detail"])

    def test_generate_ai_failure_400_keeps_rows(self) -> None:
        self.upload_resume(snapshot_v1())
        self.confirm_directions(["Python"])  # 既有已确认集
        stub = DispatchAI()
        stub.route(service.DirectionCandidates, error=AIBudgetExhausted("预算耗尽"))
        self.install_ai(stub)
        response = self.client.post("/api/directions/generate")
        self.assertEqual(response.status_code, 400)
        self.assertIn("AI 生成职位方向失败", response.json()["detail"])
        # AI 失败不改库：已确认集原样
        self.assertEqual(len(service.confirmed_directions(self.db)), 1)

    def test_generate_stores_candidates_confirmed_untouched(self) -> None:
        self.upload_resume(snapshot_v1())
        self.confirm_directions(["已确认", "方向"])
        stub = DispatchAI()
        stub.route(
            service.DirectionCandidates,
            service.DirectionCandidates(
                directions=[
                    service._DirectionItem(keywords=["Python", "后端"], rationale="依据A"),
                    service._DirectionItem(keywords=["FastAPI"], rationale="依据B"),
                ]
            ),
        )
        self.install_ai(stub)
        response = self.client.post("/api/directions/generate")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["directions"]), 2)
        self.assertTrue(all(not d["is_confirmed"] for d in body["directions"]))

        listing = self.client.get("/api/directions").json()["directions"]
        self.assertEqual(len(listing), 3)  # 2 候选 + 1 已确认
        confirmed = [d for d in listing if d["is_confirmed"]]
        self.assertEqual(confirmed[0]["keywords"], ["已确认", "方向"])

    def test_put_confirm_replaces_and_clears_candidates(self) -> None:
        self.upload_resume(snapshot_v1())
        stub = DispatchAI()
        stub.route(
            service.DirectionCandidates,
            service.DirectionCandidates(
                directions=[service._DirectionItem(keywords=["AI"], rationale="r")]
            ),
        )
        self.install_ai(stub)
        self.client.post("/api/directions/generate")

        response = self.client.put(
            "/api/directions",
            json={
                "source": "confirm",
                "directions": [
                    {"keywords": ["Python", " 后端 ", "Python"], "rationale": " edited "},
                    {"keywords": ["数据"], "rationale": "r2"},
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        confirmed = response.json()["directions"]
        self.assertEqual(confirmed[0]["keywords"], ["Python", "后端"])  # 去空白 + 保序去重
        self.assertEqual(confirmed[0]["rationale"], "edited")

        listing = self.client.get("/api/directions").json()["directions"]
        self.assertEqual(len(listing), 2)
        self.assertTrue(all(d["is_confirmed"] for d in listing))  # 候选清位

    def test_put_edit_path_same_replacement_semantics(self) -> None:
        """source=edit（手动编辑入口 [FR2.3]）：无需先生成，直接整体替换。"""
        self.upload_resume(snapshot_v1())
        response = self.client.put(
            "/api/directions",
            json={"source": "edit", "directions": [{"keywords": ["手动方向"], "rationale": "r"}]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([d["keywords"] for d in response.json()["directions"]], [["手动方向"]])

    def test_put_empty_group_422(self) -> None:
        self.upload_resume(snapshot_v1())
        response = self.client.put(
            "/api/directions",
            json={"source": "confirm", "directions": [{"keywords": [], "rationale": "r"}]},
        )
        self.assertEqual(response.status_code, 422)

    def test_put_whitespace_only_group_422(self) -> None:
        """空白关键词组过 Pydantic 后由服务层语义校验拦下（422 文案可读）。"""
        self.upload_resume(snapshot_v1())
        response = self.client.put(
            "/api/directions",
            json={"source": "confirm", "directions": [{"keywords": ["  "], "rationale": "r"}]},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("至少需要一个搜索关键词", response.json()["detail"])

    def test_put_allows_empty_list_to_clear(self) -> None:
        self.upload_resume(snapshot_v1())
        self.confirm_directions(["旧方向"])
        response = self.client.put("/api/directions", json={"source": "confirm", "directions": []})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["directions"], [])
        self.assertEqual(service.confirmed_directions(self.db), [])


if __name__ == "__main__":
    unittest.main()
