"""S3d notify/email：仅 high + 本地 SMTP sink（aiosmtpd，不真实外发）[T3]。"""

from __future__ import annotations

import json
import smtplib

import pytest
from fake_smtp import FakeSmtpSink  # tests/sim（conftest 已加入 sys.path）

from job_agent.config import Config, EmailConfig
from job_agent.notify import EventRecord
from job_agent.notify.email import EmailNotifier


def _record(event_type: str = "RISK_PAUSE", level: str = "high") -> EventRecord:
    return EventRecord(event_type, level, {"signal": "CAPTCHA", "note": "验证码"}, 7, "t")  # type: ignore[arg-type]


@pytest.fixture()
def sink() -> FakeSmtpSink:
    with FakeSmtpSink() as instance:
        yield instance


@pytest.fixture()
def email_config(sink: FakeSmtpSink) -> EmailConfig:
    return EmailConfig(
        smtp_host="127.0.0.1", smtp_port=sink.port, user="bot@local", password="pw", to="me@local"
    )


@pytest.fixture()
def plain_factory() -> callable:
    """明文 SMTP 客户端工厂（sink 无 TLS；产品路径恒 SMTP_SSL，见模块 docstring）。"""
    return lambda host, port: smtplib.SMTP(host, port, timeout=5)


def test_only_high_applies() -> None:
    notifier = EmailNotifier(EmailConfig())
    assert notifier.applies("high") is True
    assert notifier.applies("medium") is False
    assert notifier.applies("low") is False


def test_unconfigured_skipped(sink: FakeSmtpSink) -> None:
    for partial in (
        EmailConfig(),  # 全空
        EmailConfig(smtp_host="127.0.0.1", smtp_port=sink.port),  # 缺账号/收件人
    ):
        notifier = EmailNotifier(partial)
        assert notifier.configured is False
        notifier.send(_record())  # 无连接建立、无异常
    assert sink.messages == []


def test_send_delivers_high_to_local_sink(
    sink: FakeSmtpSink, email_config: EmailConfig, plain_factory: callable
) -> None:
    notifier = EmailNotifier(email_config, smtp_factory=plain_factory)
    notifier.send(_record("LOGIN_EXPIRED"))
    assert len(sink.messages) == 1
    msg = sink.messages[0]
    assert msg["Subject"] == "[job-agent][high] LOGIN_EXPIRED"
    assert msg["From"] == "bot@local"
    assert msg["To"] == "me@local"
    body = msg.get_content()
    assert "事件：LOGIN_EXPIRED" in body
    assert "级别：high" in body
    assert "时间（UTC）：t" in body
    assert '"signal": "CAPTCHA"' in body  # payload JSON（indent=2 排版）进入正文
    assert '"note": "验证码"' in body


def test_center_routes_only_high_to_email(
    db, sink: FakeSmtpSink, email_config: EmailConfig, plain_factory: callable
) -> None:
    """管线级断言：medium/low 不进邮件，high 进（即使飞书收 medium）。"""
    from .conftest import RecordingNotifier, make_center

    config = Config.model_validate({"notify": {"email": email_config.model_dump()}})
    feishu = RecordingNotifier({"high", "medium"}, channel_name="feishu")
    email = EmailNotifier(config.notify.email, smtp_factory=plain_factory)
    center = make_center(db, config, feishu, email)
    try:
        center.emit("KEY_QUESTION", {"q": "薪资"})  # medium → 仅 feishu
        center.emit("LOGIN_EXPIRED", {})  # high → 邮件
    finally:
        center.close()
    assert len(sink.messages) == 1
    assert "LOGIN_EXPIRED" in str(sink.messages[0]["Subject"])
    row = db.query_one(
        "SELECT delivered_json FROM notification_events WHERE event_type = 'KEY_QUESTION'"
    )
    assert json.loads(str(row["delivered_json"]))["email"] == "skip:level"
