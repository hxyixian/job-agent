"""EVICTED / SECURITY_CHECK 检测三形态 [G2/H1]（A-CORE-4/20 的逻辑层对应）。

- EVICTED：URL 变 about:blank、非我方发起的站外跳转；
- SECURITY_CHECK 形态 1：URL 含 _security_check / 验证类路径（verify）；
- SECURITY_CHECK 形态 2：页面出现"安全验证/环境异常/验证码/访问异常"类文字；
- SECURITY_CHECK 形态 3：导航后停留页非目标页**连续 2 次**；
- 检出后会话挂起：自动化原语拒绝、无自动重导航（导航计数不变）、
  2s 粒度轮询（watch）承担拟人延时期间的监测。
"""

from __future__ import annotations

import pytest

from job_agent.browser.strategy_base import BrowserRiskError
from job_agent.config import Config
from job_agent.db import Database
from job_agent.models import RiskSignal

from .conftest import make_session
from .mock_browser import FakeElement, FakePage

SEARCH = "https://www.zhipin.com/web/geek/job?query=python&city=101020100"
SEARCH2 = "https://www.zhipin.com/web/geek/job?query=ai&city=101020100"
CHAT = "https://www.zhipin.com/web/geek/chat"
HOME = "https://www.zhipin.com/"


def _started(db: Database, config: Config, profile) -> tuple:
    session, _, chromium = make_session(db, config, profile)
    session.start()
    session.switch_tab("work")
    session.goto(SEARCH)
    return session, chromium


# ---- EVICTED [G2 / A-CORE-4] ----


def test_evicted_about_blank(db: Database, config: Config, profile) -> None:
    """URL → about:blank：检出 EVICTED，全原语拒绝，导航计数不变（无自动重导航）。"""
    session, chromium = _started(db, config, profile)
    page: FakePage = chromium.contexts[0].pages[0]
    nav_before = list(page.nav_log)

    page.url = "about:blank"  # Mock 注入
    assert session.check_health() is RiskSignal.EVICTED
    assert session.last_signal is RiskSignal.EVICTED

    with pytest.raises(BrowserRiskError) as info:
        session.goto(SEARCH2)
    assert info.value.signal is RiskSignal.EVICTED
    with pytest.raises(BrowserRiskError):
        session.click(".job-card")
    with pytest.raises(BrowserRiskError):
        session.wait_for_selector(".job-card", timeout_ms=50)

    # 无自动重导航：会话层未发起任何新导航（A-CORE-4 期望"导航计数不变"）
    assert page.nav_log == nav_before

    # 探测/诊断仍可用（观察不属自动化）
    assert session.check_health() is RiskSignal.EVICTED
    assert session.get_url() == "about:blank"


def test_evicted_offsite_redirect(db: Database, config: Config, profile) -> None:
    """非我方发起的站外跳转 → EVICTED。"""
    session, chromium = _started(db, config, profile)
    chromium.contexts[0].pages[0].url = "https://evil.example.com/phish"
    assert session.check_health() is RiskSignal.EVICTED


# ---- SECURITY_CHECK URL 形态 [H1 / A-CORE-20] ----


def test_security_check_url_form(db: Database, config: Config, profile) -> None:
    session, chromium = _started(db, config, profile)
    page: FakePage = chromium.contexts[0].pages[0]

    page.url = "https://www.zhipin.com/web/common/_security_check?x=1"
    assert session.check_health() is RiskSignal.SECURITY_CHECK

    session2, chromium2 = _started(db, config, profile)
    chromium2.contexts[0].pages[0].url = "https://www.zhipin.com/web/user/verify?ka=x"
    assert session2.check_health() is RiskSignal.SECURITY_CHECK


def test_security_check_text_form(db: Database, config: Config, profile) -> None:
    """页面出现"安全验证"类文字 → SECURITY_CHECK（URL 正常）。"""
    session, chromium = _started(db, config, profile)
    page: FakePage = chromium.contexts[0].pages[0]
    page.elements["body"] = [FakeElement("访问异常，请完成安全验证后继续")]
    assert session.check_health() is RiskSignal.SECURITY_CHECK

    with pytest.raises(BrowserRiskError) as info:
        session.goto(SEARCH2)
    assert info.value.signal is RiskSignal.SECURITY_CHECK


def test_risk_words_variants(db: Database, config: Config, profile) -> None:
    """X4 验证口径四词：安全验证/环境异常/验证码/访问异常。"""
    for word in ("安全验证", "环境异常", "验证码", "访问异常"):
        session, chromium = _started(db, config, profile)
        chromium.contexts[0].pages[0].elements["body"] = [FakeElement(f"提示：{word}")]
        assert session.check_health() is RiskSignal.SECURITY_CHECK, word


