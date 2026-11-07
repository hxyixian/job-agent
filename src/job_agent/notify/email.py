"""邮件通知渠道（§11；SMTP SSL，**仅 high 级**）。

- 级别过滤硬编码 ``applies = (level == "high")``——§4 注释"email …（仅 high）"，
  不开放配置（与飞书的 levels 可配不同）；
- 投递：``smtplib.SMTP_SSL``（默认端口 465）+ LOGIN + ``send_message``；
- 参数完备性：smtp_host/user/password/to 任一为空 → ``configured=False``
  （管线记 skip:not_configured，不建连接）；
- ``smtp_factory`` 可注入（测试用本地 aiosmtpd sink 替换为明文 SMTP；
  产品路径恒 SSL）。
"""

from __future__ import annotations

import json
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import ClassVar, Protocol

from job_agent.config import EmailConfig
from job_agent.notify.base import EventRecord, Level, Notifier

__all__ = ["EmailNotifier"]

_TIMEOUT_S = 10.0


class _SmtpFactory(Protocol):
    """``(host, port) → SMTP 上下文管理器``（默认 SMTP_SSL；测试注入明文）。"""

    def __call__(self, host: str, port: int) -> smtplib.SMTP: ...


def _ssl_factory(host: str, port: int) -> smtplib.SMTP:
    return smtplib.SMTP_SSL(host, port, timeout=_TIMEOUT_S)


class EmailNotifier(Notifier):
    """SMTP SSL 渠道（仅 high）。"""

    name: ClassVar[str] = "email"

    def __init__(self, config: EmailConfig, *, smtp_factory: _SmtpFactory | None = None) -> None:
        self._config = config
        self._factory: _SmtpFactory = smtp_factory if smtp_factory is not None else _ssl_factory

    @property
    def configured(self) -> bool:
        c = self._config
        return bool(c.smtp_host and c.user and c.password and c.to)

    def applies(self, level: Level) -> bool:
        return level == "high"

    def send(self, record: EventRecord) -> None:
        c = self._config
        if not (c.smtp_host and c.user and c.to):
            return  # 防御：configured=False 时管线不会调用（双保险）
        msg = EmailMessage()
        msg["Subject"] = f"[job-agent][{record.level}] {record.event_type}"
        msg["From"] = c.user
        msg["To"] = c.to
        msg["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
        body = (
            f"事件：{record.event_type}\n"
            f"级别：{record.level}\n"
            f"时间（UTC）：{record.created_at}\n\n"
            f"payload：\n"
            f"{json.dumps(record.payload, ensure_ascii=False, sort_keys=True, indent=2)}\n"
        )
        msg.set_content(body)
        with self._factory(c.smtp_host, c.smtp_port) as smtp:
            smtp.login(c.user, c.password)
            smtp.send_message(msg)
