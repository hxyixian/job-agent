"""Chrome 版本记录 [G4] + 主版本联动降档 [V9]（与 core/rampup.browse_tier 对接）。

V9 协议：S4b 检测到主版本变化 → 写 ``run_state.chrome_downgrade={"active",
"since", "chrome_major"}`` + CHROME_UPGRADED 中级事件；档位折算与 30 分钟
自动恢复由 :func:`job_agent.core.rampup.browse_tier` 承担（本测试验证对接）。
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from job_agent.browser.patchright_session import PatchrightSession
from job_agent.config import Config
from job_agent.core import dryrun, rampup, runstate
from job_agent.db import Database

from .conftest import SteppableClock, make_session
from .mock_browser import make_pw

V156 = "156.0.7000.99"
V157 = "157.0.8103.38"
V158 = "158.0.5200.60"


def _chrome_events(db: Database) -> list[tuple[str, dict[str, Any]]]:
    rows = db.query(
        "SELECT level, payload_json FROM notification_events"
        " WHERE event_type = 'CHROME_UPGRADED' ORDER BY id"
    )
    return [(str(r["level"]), json.loads(r["payload_json"])) for r in rows]


def test_first_run_records_version_without_downgrade(
    db: Database, config: Config, profile, clock: SteppableClock
) -> None:
    """首次运行（无基线）：仅记录版本 + 日志 [G4]，不触发降档/事件。"""
    session, _, _ = make_session(db, config, profile, clock=clock, versions=(V157,))
    session.start()
    assert runstate.get_state(db, "chrome_version") == {
        "major": 157,
        "full": V157,
        "recorded_at": "2026-11-07T10:00:00.000Z",
    }
    assert runstate.get_state(db, rampup.KEY_CHROME_DOWNGRADE) is None
    assert _chrome_events(db) == []


def test_major_change_triggers_downgrade_and_recovers_after_30min(
    db: Database, config: Config, profile, clock: SteppableClock
) -> None:
    """主版本变化（156→157）：写降档协议 + CHROME_UPGRADED 中级事件；
    rampup.browse_tier 折算第 1 档（15/时·60/天），30 分钟后自动恢复配置值。"""
    session, _, _ = make_session(db, config, profile, clock=clock, versions=(V156, V157))
    session.start()
    session.shutdown()
    clock.advance(minutes=1)
    session.start()  # Chrome 昨夜自动升级（v13 现实场景）

    assert runstate.get_state(db, rampup.KEY_CHROME_DOWNGRADE) == {
        "active": True,
        "since": "2026-11-07T10:01:00.000Z",
        "chrome_major": 157,
    }
    assert _chrome_events(db) == [
        (
            "medium",
            {
                "from_major": 156,
                "to_major": 157,
                "full": V157,
                "downgrade": "tier1_browse_15h_60d_recover_30min",
            },
        )
    ]

    # 与 I9 browse_tier 对接：正式模式下激活期 = 第 1 档（覆盖试运行档位）
    dryrun.set_dry_run(db, config, False)
    during = rampup.browse_tier(db, config, clock.now + timedelta(minutes=1))
    assert (during.tier, during.detail_hourly, during.detail_daily, during.source) == (
        1,
        15,
        60,
        "chrome_downgrade",
    )

    # 运行 30 分钟无风控信号 → 自动恢复配置值（browse_tier 惰性结算并置 inactive）
    after = rampup.browse_tier(db, config, clock.now + timedelta(minutes=31))
    assert (after.tier, after.detail_hourly, after.detail_daily, after.source) == (
        0,
        40,
        200,
        "config",
    )
    assert runstate.get_state(db, rampup.KEY_CHROME_DOWNGRADE)["active"] is False


def test_same_major_restart_does_not_retrigger(
    db: Database, config: Config, profile, clock: SteppableClock
) -> None:
    """同主版本重启（157→157）：不重复降档、不重复事件。"""
    session, _, _ = make_session(db, config, profile, clock=clock, versions=(V157, V157, V157))
    session.start()
    session.shutdown()
    clock.advance(hours=1)
    session.start()
    session.shutdown()
    clock.advance(hours=1)
    session.start()
    assert runstate.get_state(db, rampup.KEY_CHROME_DOWNGRADE) is None
    assert _chrome_events(db) == []


def test_second_major_change_rearms_downgrade(
    db: Database, config: Config, profile, clock: SteppableClock
) -> None:
    """再次主版本变化（157→158）重新计时降档。"""
    session, _, _ = make_session(db, config, profile, clock=clock, versions=(V157, V158))
    session.start()
    session.shutdown()
    clock.advance(minutes=2)
    session.start()
    dg = runstate.get_state(db, rampup.KEY_CHROME_DOWNGRADE)
    assert dg == {"active": True, "since": "2026-11-07T10:02:00.000Z", "chrome_major": 158}
    assert _chrome_events(db)[0][1]["to_major"] == 158


def test_version_bookkeeping_without_db(
    config: Config, profile, clock: SteppableClock, caplog: pytest.LogCaptureFixture
) -> None:
    """db 缺省（无装配场景）：版本照常记日志，不落库不报错。"""
    pw, _ = make_pw((V157,))
    session = PatchrightSession(
        config, None, profile_dir=profile, clock=clock, pw_provider=lambda: pw
    )
    with caplog.at_level("INFO", logger="job_agent.browser"):
        session.start()
    assert any("Chrome 版本" in rec.message for rec in caplog.records)