# ---- SECURITY_CHECK 落点形态（连续 2 次非目标页）[H1 / A-CORE-20] ----


def test_security_check_landing_twice(db: Database, config: Config, profile) -> None:
    session, _, chromium = make_session(db, config, profile)
    session.start()
    session.switch_tab("work")
    ctx = chromium.contexts[0]
    ctx.landings[SEARCH] = HOME  # 导航弹回首页（拦截形态 3）

    first = session.goto(SEARCH)  # 第 1 次：仅计数，不熔断
    assert first == HOME
    assert session.last_signal is RiskSignal.NONE

    with pytest.raises(BrowserRiskError) as info:  # 第 2 次：连续非目标页 → 熔断
        session.goto(SEARCH)
    assert info.value.signal is RiskSignal.SECURITY_CHECK
    assert session.last_signal is RiskSignal.SECURITY_CHECK

    # 无自动重导航：两次 goto 之外会话层未发起任何导航
    page: FakePage = ctx.pages[0]
    assert page.nav_log == [SEARCH, SEARCH]


def test_landing_counter_resets_on_good_landing(db: Database, config: Config, profile) -> None:
    """中间夹一次正常落点 → 连续计数归零（不累计误熔断）。"""
    session, _, chromium = make_session(db, config, profile)
    session.start()
    session.switch_tab("work")
    ctx = chromium.contexts[0]

    ctx.landings[SEARCH] = HOME
    assert session.goto(SEARCH) == HOME  # bad ×1
    ctx.landings.pop(SEARCH)
    assert session.goto(SEARCH) == SEARCH  # good → 归零
    ctx.landings[SEARCH] = HOME
    assert session.goto(SEARCH) == HOME  # 仍是 bad ×1，不熔断
    with pytest.raises(BrowserRiskError):
        session.goto(SEARCH)  # bad ×2 → 熔断


def test_landing_validation_honors_custom_prefix(db: Database, config: Config, profile) -> None:
    """expect_url_prefix 显式指定时按其判定（S6 会话页首次导航等场景）。"""
    session, _, chromium = make_session(db, config, profile)
    session.start()
    ctx = chromium.contexts[0]
    session.ensure_session_tab(CHAT)  # 首次导航落点 = 目标页 → 通过
    # 显式前缀放宽：查询串差异不影响判定（默认前缀已去 query，此处验证可显式给路径段）
    session.switch_tab("work")
    ctx.landings[SEARCH] = "https://www.zhipin.com/web/geek/job?query=other"
    landing = session.goto(SEARCH, expect_url_prefix="https://www.zhipin.com/web/geek/job")
    assert landing.endswith("query=other")


# ---- 轮询检测 2s 粒度（watch）----


def test_watch_returns_signal_and_polls(db: Database, config: Config, profile) -> None:
    """watch 观察窗：健康时按粒度轮询满窗返回 NONE；检出立即返回。"""
    session, _, chromium = make_session(db, config, profile, poll_interval_s=0.05)
    session.start()
    session.switch_tab("work")
    session.goto(SEARCH)
    page: FakePage = chromium.contexts[0].pages[0]
    page.elements["body"] = [FakeElement("职位列表")]

    assert session.watch(0.2) is RiskSignal.NONE
    assert page.text_probe_count >= 3  # 0.2s / 0.05s 粒度

    page.url = "about:blank"
    assert session.watch(5.0, interval_s=0.05) is RiskSignal.EVICTED  # 立即返回


def test_risk_pending_until_manual_relaunch(db: Database, config: Config, profile) -> None:
    """M5 语义：挂起不自动恢复；唯一恢复路径 = 人工重新拉起（start）。"""
    session, _, chromium = make_session(db, config, profile)
    session.start()
    session.switch_tab("work")
    session.goto(SEARCH)
    page: FakePage = chromium.contexts[0].pages[0]

    page.url = "https://www.zhipin.com/web/common/_security_check"
    assert session.check_health() is RiskSignal.SECURITY_CHECK
    # 即便页面"恢复"（URL 回正常），挂起仍保持——无自动恢复
    page.url = SEARCH
    with pytest.raises(BrowserRiskError):
        session.goto(SEARCH2)

    session.shutdown()
    session.start()  # 人工重新拉起 → 清除挂起
    session.switch_tab("work")
    session.goto(SEARCH)
    assert session.get_url() == SEARCH
