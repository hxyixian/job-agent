"""S3d notify/feishu：签名正确性 + 假端点断言（本地 HTTP，绝不真实外发）[T3]。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
import pytest
from fake_webhook import FakeWebhook  # tests/sim（conftest 已加入 sys.path）

from job_agent.config import Config, FeishuConfig
from job_agent.notify import EventRecord, NotificationCenter
from job_agent.notify.email import EmailNotifier
from job_agent.notify.feishu import FeishuNotifier, format_event_text, gen_sign

# ---- 签名算法（官方算法的独立复算 + 黄金向量） ----


def test_gen_sign_matches_official_algorithm() -> None:
    """按飞书文档算法独立复算：key = "{timestamp}\\n{secret}"、空消息、Base64。"""
    ts, secret = "1762500000", "test-secret"
    # 官方文档算法逐字复现（独立于被测实现书写）
    string_to_sign = f"{ts}\n{secret}"
    expected = base64.b64encode(
        hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    ).decode("utf-8")
    assert gen_sign(ts, secret) == expected


def test_gen_sign_golden_vector() -> None:
    """黄金向量（开发期由官方算法预计算的定值，防实现与测试同源漂移）。"""
    assert gen_sign("1762500000", "test-secret") == "NekhJpvecvSSNOFdKbsNb2F5aXxK11c2zMb2Bx+hWcE="
    # 换 secret 结果随之变化（确非恒等/时序依赖）
    assert gen_sign("1762500000", "other") != gen_sign("1762500000", "test-secret")


def test_signed_request_body_structure() -> None:
    """带 secret 的请求体含 timestamp/sign/msg_type/content，sign 与 timestamp 配对可验。"""
    with FakeWebhook() as fake, httpx.Client(trust_env=False) as client:
        notifier = FeishuNotifier(
            FeishuConfig(webhook=fake.url, secret="s3cret", levels=["high", "medium"]),
            client=client,
        )
        notifier.send(EventRecord("RISK_PAUSE", "high", {"signal": "CAPTCHA"}, 1, "t"))
        request = fake.requests[0]
        body = request.json
        assert request.method == "POST"
        assert body["msg_type"] == "text"
        assert body["timestamp"].isdigit()
        # 签名可由接收方用同算法验证（timestamp 来自实际请求体）
        assert body["sign"] == gen_sign(str(body["timestamp"]), "s3cret")
        assert "RISK_PAUSE" in body["content"]["text"]
        assert "[job-agent][high]" in body["content"]["text"]


def test_unsigned_when_secret_empty() -> None:
    with FakeWebhook() as fake, httpx.Client(trust_env=False) as client:
        notifier = FeishuNotifier(
            FeishuConfig(webhook=fake.url, secret="", levels=["high"]),
            client=client,
        )
        notifier.send(EventRecord("LOGIN_EXPIRED", "high", {}, 1, "t"))
        body = fake.requests[0].json
        assert "sign" not in body and "timestamp" not in body  # 未启用签名


def test_level_filter_and_configured_flag() -> None:
    cfg = FeishuConfig(webhook="http://127.0.0.1:1/hook", levels=["high", "medium"])
    notifier = FeishuNotifier(cfg)
    assert notifier.applies("high") and notifier.applies("medium")
    assert not notifier.applies("low")
    assert notifier.configured is True

    empty = FeishuNotifier(FeishuConfig())
    assert empty.configured is False  # webhook 未配置


def test_send_http_error_raises_for_center_status() -> None:
    """端点 5xx：send 抛异常（管线转为 error 状态，不向上传播）。"""
    with FakeWebhook() as fake, httpx.Client(trust_env=False) as client:
        fake.set_response_status(500)
        notifier = FeishuNotifier(
            FeishuConfig(webhook=fake.url, levels=["high"]),
            client=client,
        )
        with pytest.raises(httpx.HTTPStatusError):
            notifier.send(EventRecord("RISK_PAUSE", "high", {}, 1, "t"))
        assert len(fake.requests) == 1  # 无内置重试（失败记状态即可）


def test_center_to_fake_webhook_end_to_end(db) -> None:
    """经通知中心管线 → 假端点的端到端：级别过滤 + 签名正文 + delivered_json=ok。"""
    with FakeWebhook() as fake, httpx.Client(trust_env=False) as client:
        config = Config.model_validate(
            {
                "notify": {
                    "feishu": {
                        "webhook": fake.url,
                        "secret": "e2e",
                        "levels": ["high", "medium"],
                    }
                }
            }
        )
        feishu = FeishuNotifier(config.notify.feishu, client=client)
        email = EmailNotifier(config.notify.email)  # 未配置 → skip:not_configured
        center = NotificationCenter(db, config, [feishu, email])
        try:
            center.emit("KEY_QUESTION", {"q": "到岗时间？"})
            center.emit("DAILY_DIGEST", {})  # low：飞书过滤，无请求
        finally:
            center.close()
        assert len(fake.requests) == 1
        body = fake.requests[0].json
        assert body["sign"] == gen_sign(str(body["timestamp"]), "e2e")
        text = body["content"]["text"]
        assert "KEY_QUESTION" in text and "到岗时间？" in text
        row = db.query_one(
            "SELECT delivered_json FROM notification_events WHERE event_type = 'KEY_QUESTION'"
        )
        delivered = json.loads(str(row["delivered_json"]))
        assert delivered["feishu"] == "ok"
        assert delivered["email"] == "skip:not_configured"


def test_payload_truncated_in_message_text() -> None:
    long_payload = {"blob": "x" * 2000}
    record = EventRecord("ADAPTER_BROKEN", "high", long_payload, 1, "t")
    text = format_event_text(record)
    assert len(text) < 1000  # 截断生效（正文上限 ≈ 头 + 800）
    assert text.endswith("…（截断）")
    assert format_event_text(EventRecord("RISK_PAUSE", "high", {"a": 1}, 1, "t")).count("\n") == 1
