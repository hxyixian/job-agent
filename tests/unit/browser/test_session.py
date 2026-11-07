"""S4b 会话契约与生命周期测试：接口面、§5.3 Best Practice 启动形态 [V4]、
Page 级原语、双标签页 [Q22]、TargetClosed 自动重建限流 [A-CORE-25]。"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from job_agent.browser.patchright_session import PatchrightSession
from job_agent.browser.strategy_base import (
    BrowserClosedError,
    BrowserNotRunningError,
    BrowserRiskError,
    BrowserSession,
    BrowserSessionError,
    LoginWindowOpenError,
)
from job_agent.config import Config
from job_agent.db import Database
from job_agent.models import RiskSignal

from .conftest import LOGGER_NAME, SteppableClock, make_session
from .mock_browser import FakeElement, FakePage, FakeProcessRecorder

SEARCH = "https://www.zhipin.com/web/geek/job?query=python&city=101020100"
SEARCH2 = "https://www.zhipin.com/web/geek/job?query=ai&city=101020100"
CHAT = "https://www.zhipin.com/web/geek/chat"

EXPECTED_ABSTRACT = {
    # profile 生命周期
    "start",
    "shutdown",
    "running",
    "awaiting_manual_relaunch",
    # 双标签页 [Q22]
    "ensure_session_tab",
    "switch_tab",
    "close_tab",
    # Page 级原语
    "goto",
    "wait_for_selector",
    "click",
    "query_text",
    "query_attrs",
    "scroll",
    "screenshot",
    "get_url",
    # 健康检测 [G2/H1]
    "check_health",
    "watch",
    "last_signal",
    # 登录模式 [M3/G5]
    "launch_login_window",
    "login_window_alive",
}


def _events_of(db: Database, event_type: str) -> list[tuple[str, dict[str, Any]]]:
    rows = db.query(
        "SELECT level, payload_json FROM notification_events WHERE event_type = ? ORDER BY id",
        (event_type,),
    )
    return [(str(r["level"]), json.loads(r["payload_json"])) for r in rows]


# ---- 接口契约（strategy_base）----


def test_browser_session_interface_surface() -> None:
    """抽象面 = §5.3 条目（8 原语 + 生命周期 + 双标签页 + 检测 + 登录模式）。"""
    abstract = {
        name
        for name, value in vars(BrowserSession).items()
        if getattr(value, "__isabstractmethod__", False)
    }
    assert abstract == EXPECTED_ABSTRACT
    assert issubclass(PatchrightSession, BrowserSession)
    with pytest.raises(TypeError):
        BrowserSession()  # type: ignore[abstract]


def test_context_manager_starts_and_shuts_down(
    db: Database, config: Config, profile, clock: SteppableClock
) -> None:
    session, _, chromium = make_session(db, config, profile, clock=clock)
    with session:
        assert session.running is True
    assert session.running is False
    assert chromium.contexts[0].closed is True


# ---- §5.3 Best Practice 启动形态 [V4/M4] ----


def test_start_uses_best_practice_launch_kwargs(
    db: Database, config: Config, profile, clock: SteppableClock, caplog: pytest.LogCaptureFixture
) -> None:
    """[V4] 逐字核对：channel/user_data_dir/headless=False/no_viewport=True，
    且**不含** args/ignore_default_args/extra_http_headers/user_agent（Patchright
    默认启动参数、无注入——第二轮 X4 定型形态）。"""
    session, _, chromium = make_session(db, config, profile, clock=clock)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        session.start()
    assert chromium.launch_calls == [
        {
            "channel": "chrome",
            "user_data_dir": str(profile),
            "headless": False,
            "no_viewport": True,
        }
    ]
    # [G4] Chrome 版本记录入日志
    assert any("Chrome 版本" in rec.message for rec in caplog.records)
    # 幂等：重复 start 不再 launch
    session.start()
    assert len(chromium.launch_calls) == 1
    assert session.awaiting_manual_relaunch is False


def test_not_running_ops_raise(db: Database, config: Config, profile) -> None:
    session, _, _ = make_session(db, config, profile)
    with pytest.raises(BrowserNotRunningError):
        session.get_url()
    with pytest.raises(BrowserNotRunningError):
        session.goto(SEARCH)
    session.shutdown()  # 幂等
    assert session.running is False


# ---- Page 级原语 ----


def test_primitives_roundtrip(db: Database, config: Config, profile) -> None:
    session, _, chromium = make_session(db, config, profile)
    session.start()
    session.switch_tab("work")
    assert chromium.contexts[0].new_page_count == 1
    page: FakePage = chromium.contexts[0].pages[0]

    landing = session.goto(SEARCH)
    assert landing == SEARCH
    assert page.nav_log == [SEARCH]
    assert session.get_url() == SEARCH

    page.elements[".job-card"] = [
        FakeElement("Python 工程师", {"href": "/job_detail/a.html"}),
        FakeElement("后端开发", {"href": "/job_detail/b.html"}),
    ]
    assert session.wait_for_selector(".job-card", timeout_ms=1000) is True
    assert session.wait_for_selector(".missing", timeout_ms=60) is False
    assert session.click(".job-card") is True
    assert session.click(".missing", timeout_ms=60) is False
    assert session.query_text(".job-card") == "Python 工程师"
    assert session.query_text(".missing") is None
    assert session.query_attrs(".job-card", ["href"]) == [
        {"href": "/job_detail/a.html"},
        {"href": "/job_detail/b.html"},
    ]
    session.scroll(dy=800)
    assert page.mouse.wheels == [(0, 800)]
    session.screenshot("shot.png")
    assert page.screenshots == ["shot.png"]


def test_wait_for_selector_slices_at_poll_interval(db: Database, config: Config, profile) -> None:
    """2s 粒度分片（注入 0.1s 测试粒度）：等待期间健康检查反复执行。"""
    session, _, chromium = make_session(db, config, profile, poll_interval_s=0.1)
    session.start()
    session.switch_tab("work")
    assert session.goto(SEARCH) == SEARCH
    page: FakePage = chromium.contexts[0].pages[0]
    page.elements["body"] = [FakeElement("职位列表")]
    assert session.wait_for_selector(".missing", timeout_ms=400) is False
    # 400ms 窗口 / 0.1s 粒度 → body 文字探测 ≥ 3 次（每片前各一次）
    assert page.text_probe_count >= 3


# ---- 双标签页模型 [Q22] ----


def test_dual_tabs_session_page_resident(db: Database, config: Config, profile) -> None:
    session, _, chromium = make_session(db, config, profile)
    session.start()
    ctx = chromium.contexts[0]

    session.ensure_session_tab(CHAT)
    assert len(ctx.pages) == 1
    session_page: FakePage = ctx.pages[0]
    assert session_page.nav_log == [CHAT]
    assert session.get_url() == CHAT

    # 搜索/详情在工作页进行
    session.switch_tab("work")
    assert len(ctx.pages) == 2
    work_page: FakePage = ctx.pages[1]
    assert session.goto(SEARCH) == SEARCH
    assert work_page.nav_log == [SEARCH]
    assert session_page.nav_log == [CHAT]  # 会话页零额外导航（轮询只读 DOM）

    # 会话页常驻：不可关闭；工作页可关闭重开
    with pytest.raises(BrowserSessionError, match="Q22"):
        session.close_tab("session")
    session.close_tab("work")
    assert work_page.closed is True
    assert len(ctx.pages) == 1
    session.switch_tab("work")  # 惰性重建工作页（会话页复用、不重开）
    assert ctx.new_page_count == 3  # 会话页 1 次 + 工作页 2 次

    # 重建后（重启）会话页须重新 ensure：直接 switch 被拒绝
    session.shutdown()
    session.start()
    with pytest.raises(BrowserSessionError, match="ensure_session_tab"):
        session.switch_tab("session")


# ---- TargetClosedError 自动重建限流 [A-CORE-25] ----


def test_rebuild_throttle(db: Database, config: Config, profile, clock: SteppableClock) -> None:
    """A-CORE-25：普通关闭 → 自动重建仅 1 次/小时；第 2 次拒绝（BROWSER_LOST
    高级告警 + 等待人工）；人工重新拉起不受限流影响。"""
    session, _, chromium = make_session(db, config, profile, clock=clock)
    session.start()
    session.switch_tab("work")
    session.goto(SEARCH)

    # 第 1 次关闭（10:00）→ 自动重建
    chromium.contexts[0].pages[0].kill()
    with pytest.raises(BrowserClosedError):
        session.goto(SEARCH2)
    assert session.running is True
    assert len(chromium.launch_calls) == 2

    # 重建后标签页重置：调用方重新初始化（工作页惰性重建）
    session.switch_tab("work")
    assert session.goto(SEARCH) == SEARCH
    page2: FakePage = chromium.contexts[1].pages[0]

    # 第 2 次关闭（10:10，1 小时内）→ 拒绝自动重建
    clock.advance(minutes=10)
    page2.kill()
    with pytest.raises(BrowserClosedError):
        session.goto(SEARCH2)
    assert session.running is False
    assert session.awaiting_manual_relaunch is True
    assert len(chromium.launch_calls) == 2  # 零额外重建
    assert _events_of(db, "BROWSER_LOST") == [
        (
            "high",
            {
                "reason": "throttled",
                "action": "pause_awaiting_manual_relaunch",
                "at": "2026-11-07T10:10:00.000Z",
                "last_auto_rebuild_at": "2026-11-07T10:00:00.000Z",
            },
        )
    ]

    # 等待人工期间原语一律拒绝
    with pytest.raises(BrowserClosedError):
        session.get_url()

    # 人工重新拉起（控制台『重新拉起浏览器』）——不计自动重建预算
    clock.advance(minutes=5)
    session.start()
    assert session.running is True
    assert session.awaiting_manual_relaunch is False
    assert len(chromium.launch_calls) == 3

    # 1 小时窗口滑过后（距上次自动重建 76min）自动重建恢复可用
    session.switch_tab("work")
    session.goto(SEARCH)
    page3: FakePage = chromium.contexts[2].pages[0]
    clock.advance(minutes=61)
    page3.kill()
    with pytest.raises(BrowserClosedError):
        session.goto(SEARCH2)
    assert session.running is True
    assert len(chromium.launch_calls) == 4


def test_rebuild_refused_when_risk_preceded(
    db: Database, config: Config, profile, clock: SteppableClock
) -> None:
    """A-CORE-25：关闭前出现过 EVICTED → 零重建，直接暂停等人工 [G2/M5]。"""
    session, _, chromium = make_session(db, config, profile, clock=clock)
    session.start()
    session.switch_tab("work")
    session.goto(SEARCH)
    page: FakePage = chromium.contexts[0].pages[0]

    page.url = "about:blank"  # Mock 注入驱逐
    assert session.check_health() is RiskSignal.EVICTED
    page.kill()  # 随后浏览器关闭（TargetClosedError 注入）
    with pytest.raises(BrowserClosedError):
        session.check_health()
    assert len(chromium.launch_calls) == 1  # 零重建
    assert session.running is False
    assert session.awaiting_manual_relaunch is True
    assert _events_of(db, "BROWSER_LOST") == [
        (
            "high",
            {
                "reason": "risk_preceded",
                "action": "pause_awaiting_manual_relaunch",
                "at": "2026-11-07T10:00:00.000Z",
                "signal": "EVICTED",
            },
        )
    ]

    # 恢复唯一路径 = 人工重新拉起（同时清除风控挂起）
    session.start()
    assert session.running is True
    assert session.awaiting_manual_relaunch is False


def test_rebuild_launch_failure_refuses(
    db: Database, config: Config, profile, clock: SteppableClock
) -> None:
    """自动重建时启动失败 → 同样 BROWSER_LOST + 等待人工。"""
    session, _, chromium = make_session(db, config, profile, clock=clock)
    session.start()
    session.switch_tab("work")
    session.goto(SEARCH)
    assert len(chromium.launch_calls) == 1

    chromium.contexts[0].pages[0].kill()
    chromium.fail_next_launch = True  # 注入重建启动失败
    with pytest.raises(BrowserClosedError):
        session.goto(SEARCH2)
    assert session.running is False
    assert session.awaiting_manual_relaunch is True
    reasons = [payload["reason"] for _, payload in _events_of(db, "BROWSER_LOST")]
    assert reasons == ["launch_failed"]


def test_manual_start_rejected_while_login_window_open(
    db: Database, config: Config, profile
) -> None:
    """登录窗口存活期间引擎启动被拒绝（同一 profile 不能双进程占用）[M3/G5]。"""
    recorder = FakeProcessRecorder()
    session, _, _ = make_session(
        db,
        config,
        profile,
        process_launcher=recorder.launch,
        chrome_executable_resolver=lambda channel: "C:/fake/chrome.exe",
    )
    session.launch_login_window()
    with pytest.raises(LoginWindowOpenError, match="请先关闭登录窗口"):
        session.start()
    recorder.procs[0].exit()  # 用户关闭登录窗口
    session.start()
    assert session.running is True


def test_browser_risk_error_carries_signal() -> None:
    err = BrowserRiskError(RiskSignal.SECURITY_CHECK, "detail-x")
    assert err.signal is RiskSignal.SECURITY_CHECK
    assert "SECURITY_CHECK" in str(err)
