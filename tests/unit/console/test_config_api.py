"""配置路由测试（S5b；§10.2「投递任务配置 / 通知配置 / AI 配置」行）。

覆盖：GET 打码回显（****尾4位）、PUT 写回往返（打码回显 → 原值写回）、
校验失败 422（auto_reply_limit 下限 [A2] / feishu.levels 必含 high /
未知字段）、分节保存合入整树（其余节不受影响）、same_as_delivery 标记
往返、派生信息（safety_warnings [R6-1] / G3 每日实际上限推算 / 全量重写
提示 [B14]）、文件确为全量重写产物（手写注释不保留）。
"""

from __future__ import annotations

import unittest

from job_agent.config import load_config

from .support import ConsoleApiTestCase


class ConfigGetApiTest(ConsoleApiTestCase):
    """GET /api/config：默认值 + 打码 + 派生信息。"""

    def test_defaults_when_no_file(self) -> None:
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["config"]["delivery"]["daily_limit"], 100)
        self.assertEqual(body["config"]["delivery"]["polling"]["schedule"], "same_as_delivery")
        self.assertEqual(body["config"]["ai"]["api_key"], "")  # 空值不打码
        derived = body["derived"]
        # G3：min(100, 200) = 100；放量首日 min(10, 60) = 10
        self.assertEqual(derived["effective_daily_delivery"]["effective_daily_cap"], 100)
        self.assertEqual(derived["effective_daily_delivery"]["rampup_first_day_cap"], 10)
        self.assertIn("安全优先于投递量", derived["effective_daily_delivery"]["note"])
        # B14：全量重写提示
        self.assertIn("手写注释不会被保留", derived["rewrite_notice"])
        # 默认参数在实测验证节奏之上 → 无警告 [R6-1]
        self.assertEqual(derived["safety_warnings"], [])

    def test_sensitive_fields_masked(self) -> None:
        self.seed_config_file()
        body = self.client.get("/api/config").json()["config"]
        self.assertEqual(body["ai"]["api_key"], "****1234")
        self.assertEqual(body["notify"]["feishu"]["webhook"], "****ef99")
        self.assertEqual(body["notify"]["feishu"]["secret"], "****et99")
        self.assertEqual(body["notify"]["email"]["password"], "****9876")

    def test_section_gets_masked(self) -> None:
        self.seed_config_file()
        notify = self.client.get("/api/config/notify").json()["notify"]
        self.assertEqual(notify["feishu"]["webhook"], "****ef99")
        self.assertEqual(notify["email"]["password"], "****9876")
        self.assertEqual(notify["feishu"]["levels"], ["high", "medium"])  # 非敏感原样
        ai = self.client.get("/api/config/ai").json()["ai"]
        self.assertEqual(ai["api_key"], "****1234")
        self.assertEqual(ai["model"], "deepseek-chat")


