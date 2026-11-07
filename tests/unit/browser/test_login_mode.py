"""登录模式 [M3/G5]：零自动化参数启动 + 窗口存活期间引擎互斥。"""

from __future__ import annotations

import pytest

from job_agent.browser.strategy_base import BrowserSessionError, BrowserSetupError
from job_agent.config import Config
from job_agent.db import Database

from .conftest import make_session
from .mock_browser import FakeProcessRecorder

FAKE_EXE = "C:/fake/chrome.exe"


def _login_session(db: Database, config: Config, profile) -> tuple:
    recorder = FakeProcessRecorder()
    session, _, _ = make_session(
        db,
        config,
        profile,
        process_launcher=recorder.launch,
        chrome_executable_resolver=lambda channel: FAKE_EXE,
    )
    return session, recorder


def test_launch_login_window_zero_automation_args(db: Database, config: Config, profile) -> None:
    """M3/G5：登录窗口 = 普通浏览器进程，仅 --user-data-dir 一个参数——
    无调试端口/管道/任何自动化开关。"""
    session, recorder = _login_session(db, config, profile)
    session.launch_login_window()
    assert recorder.calls == [[FAKE_EXE, f"--user-data-dir={profile}"]]
    joined = " ".join(recorder.calls[0]).lower()
    assert "remote-debugging" not in joined
    assert "automation" not in joined
    assert session.login_window_alive() is True

    # 幂等：窗口已存活时重复调用不新开进程
    session.launch_login_window()
    assert len(recorder.calls) == 1


def test_engine_mutex_while_login_window_open(db: Database, config: Config, profile) -> None:
    """窗口存活期间引擎启动被拒（提示"请先关闭登录窗口"）；关闭后可启动。"""
    session, recorder = _login_session(db, config, profile)
    session.launch_login_window()
    with pytest.raises(BrowserSessionError, match="请先关闭登录窗口"):
        session.start()

    recorder.procs[0].exit()  # 用户关闭登录窗口
    assert session.login_window_alive() is False
    session.start()
    assert session.running is True


def test_login_window_rejected_while_engine_running(db: Database, config: Config, profile) -> None:
    """引擎浏览器运行中不能开登录窗口（同一 profile 双进程占用）。"""
    session, _ = _login_session(db, config, profile)
    session.start()
    with pytest.raises(BrowserSessionError, match="先停止引擎"):
        session.launch_login_window()


def test_login_window_requires_executable(db: Database, config: Config, profile) -> None:
    """找不到本机浏览器可执行文件 → BrowserSetupError（不静默失败）。"""
    session, _, _ = make_session(db, config, profile, chrome_executable_resolver=lambda channel: "")
    with pytest.raises(BrowserSetupError, match="未找到"):
        session.launch_login_window()


def test_engine_shutdown_allows_login_mode(db: Database, config: Config, profile) -> None:
    """引擎停止后可进入登录模式（FR1.3 登录失效恢复路径）。"""
    session, recorder = _login_session(db, config, profile)
    session.start()
    session.shutdown()
    session.launch_login_window()
    assert session.login_window_alive() is True
    assert len(recorder.calls) == 1
