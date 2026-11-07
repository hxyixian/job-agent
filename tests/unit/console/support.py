"""console 路由层测试支撑：最小 app 装配 + 离线夹具（TEST1 全 mock）。

S5a 的 ``console/app.py``（安全加固装配）并行交付中——路由层测试按合并
契约用最小 ``FastAPI()`` + ``register_content_routes`` 装配（无 S5a 中间件，
安全六项由 S5a 测试覆盖；本层专注路由行为：写回往返/校验 422/打码/
勾选绑定失效/方向流）。

AI 桩：patch ``job_agent.ai._client_ref.resolve_call_json``（service 与
greeting 共享同一模块引用，单一 patch 点覆盖结构化/方向/招呼语全部调用）。
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from job_agent.ai import _client_ref
from job_agent.ai import greeting as greeting_mod
from job_agent.config import Config, write_back
from job_agent.console.api._helpers import ContentContext
from job_agent.console.api.content import create_content_router
from job_agent.core.orchestrator import make_command_queue
from job_agent.db import Database, open_database
from job_agent.models import JobDirection, ResumeSnapshot
from job_agent.resume import parser, service

__all__ = [
    "RESUME_BODY",
    "ConsoleApiTestCase",
    "DispatchAI",
    "blank_pdf_bytes",
    "build_pdf_bytes",
    "seeded_config",
    "snapshot_v1",
    "snapshot_v2",
    "structured_result",
]

RESUME_BODY = (
    "Zhang Gong. CS bachelor since 2018. Python backend engineer at ACME. "
    "Order system microservices with FastAPI and PostgreSQL. "
    "Kubernetes deployment. High concurrency order platform rebuild. "
    "Peak QPS 8000 with availability 99.95 percent over three years."
)


def build_pdf_bytes(body: str) -> bytes:
    """单页 Helvetica 文本 PDF（pypdf 可正确提取；与 resume 域夹具同法）。"""

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
    """无文字层的"扫描件"替身（提取文本为空 → ScannedPdfError）。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class DispatchAI:
    """fake ``call_json``：按 schema 分派结果/异常；未配置的 schema 断言失败。"""

    def __init__(self) -> None:
        self.routes: dict[type, object] = {}
        self.calls: list[tuple[list[dict[str, str]], type]] = []

    def route(
        self, schema: type, result: object | None = None, error: Exception | None = None
    ) -> None:
        self.routes[schema] = error if error is not None else result

    def __call__(self, messages: list[dict[str, str]], schema: type) -> object:
        self.calls.append((messages, schema))
        if schema not in self.routes:
            raise AssertionError(f"AI 桩未配置 schema：{schema}")
        outcome = self.routes[schema]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def count(self, schema: type) -> int:
        return sum(1 for _, s in self.calls if s is schema)


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


def seeded_config() -> Config:
    """带敏感字段种子的配置（打码往返用例的初始态）。"""
    base = Config()
    return base.model_copy(
        update={
            "ai": base.ai.model_copy(update={"api_key": "sk-test1234"}),
            "notify": base.notify.model_copy(
                update={
                    "feishu": base.notify.feishu.model_copy(
                        update={
                            "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef99",
                            "secret": "topsecret99",
                        }
                    ),
                    "email": base.notify.email.model_copy(update={"password": "pwd9876"}),
                }
            ),
        }
    )


class ConsoleApiTestCase(unittest.TestCase):
    """公共环境：临时目录 + 迁移库 + 最小 FastAPI app + TestClient。

    路由层测试用最小 ``FastAPI()`` + ``create_content_router``（S5b 契约
    装配，无 S5a 中间件——安全六项由 test_hardening 覆盖，全 app 集成见
    test_console_integration）。指令队列默认装配（reload_config 断言用）。
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="job_agent_console_")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db: Database = open_database(self.dir / "t.db")
        self.addCleanup(self.db.close)  # LIFO：先关连接再删目录（Windows 文件占用）
        self.config_path = self.dir / "config.yaml"
        self.command_queue = make_command_queue()
        app = FastAPI()
        app.include_router(
            create_content_router(
                ContentContext(
                    db=self.db,
                    config_path=self.config_path,
                    command_queue=self.command_queue,
                )
            )
        )
        self.client = TestClient(app)

    # ---- 夹具操作 ----

    def install_ai(self, stub: DispatchAI) -> DispatchAI:
        """把分派桩接到 call_json 定位点（service 与 greeting 共享该模块）。"""
        patcher = patch.object(_client_ref, "resolve_call_json", lambda: stub)
        patcher.start()
        self.addCleanup(patcher.stop)
        return stub

    def upload_resume(self, snapshot: ResumeSnapshot, body: str = RESUME_BODY) -> dict:
        """便捷链：装桩 + multipart 上传（返回响应 JSON；调用方断言状态）。"""
        stub = DispatchAI()
        stub.route(parser.StructuredResume, structured_result(snapshot))
        self.install_ai(stub)
        response = self.client.post(
            "/api/resume/upload",
            files={"file": ("resume.pdf", build_pdf_bytes(body), "application/pdf")},
        )
        assert response.status_code == 200, response.text
        return response.json()

    def confirm_directions(self, *keywords: str) -> None:
        service.directions_confirm(
            self.db,
            [
                JobDirection(keywords=list(kws), rationale="测试依据", is_confirmed=True)
                for kws in keywords
            ],
        )

    def finalize_greeting(self, kind: str = "active", content: str = "主动招呼语定稿内容") -> None:
        greeting_mod.save_greeting_template(self.db, kind, content)  # type: ignore[arg-type]

    def seed_config_file(self, config: Config | None = None) -> None:
        write_back(config if config is not None else seeded_config(), self.config_path)
