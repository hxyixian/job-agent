"""招呼语工作台路由测试（S5b；§10.2「招呼语工作台」行）。

覆盖：双模板当前态（未定稿 null → FR5.3 页面提示依据）、候选生成
（前置简历/AI 失败 400/恒 3 条）、定稿保存（空内容 422/kind 枚举 422/
双 kind 独立版本链）、**F1 同步确认勾选生命周期**（未定稿 400 → 确认后
platform_synced=True → 重新定稿自动失效（内容哈希绑定）→ 再确认恢复）。
"""

from __future__ import annotations

import unittest

from job_agent.ai.errors import AIBudgetExhausted
from job_agent.ai.greeting import GreetingCandidates

from .support import ConsoleApiTestCase, DispatchAI, snapshot_v1


def stub_candidates() -> GreetingCandidates:
    return GreetingCandidates(candidates=["候选一", "候选二", "候选三"])


class GreetingsCurrentApiTest(ConsoleApiTestCase):
    """GET /api/greetings/current 与定稿保存。"""

    def test_initial_both_null(self) -> None:
        response = self.client.get("/api/greetings/current")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["active"])
        self.assertIsNone(body["passive"])

    def test_finalize_and_two_kinds_independent(self) -> None:
        response = self.client.put(
            "/api/greetings/current", json={"kind": "active", "content": "主动定稿"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["content"], "主动定稿")
        self.assertEqual(body["version"], 1)
        self.assertFalse(body["platform_synced"])  # 新定稿未同步

        self.client.put("/api/greetings/current", json={"kind": "passive", "content": "被动定稿"})
        current = self.client.get("/api/greetings/current").json()
        self.assertEqual(current["active"]["content"], "主动定稿")
        self.assertEqual(current["passive"]["content"], "被动定稿")

        # 重新定稿 → 版本递增，同步确认随之失效
        again = self.client.put(
            "/api/greetings/current", json={"kind": "active", "content": "主动定稿v2"}
        ).json()
        self.assertEqual(again["version"], 2)
        self.assertFalse(again["platform_synced"])

    def test_finalize_blank_content_422(self) -> None:
        response = self.client.put(
            "/api/greetings/current", json={"kind": "active", "content": "   "}
        )
        self.assertEqual(response.status_code, 422)

    def test_finalize_invalid_kind_422(self) -> None:
        response = self.client.put("/api/greetings/current", json={"kind": "greet", "content": "x"})
        self.assertEqual(response.status_code, 422)


class GreetingsGenerateApiTest(ConsoleApiTestCase):
    """POST /api/greetings/generate?kind=。"""

    def test_generate_requires_resume(self) -> None:
        response = self.client.post("/api/greetings/generate", params={"kind": "active"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("尚未上传简历", response.json()["detail"])

    def test_generate_invalid_kind_422(self) -> None:
        response = self.client.post("/api/greetings/generate", params={"kind": "bad"})
        self.assertEqual(response.status_code, 422)

    def test_generate_ai_failure_400(self) -> None:
        self.upload_resume(snapshot_v1())
        stub = DispatchAI()
        stub.route(GreetingCandidates, error=AIBudgetExhausted("预算耗尽"))
        self.install_ai(stub)
        response = self.client.post("/api/greetings/generate", params={"kind": "passive"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("预算耗尽", response.json()["detail"])

    def test_generate_returns_three_with_confirmed_directions(self) -> None:
        self.upload_resume(snapshot_v1())
        self.confirm_directions(["Python", "后端"])
        stub = DispatchAI()
        stub.route(GreetingCandidates, stub_candidates())
        self.install_ai(stub)
        response = self.client.post("/api/greetings/generate", params={"kind": "active"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["kind"], "active")
        self.assertEqual(body["candidates"], ["候选一", "候选二", "候选三"])
        # 求职目标 = 已确认方向（进入 prompt 数据块）
        messages = stub.calls[0][0]
        self.assertTrue(any("Python、后端" in m["content"] for m in messages))


class PlatformSyncApiTest(ConsoleApiTestCase):
    """POST /api/greetings/platform-synced：F1 确认勾选（内容哈希绑定）。"""

    def _finalize(self, content: str) -> None:
        response = self.client.put(
            "/api/greetings/current", json={"kind": "active", "content": content}
        )
        assert response.status_code == 200, response.text

    def test_sync_before_finalize_400(self) -> None:
        response = self.client.post("/api/greetings/platform-synced", json={"kind": "active"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("尚未定稿", response.json()["detail"])

    def test_sync_lifecycle_confirm_invalidate_reconfirm(self) -> None:
        self._finalize("定稿内容A")
        # 确认勾选 → 与当前内容哈希绑定
        response = self.client.post("/api/greetings/platform-synced", json={"kind": "active"})
        self.assertEqual(response.status_code, 200)
        synced = response.json()
        self.assertTrue(synced["platform_synced"])
        self.assertIsNotNone(synced["platform_synced_at"])

        current = self.client.get("/api/greetings/current").json()["active"]
        self.assertTrue(current["platform_synced"])

        # 模板修改（重新定稿）→ 勾选自动失效 [F1/FR5.3]
        self._finalize("定稿内容B")
        current = self.client.get("/api/greetings/current").json()["active"]
        self.assertFalse(current["platform_synced"])
        self.assertEqual(current["version"], 2)

        # 对新内容重新确认 → 恢复有效
        response = self.client.post("/api/greetings/platform-synced", json={"kind": "active"})
        self.assertTrue(response.json()["platform_synced"])

    def test_sync_only_binds_active_kind_row(self) -> None:
        """勾选按 kind 定位当前行：passive 定稿不受 active 勾选影响。"""
        self._finalize("主动内容")
        self.client.put("/api/greetings/current", json={"kind": "passive", "content": "被动内容"})
        self.client.post("/api/greetings/platform-synced", json={"kind": "active"})
        current = self.client.get("/api/greetings/current").json()
        self.assertTrue(current["active"]["platform_synced"])
        self.assertFalse(current["passive"]["platform_synced"])


if __name__ == "__main__":
    unittest.main()