class ConfigPutApiTest(ConsoleApiTestCase):
    """PUT /api/config：整树写回往返。"""

    def test_roundtrip_masked_echo_keeps_original_secret(self) -> None:
        """GET（打码）→ 改限额 → PUT → 文件中 api_key 仍为原值（写入走原值）。"""
        self.seed_config_file()
        fetched = self.client.get("/api/config").json()["config"]
        fetched["delivery"]["daily_limit"] = 50
        response = self.client.put("/api/config", json=fetched)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

        on_disk = load_config(self.config_path)
        self.assertEqual(on_disk.delivery.daily_limit, 50)
        self.assertEqual(on_disk.ai.api_key, "sk-test1234")
        self.assertEqual(
            on_disk.notify.feishu.webhook, "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef99"
        )
        self.assertEqual(on_disk.notify.email.password, "pwd9876")
        # 回显仍打码
        again = self.client.get("/api/config").json()["config"]
        self.assertEqual(again["ai"]["api_key"], "****1234")
        self.assertEqual(again["delivery"]["daily_limit"], 50)

    def test_new_plaintext_secret_replaces(self) -> None:
        self.seed_config_file()
        fetched = self.client.get("/api/config").json()["config"]
        fetched["ai"]["api_key"] = "sk-brand-new-key9999"
        self.client.put("/api/config", json=fetched)
        self.assertEqual(load_config(self.config_path).ai.api_key, "sk-brand-new-key9999")

    def test_empty_secret_clears_value(self) -> None:
        self.seed_config_file()
        fetched = self.client.get("/api/config").json()["config"]
        fetched["notify"]["email"]["password"] = ""
        self.client.put("/api/config", json=fetched)
        self.assertEqual(load_config(self.config_path).notify.email.password, "")

    def test_auto_reply_limit_zero_422_and_file_unchanged(self) -> None:
        self.seed_config_file()
        fetched = self.client.get("/api/config").json()["config"]
        fetched["messaging"]["auto_reply_limit"] = 0  # 下限 1 [A2]
        response = self.client.put("/api/config", json=fetched)
        self.assertEqual(response.status_code, 422)
        self.assertIn("auto_reply_limit", response.json()["detail"])
        # 校验失败不写文件
        self.assertEqual(load_config(self.config_path).messaging.auto_reply_limit, 2)

    def test_unknown_field_422(self) -> None:
        fetched = self.client.get("/api/config").json()["config"]
        fetched["delivery"]["typo_field"] = 1
        response = self.client.put("/api/config", json=fetched)
        self.assertEqual(response.status_code, 422)
        self.assertIn("typo_field", response.json()["detail"])

    def test_full_rewrite_drops_manual_comments(self) -> None:
        """保存 = 全量重写：文件中的手写注释不保留 [B14]。"""
        self.seed_config_file()
        text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text("# 我的手写注释\n" + text, encoding="utf-8")
        fetched = self.client.get("/api/config").json()["config"]
        self.client.put("/api/config", json=fetched)
        written = self.config_path.read_text(encoding="utf-8")
        self.assertNotIn("我的手写注释", written)
        self.assertIn("手写注释不会被保留", written)  # 生成文件头提示

    def test_same_as_delivery_marker_roundtrip(self) -> None:
        fetched = self.client.get("/api/config").json()["config"]
        self.assertEqual(fetched["delivery"]["polling"]["schedule"], "same_as_delivery")
        self.client.put("/api/config", json=fetched)
        self.assertIsNone(load_config(self.config_path).delivery.polling.schedule)
        self.assertIn("same_as_delivery", self.config_path.read_text(encoding="utf-8"))

    def test_derived_updated_after_put(self) -> None:
        fetched = self.client.get("/api/config").json()["config"]
        # 压低详情日浏览上限 → G3 推算收窄；压低最小间隔 → 安全警告 [R6-1]
        fetched["delivery"]["browse_limits"]["detail_daily"] = 60
        fetched["delivery"]["min_interval_s"]["detail"] = 10
        response = self.client.put("/api/config", json=fetched)
        derived = response.json()["derived"]
        self.assertEqual(derived["effective_daily_delivery"]["effective_daily_cap"], 60)
        self.assertTrue(any("min_interval_s.detail" in w for w in derived["safety_warnings"]))


class ConfigSectionApiTest(ConsoleApiTestCase):
    """GET/PUT /api/config/notify 与 /api/config/ai：分节读写。"""

    def test_notify_section_roundtrip_keeps_other_sections(self) -> None:
        self.seed_config_file()
        section = self.client.get("/api/config/notify").json()["notify"]
        section["feishu"]["secret"] = "****et99"  # 打码回显原样回传
        section["dnd"]["enabled"] = True
        section["dnd"]["start"] = "22:30"
        response = self.client.put("/api/config/notify", json=section)
        self.assertEqual(response.status_code, 200)

        on_disk = load_config(self.config_path)
        self.assertEqual(on_disk.notify.feishu.secret, "topsecret99")  # 原值写回
        self.assertTrue(on_disk.notify.dnd.enabled)
        self.assertEqual(on_disk.notify.dnd.start, "22:30")
        # 其余节不受影响
        self.assertEqual(on_disk.ai.api_key, "sk-test1234")
        self.assertEqual(on_disk.delivery.daily_limit, 100)

    def test_notify_levels_without_high_422(self) -> None:
        section = self.client.get("/api/config/notify").json()["notify"]
        section["feishu"]["levels"] = ["medium"]
        response = self.client.put("/api/config/notify", json=section)
        self.assertEqual(response.status_code, 422)
        self.assertIn("必须包含 high", response.json()["detail"])

    def test_ai_section_roundtrip_and_update(self) -> None:
        self.seed_config_file()
        section = self.client.get("/api/config/ai").json()["ai"]
        section["api_key"] = "****1234"  # 打码回显
        section["model"] = "deepseek-reasoner"
        section["min_score"] = 70
        response = self.client.put("/api/config/ai", json=section)
        self.assertEqual(response.status_code, 200)

        on_disk = load_config(self.config_path)
        self.assertEqual(on_disk.ai.api_key, "sk-test1234")
        self.assertEqual(on_disk.ai.model, "deepseek-reasoner")
        self.assertEqual(on_disk.ai.min_score, 70)

    def test_ai_section_invalid_temperature_422(self) -> None:
        section = self.client.get("/api/config/ai").json()["ai"]
        section["temperature"] = 5
        response = self.client.put("/api/config/ai", json=section)
        self.assertEqual(response.status_code, 422)
        self.assertIn("temperature", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
