"""诊断路由测试（S5b）：POST /api/notify/test + POST /api/ai/test。

覆盖：通知测试假端点默认（渲染正文、零网络请求）、真实发送（未配置
ok=false / 正常投递 / 打码配置 → 原值生效）、邮件假端点预览、
AI 连通性测试（表单值一次性客户端 / 当前生效客户端 / 预算耗尽文案 /
非法配置 422）。渠道与客户端均以桩注入，TEST1 全离线。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from job_agent.ai.errors import AIBudgetExhausted
from job_agent.console.api import diagnostics
from job_agent.notify.feishu import FeishuConfig

from .support import ConsoleApiTestCase


class RecordingFeishu:
    """FeishuNotifier 桩：记录配置与投递，不联网。"""

    last: RecordingFeishu | None = None

    def __init__(self, config: FeishuConfig) -> None:
        self.config = config
        self.sent: list[object] = []
        self.closed = False
        RecordingFeishu.last = self

    @property
    def configured(self) -> bool:
        return bool(self.config.webhook)

    def applies(self, level: str) -> bool:  # pragma: no cover - 接口满足
        return True

    def send(self, record: object) -> None:
        self.sent.append(record)

    def close(self) -> None:
        self.closed = True


class StubProbeClient:
    """AIClient 桩：记录构造参数，返回 pong=True。"""

    last: StubProbeClient | None = None

    def __init__(self, config: object, **kwargs: object) -> None:
        self.config = config
        self.kwargs = kwargs
        StubProbeClient.last = self

    def call_json(self, messages: object, schema: type) -> object:
        return schema(pong=True)


class BudgetClient:
    """预算耗尽客户端（get_default_client 替身）。"""

    def call_json(self, messages: object, schema: type) -> object:
        raise AIBudgetExhausted("当日 AI 调用已达预算上限 1000 次（B13）")


class NotifyTestApiTest(ConsoleApiTestCase):
    """POST /api/notify/test。"""

    def test_fake_default_renders_without_sending(self) -> None:
        with patch.object(diagnostics, "FeishuNotifier", RecordingFeishu):
            response = self.client.post(
                "/api/notify/test",
                json={"channel": "feishu"},  # mode 缺省 = fake
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["mode"], "fake")
        self.assertFalse(body["configured"])  # 未配置 webhook（空库配置）
        self.assertIn("CONSOLE_TEST", body["formatted"])
        self.assertIn("feishu", body["formatted"])
        assert RecordingFeishu.last is not None
        self.assertEqual(len(RecordingFeishu.last.sent), 0)  # 假端点：不发送

    def test_fake_with_form_config_marked_configured(self) -> None:
        with patch.object(diagnostics, "FeishuNotifier", RecordingFeishu):
            response = self.client.post(
                "/api/notify/test",
                json={
                    "channel": "feishu",
                    "mode": "fake",
                    "config": {
                        "webhook": "https://open.feishu.cn/hook/new-hook-001",
                        "levels": ["high"],
                    },
                },
            )
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["configured"])
        assert RecordingFeishu.last is not None
        self.assertEqual(len(RecordingFeishu.last.sent), 0)  # 假端点：不发送
        self.assertTrue(RecordingFeishu.last.closed)

    def test_fake_email_preview(self) -> None:
        response = self.client.post(
            "/api/notify/test",
            json={
                "channel": "email",
                "mode": "fake",
                "config": {"smtp_host": "smtp.test", "to": "a@b.com"},
            },
        )
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertIn("收件人：a@b.com", body["formatted"])
        self.assertIn("CONSOLE_TEST", body["formatted"])

    def test_real_not_configured_ok_false(self) -> None:
        with patch.object(diagnostics, "FeishuNotifier", RecordingFeishu):
            response = self.client.post(
                "/api/notify/test", json={"channel": "feishu", "mode": "real"}
            )
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertFalse(body["configured"])
        self.assertIn("未配置", body["error"])

    def test_real_sends_with_masked_form_config_resolved(self) -> None:
        """真实模式 + 表单值（打码回显）→ 以存储原值投递。"""
        self.seed_config_file()
        with patch.object(diagnostics, "FeishuNotifier", RecordingFeishu):
            response = self.client.post(
                "/api/notify/test",
                json={
                    "channel": "feishu",
                    "mode": "real",
                    "config": {"webhook": "****ef99"},  # 打码回显 → 原值
                },
            )
        body = response.json()
        self.assertTrue(body["ok"])
        assert RecordingFeishu.last is not None
        self.assertEqual(
            RecordingFeishu.last.config.webhook,
            "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef99",
        )
        self.assertEqual(len(RecordingFeishu.last.sent), 1)

    def test_real_send_failure_reported(self) -> None:
        class BrokenFeishu(RecordingFeishu):
            def send(self, record: object) -> None:
                raise RuntimeError("webhook 404")

        with patch.object(diagnostics, "FeishuNotifier", BrokenFeishu):
            response = self.client.post(
                "/api/notify/test",
                json={"channel": "feishu", "mode": "real", "config": {"webhook": "https://x/hook"}},
            )
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("RuntimeError", body["error"])
        self.assertIn("webhook 404", body["error"])

    def test_invalid_channel_422(self) -> None:
        response = self.client.post("/api/notify/test", json={"channel": "sms"})
        self.assertEqual(response.status_code, 422)

    def test_invalid_form_config_422(self) -> None:
        """表单配置非法（levels 缺 high 的约束在渠道模型上同样生效）。"""
        response = self.client.post(
            "/api/notify/test",
            json={"channel": "feishu", "config": {"levels": ["low"]}},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("high", response.json()["detail"])


class AiTestApiTest(ConsoleApiTestCase):
    """POST /api/ai/test。"""

    def test_with_form_config_uses_one_off_client(self) -> None:
        with patch.object(diagnostics, "AIClient", StubProbeClient):
            response = self.client.post(
                "/api/ai/test",
                json={"config": {"base_url": "https://api.test/v1", "api_key": "sk-form-key-777"}},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["pong"])
        self.assertGreaterEqual(body["latency_ms"], 0)
        assert StubProbeClient.last is not None
        self.assertEqual(StubProbeClient.last.config.api_key, "sk-form-key-777")

    def test_masked_form_config_resolved_to_stored(self) -> None:
        self.seed_config_file()
        with patch.object(diagnostics, "AIClient", StubProbeClient):
            self.client.post("/api/ai/test", json={"config": {"api_key": "****1234"}})
        assert StubProbeClient.last is not None
        self.assertEqual(StubProbeClient.last.config.api_key, "sk-test1234")

    def test_without_config_uses_default_client(self) -> None:
        with patch.object(diagnostics, "get_default_client", lambda: BudgetClient()):
            response = self.client.post("/api/ai/test", json={})
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("预算", body["error"])

    def test_invalid_config_422(self) -> None:
        response = self.client.post("/api/ai/test", json={"config": {"temperature": 9}})
        self.assertEqual(response.status_code, 422)
        self.assertIn("temperature", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
