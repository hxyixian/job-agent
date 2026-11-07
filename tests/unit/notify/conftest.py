"""tests/unit/notify 共享夹具：临时库 + 录制型假渠道。

假渠道 ``RecordingNotifier`` 不触网——网络路径的正/负例由
``fake_webhook``/``fake_smtp``（tests/sim，仅绑定 127.0.0.1）单独覆盖。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from job_agent.config import Config
from job_agent.db import Database, open_database
from job_agent.notify import EventRecord, Level, NotificationCenter, Notifier


class RecordingNotifier(Notifier):
    """录制型渠道：按可配级别集合接收，记录 send 的每个事件。

    ``error_on`` 非 None 时对匹配的事件类型抛异常（验证"渠道失败不阻断
    管线、落库不受影响"）。
    """

    name: ClassVar[str] = "recorder"

    def __init__(
        self,
        levels: set[Level],
        *,
        channel_name: str = "recorder",
        configured: bool = True,
        error_on: frozenset[str] = frozenset(),
    ) -> None:
        self._levels = set(levels)
        self.name = channel_name  # 实例级渠道名（delivered_json 的键）
        self._configured = configured
        self._error_on = set(error_on)
        self.sent: list[EventRecord] = []

    @property
    def configured(self) -> bool:
        return self._configured

    def applies(self, level: Level) -> bool:
        return level in self._levels

    def send(self, record: EventRecord) -> None:
        if record.event_type in self._error_on:
            raise RuntimeError(f"simulated failure for {record.event_type}")
        self.sent.append(record)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = open_database(tmp_path / "notify_test.db")
    yield database
    database.close()


@pytest.fixture()
def config() -> Config:
    """默认配置（免打扰关、飞书/邮件未配置——测试显式覆盖各节）。"""
    return Config()


def make_center(db: Database, config: Config, *notifiers: Notifier) -> NotificationCenter:
    """构造并自动注册的通知中心（用例内配合 unregister 使用）。"""
    return NotificationCenter(db, config, list(notifiers))


def events_in(db: Database, event_type: str | None = None) -> list[tuple[str, str, str, str]]:
    """(event_type, level, payload_json, delivered_json) 按 id 升序。"""
    if event_type is None:
        rows = db.query(
            "SELECT event_type, level, payload_json, delivered_json"
            " FROM notification_events ORDER BY id"
        )
    else:
        rows = db.query(
            "SELECT event_type, level, payload_json, delivered_json"
            " FROM notification_events WHERE event_type = ? ORDER BY id",
            (event_type,),
        )
    return [
        (
            str(r["event_type"]),
            str(r["level"]),
            str(r["payload_json"]),
            "" if r["delivered_json"] is None else str(r["delivered_json"]),
        )
        for r in rows
    ]
