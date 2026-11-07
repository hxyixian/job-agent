"""S4b 浏览器会话层测试共享装配（mock page，无真实浏览器）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from job_agent.browser.patchright_session import PatchrightSession
from job_agent.config import Config
from job_agent.db import Database, open_database

from .mock_browser import FakeChromium, FakePlaywright, make_pw

LOGGER_NAME = "job_agent.browser"


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = open_database(tmp_path / "browser_test.db")
    yield database
    database.close()


@pytest.fixture()
def config() -> Config:
    return Config()  # 出厂默认：channel=chrome、headless=False


@pytest.fixture()
def profile(tmp_path: Path) -> Path:
    return tmp_path / "prof"


class SteppableClock:
    """可步进时钟（重建限流 / V9 降档时间窗测试）。"""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


@pytest.fixture()
def clock() -> SteppableClock:
    return SteppableClock(datetime(2026, 11, 7, 10, 0, tzinfo=UTC))


def make_session(
    db: Database,
    config: Config,
    profile: Path,
    *,
    clock: SteppableClock | None = None,
    versions: tuple[str, ...] | list[str] = ("157.0.8103.38",),
    **kwargs: Any,
) -> tuple[PatchrightSession, FakePlaywright, FakeChromium]:
    """构造注入替身的 PatchrightSession（不启动；测试自行 start）。"""
    pw, chromium = make_pw(versions)
    session = PatchrightSession(
        config,
        db,
        profile_dir=profile,
        clock=clock,
        pw_provider=lambda: pw,
        **kwargs,
    )
    return session, pw, chromium
