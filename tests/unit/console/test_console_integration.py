"""S5b × S5a 装配集成测试：S5b 五页路由经 ``console.app.create_app`` 挂载。

验证合并契约（S5a 并行交付后的真实装配形态）：

- ``create_app(db=...)`` 自动注册 S5b 路由（api/content.create_content_router）；
- 六项加固对 S5b 路由同样生效：非 GET 缺 ``X-Job-Agent`` 头 403、GET 放行；
- 异常统一 JSON 信封（``{"error": {"code", "message"}}``）覆盖 S5b 的
  400/422 映射（common.js errText 按此信封取文案）；
- 配置保存后经指令队列投递 ``reload_config``（§5.5 I6）；
- 响应打码中间件与 S5b 源头打码两层叠加结果稳定（幂等）。
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from job_agent.config import load_config
from job_agent.console import create_app
from job_agent.core.orchestrator import make_command_queue
from job_agent.db import Database, open_database
from job_agent.resume import parser

from .support import (
    RESUME_BODY,
    DispatchAI,
    build_pdf_bytes,
    seeded_config,
    snapshot_v1,
    structured_result,
)


class ConsoleIntegrationTest(unittest.TestCase):
    """全 app（S5a 加固 + S5b 路由）冒烟。"""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        tmp = tempfile.TemporaryDirectory(prefix="job_agent_console_int_")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db: Database = open_database(self.dir / "int.db")
        self.addCleanup(self.db.close)
        self.config_path = self.dir / "config.yaml"
        self.command_queue = make_command_queue()
        self.app = create_app(
            db=self.db,
            command_queue=self.command_queue,
            config_loader=lambda: load_config(self.config_path),
            config_path=self.config_path,
        )
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8000")
        self.hdr = {"X-Job-Agent": "console"}

    def test_content_routes_registered_with_hardening(self) -> None:
        # GET 放行（无副作用端点）
        self.assertEqual(self.client.get("/api/resume/structured").status_code, 200)
        self.assertEqual(self.client.get("/api/greetings/current").status_code, 200)
        # 非 GET 缺安全头 → 403（S5a 加固第 3 项对 S5b 路由生效）
        response = self.client.post("/api/directions/generate")
        self.assertEqual(response.status_code, 403)

        # 带头放行：上传链贯通（AI 桩）
        stub = DispatchAI()
        stub.route(parser.StructuredResume, structured_result(snapshot_v1()))
        from unittest.mock import patch

        from job_agent.ai import _client_ref

        with patch.object(_client_ref, "resolve_call_json", lambda: stub):
            response = self.client.post(
                "/api/resume/upload",
                files={"file": ("resume.pdf", build_pdf_bytes(RESUME_BODY), "application/pdf")},
                headers=self.hdr,
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["regenerate_hint"])

    def test_error_envelope_on_domain_400(self) -> None:
        response = self.client.post("/api/directions/generate", headers=self.hdr)
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"]["code"], "bad_request")
        self.assertIn("尚未上传简历", body["error"]["message"])

    def test_error_envelope_on_validation_422(self) -> None:
        response = self.client.put(
            "/api/directions",
            json={"source": "confirm", "directions": [{"keywords": []}]},
            headers=self.hdr,
        )
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_config_save_enqueues_reload_config(self) -> None:
        from job_agent.config import write_back

        write_back(seeded_config(), self.config_path)
        fetched = self.client.get("/api/config").json()["config"]
        fetched["delivery"]["daily_limit"] = 33
        response = self.client.put("/api/config", json=fetched, headers=self.hdr)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["reload_requested"])
        command = self.command_queue.get_nowait()
        self.assertEqual(command.cmd, "reload_config")
        self.assertEqual(load_config(self.config_path).delivery.daily_limit, 33)

    def test_masking_layers_stable(self) -> None:
        """S5b 源头打码 + S5a 响应中间件兜底：两层叠加幂等（一次 GET 即可断言）。"""
        from job_agent.config import write_back

        write_back(seeded_config(), self.config_path)
        body = self.client.get("/api/config/ai").json()["ai"]
        self.assertEqual(body["api_key"], "****1234")
        notify = self.client.get("/api/config/notify").json()["notify"]
        self.assertEqual(notify["feishu"]["secret"], "****et99")

    def test_without_db_no_content_routes(self) -> None:
        """db 缺省（S5a 纯骨架形态）：S5b 路由不注册，health 仍可用。"""
        app = create_app()
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        self.assertEqual(client.get("/api/health").status_code, 200)
        self.assertEqual(client.get("/api/resume/structured").status_code, 404)


if __name__ == "__main__":
    unittest.main()
