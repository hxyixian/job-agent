"""S2d 放量/试运行状态机测试（§5.5 I9；A-CORE-17/21、M5、N2 隔离）。

时钟全部注入（T4-7 口径）："北京日 X 10:00" 统一表示为 UTC X 日 02:00
（北京 = UTC+8，无夏令时）。事件断言直接查 notification_events（占位协议）。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from job_agent.config import Config
from job_agent.core import dryrun, rampup
from job_agent.core.dryrun import count_real_applications, count_real_send_actions
from job_agent.db import Database, open_database
from job_agent.models import RiskSignal
from job_agent.timeutil import utc_str


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = open_database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture()
def config() -> Config:
    return Config()  # 出厂默认：dry_run=True、ramp_up 3 日/日限 10、浏览 40/200、首档 15/60


def bj(date_: date, hour: int = 10) -> datetime:
    """北京日 date_ 的 hour 点（默认 10:00）→ UTC datetime。"""
    return datetime(date_.year, date_.month, date_.day, hour - 8, tzinfo=UTC)


def real_mode(db: Database, config: Config) -> None:
    """关闭试运行（同时按 §13 启动放量期）。"""
    dryrun.set_dry_run(db, config, False)


def add_greeting(
    db: Database,
    at: datetime,
    *,
    target: str = "J1",
    dry: int = 0,
    state_: str = "sent",
) -> None:
    # 迁移 CHECK：skipped_dryrun 与 dry_run=1 严格绑定
    row_state = "skipped_dryrun" if dry == 1 else state_
    db.execute(
        "INSERT INTO send_intents (kind, target_ref, payload_digest, state, dry_run, created_at)"
        " VALUES ('greeting', ?, 'digest-x', ?, ?, ?)",
        (target, row_state, dry, utc_str(at)),
    )


def events_of(db: Database, event_type: str) -> list[tuple[str, dict[str, Any]]]:
    rows = db.query(
        "SELECT level, payload_json FROM notification_events WHERE event_type = ? ORDER BY id",
        (event_type,),
    )
    return [(str(r["level"]), json.loads(r["payload_json"])) for r in rows]


# ---- I9：is_dry_run 模式标志 ----


def test_is_dry_run_seeded_from_config_and_persists(tmp_path: Path, db: Database) -> None:
    assert dryrun.is_dry_run(db, Config()) is True  # 出厂默认
    # 键缺失时以配置播种：config 预置 dry_run=False 的路径
    db2 = open_database(tmp_path / "seed.db")
    try:
        cfg_off = Config.model_validate({"delivery": {"dry_run": False}})
        assert dryrun.is_dry_run(db2, cfg_off) is False
        # 播种后 run_state 为准：同一库即使换默认 True 的配置仍读到 False
        assert dryrun.is_dry_run(db2, Config()) is False
    finally:
        db2.close()


def test_set_dry_run_roundtrip(db: Database, config: Config) -> None:
    dryrun.set_dry_run(db, config, False)
    assert dryrun.is_dry_run(db, config) is False
    dryrun.set_dry_run(db, config, True)
    assert dryrun.is_dry_run(db, config) is True


# ---- I9：放量期状态机 [N1/G10/§13/A-CORE-17] ----


def test_disable_dry_run_starts_rampup(db: Database, config: Config) -> None:
    assert rampup.is_rampup_active(db, config) is False
    assert rampup.rampup_days_left(db, config) == 0
    assert rampup.effective_daily_limit(db, config, bj(date(2026, 11, 9))) == 100  # 配置值

    real_mode(db, config)  # §13：dry_run 关闭 → remaining_days=3
    assert rampup.is_rampup_active(db, config) is True
    assert rampup.rampup_days_left(db, config) == 3
    assert rampup.effective_daily_limit(db, config, bj(date(2026, 11, 9))) == 10


def test_effective_day_calendar_skips_blocked_day(db: Database, config: Config) -> None:
    """A-CORE-17：有效运行日日历——被限额拦截（无投递动作）的当日不计 [G10/Q20]。"""
    real_mode(db, config)
    d1, d2, d3, d4, d5 = (date(2026, 11, n) for n in range(9, 14))

    add_greeting(db, bj(d1))  # 第 1 日：有投递 → 有效日
    assert rampup.record_rampup_day_if_delivered(db, config, bj(d1)) is None
    assert rampup.rampup_days_left(db, config) == 2

    assert rampup.record_rampup_day_if_delivered(db, config, bj(d2)) is None  # 无投递（被拦）→ 不计
    assert rampup.rampup_days_left(db, config) == 2

    add_greeting(db, bj(d3))
    assert rampup.record_rampup_day_if_delivered(db, config, bj(d3)) is None
    assert rampup.rampup_days_left(db, config) == 1

    add_greeting(db, bj(d4))  # 第 3 个有效日 → 期满
    event = rampup.record_rampup_day_if_delivered(db, config, bj(d4))
    assert event is not None and event.event_type == "RAMPUP_DONE"
    assert rampup.rampup_days_left(db, config) == 0
    assert rampup.is_rampup_active(db, config) is False
    assert rampup.effective_daily_limit(db, config, bj(d4)) == 100  # 自动恢复配置值

    rows = events_of(db, "RAMPUP_DONE")
    assert len(rows) == 1
    level, payload = rows[0]
    assert level == "low"  # 裁决（2026-11-07）：以需求 FR3.9/N1"低级通知"为准，I5 已同步修正
    assert payload["source"] == "auto_recover"
    assert payload["restored_daily_limit"] == 100

    add_greeting(db, bj(d5))  # 期满后不再记录
    assert rampup.record_rampup_day_if_delivered(db, config, bj(d5)) is None
    assert len(events_of(db, "RAMPUP_DONE")) == 1


def test_effective_day_idempotent_within_day(db: Database, config: Config) -> None:
    real_mode(db, config)
    d1 = date(2026, 11, 9)
    add_greeting(db, bj(d1, 9))
    rampup.record_rampup_day_if_delivered(db, config, bj(d1, 10))
    rampup.record_rampup_day_if_delivered(db, config, bj(d1, 22))  # 同日再调 → 幂等
    assert rampup.rampup_days_left(db, config) == 2


def test_dry_run_greetings_do_not_count_as_effective_day(db: Database, config: Config) -> None:
    real_mode(db, config)
    d1 = date(2026, 11, 9)
    add_greeting(db, bj(d1), dry=1)  # 拟发送不是投递动作
    rampup.record_rampup_day_if_delivered(db, config, bj(d1))
    assert rampup.rampup_days_left(db, config) == 3


def test_effective_day_uses_beijing_calendar(db: Database, config: Config) -> None:
    """[R6-4] 有效日窗口 = 北京自然日（day_start_utc），不用 UTC 日期。"""
    real_mode(db, config)
    add_greeting(db, datetime(2026, 11, 7, 17, 0, tzinfo=UTC))  # 北京 11-08 01:00（UTC 日期 11-07）
    assert rampup.record_rampup_day_if_delivered(db, config, bj(date(2026, 11, 8), 10)) is None
    assert rampup.rampup_days_left(db, config) == 2  # 计入北京 11-08

    # 对照：北京 11-09 窗口（UTC 11-08T16:00Z 起）内无投递 → 不计
    add_greeting(db, datetime(2026, 11, 7, 15, 0, tzinfo=UTC))  # 北京 11-07 23:00
    assert rampup.record_rampup_day_if_delivered(db, config, bj(date(2026, 11, 9), 10)) is None
    assert rampup.rampup_days_left(db, config) == 2


def test_attempted_but_failed_delivery_counts_as_action(db: Database, config: Config) -> None:
    """当日发生过投递动作（即便 failed）即有效日——G10 口径是"动作发生"。"""
    real_mode(db, config)
    d1 = date(2026, 11, 9)
    add_greeting(db, bj(d1), state_="failed")
    rampup.record_rampup_day_if_delivered(db, config, bj(d1))
    assert rampup.rampup_days_left(db, config) == 2


def test_manual_end_and_extend(db: Database, config: Config) -> None:
    real_mode(db, config)

    # 手动提前结束 [FR3.9]
    event = rampup.end_rampup_early(db, config)
    assert event is not None
    assert events_of(db, "RAMPUP_DONE")[0][1]["source"] == "manual_early"
    assert rampup.is_rampup_active(db, config) is False
    assert rampup.effective_daily_limit(db, config, bj(date(2026, 11, 9))) == 100
    assert rampup.end_rampup_early(db, config) is None  # 幂等

    # 延长：done → active（剩余 = 新上限 − 已计日数）
    rampup.extend_rampup(db, config, 2)
    assert rampup.is_rampup_active(db, config) is True
    assert rampup.rampup_days_left(db, config) == 5  # 3+2 − 0


def test_extend_active_period_and_validation(db: Database, config: Config) -> None:
    real_mode(db, config)
    add_greeting(db, bj(date(2026, 11, 9)))
    rampup.record_rampup_day_if_delivered(db, config, bj(date(2026, 11, 9)))
    rampup.extend_rampup(db, config, 2)
    assert rampup.rampup_days_left(db, config) == 4  # (3+2) − 1

    with pytest.raises(ValueError, match="≥ 1"):
        rampup.extend_rampup(db, config, 0)


def test_extend_rejected_before_start(db: Database, config: Config) -> None:
    with pytest.raises(ValueError, match="尚未开始"):
        rampup.extend_rampup(db, config, 2)  # 试运行中（inactive）不可延长


def test_auto_recover_false_waits_for_manual_end(db: Database, config: Config) -> None:
    cfg = Config.model_validate({"delivery": {"ramp_up": {"auto_recover": False}}})
    real_mode(db, cfg)
    for n in (9, 10, 11):  # 3 个有效日
        add_greeting(db, bj(date(2026, 11, n)))
        assert rampup.record_rampup_day_if_delivered(db, cfg, bj(date(2026, 11, n))) is None
    assert rampup.rampup_days_left(db, cfg) == 0
    assert rampup.is_rampup_active(db, cfg) is True  # 期满不自动恢复
    assert rampup.effective_daily_limit(db, cfg, bj(date(2026, 11, 11))) == 10
    assert events_of(db, "RAMPUP_DONE") == []
    assert rampup.end_rampup_early(db, cfg) is not None  # 用户手动恢复
    assert rampup.effective_daily_limit(db, cfg, bj(date(2026, 11, 11))) == 100


def test_dry_run_toggle_keeps_rampup_progress(db: Database, config: Config) -> None:
    """试运行中途反复切换不重置放量进度（激活仅一次）。"""
    real_mode(db, config)
    add_greeting(db, bj(date(2026, 11, 9)))
    rampup.record_rampup_day_if_delivered(db, config, bj(date(2026, 11, 9)))
    dryrun.set_dry_run(db, config, True)
    dryrun.set_dry_run(db, config, False)
    assert rampup.rampup_days_left(db, config) == 2  # 不回到 3


def test_lazy_activation_when_config_preset_dry_run_off(db: Database) -> None:
    """配置文件预置 dry_run=false（未经 set_dry_run 切换）→ 读取时惰性激活放量期。"""
    cfg = Config.model_validate({"delivery": {"dry_run": False}})
    assert dryrun.is_dry_run(db, cfg) is False
    assert rampup.is_rampup_active(db, cfg) is True
    assert rampup.rampup_days_left(db, cfg) == 3


# ---- I9：browse_tier 试运行浏览档位 [H2/A-CORE-21] ----


def test_browse_tier_progression_by_clean_days(db: Database, config: Config) -> None:
    d1, d2, d3, d4 = (date(2026, 11, n) for n in (9, 10, 11, 12))

    t1 = rampup.browse_tier(db, config, bj(d1))  # 第 1 个试运行日：首档 15/60
    assert (t1.tier, t1.detail_hourly, t1.detail_daily, t1.source) == (1, 15, 60, "dryrun_rampup")
    assert rampup.browse_tier(db, config, bj(d1, 22)) == t1  # 同日稳定

    t2 = rampup.browse_tier(db, config, bj(d2))  # 无风控日 → 翻倍
    assert (t2.tier, t2.detail_hourly, t2.detail_daily) == (2, 30, 120)
    t3 = rampup.browse_tier(db, config, bj(d3))  # 60→封顶 40；240→封顶 200（逐项封顶）
    assert (t3.tier, t3.detail_hourly, t3.detail_daily) == (3, 40, 200)
    t4 = rampup.browse_tier(db, config, bj(d4))  # 已达配置值，档位不再变化
    assert (t4.tier, t4.detail_hourly, t4.detail_daily) == (4, 40, 200)


def test_browse_tier_config_values_outside_dry_run(db: Database, config: Config) -> None:
    real_mode(db, config)  # 放量期（正式运行）无试运行档位 → 配置值
    tier = rampup.browse_tier(db, config, bj(date(2026, 11, 9)))
    assert (tier.tier, tier.detail_hourly, tier.detail_daily, tier.source) == (0, 40, 200, "config")


def test_browse_tier_disabled_returns_config(db: Database) -> None:
    cfg = Config.model_validate({"delivery": {"browse_rampup": {"enabled": False}}})
    tier = rampup.browse_tier(db, cfg, bj(date(2026, 11, 9)))
    assert (tier.tier, tier.source, tier.detail_hourly, tier.detail_daily) == (0, "config", 40, 200)


def test_browse_tier_first_day_values_follow_config(db: Database) -> None:
    cfg = Config.model_validate(
        {"delivery": {"browse_rampup": {"first_day": {"detail_hourly": 10, "detail_daily": 40}}}}
    )
    tier = rampup.browse_tier(db, cfg, bj(date(2026, 11, 9)))
    assert (tier.detail_hourly, tier.detail_daily) == (10, 40)


# ---- M5：风控处置 [M5/G2/H1/A-CORE-17/A-CORE-21] ----


def test_risk_signal_resets_tier_and_pauses_awaiting_user(db: Database, config: Config) -> None:
    d1, d2, d3 = date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)
    rampup.browse_tier(db, config, bj(d1))
    assert rampup.browse_tier(db, config, bj(d2)).tier == 2

    event = rampup.handle_risk_signal(db, config, RiskSignal.SECURITY_CHECK, bj(d2, 12))
    assert (event.event_type, event.level) == ("SECURITY_CHECK", "high")
    assert event.payload["m5"] is True  # 试运行期间 → M5
    # 浏览档位回第 1 档 [H2]
    tier = rampup.browse_tier(db, config, bj(d2, 13))
    assert (tier.tier, tier.detail_hourly, tier.detail_daily) == (1, 15, 60)

    # M5：无自动恢复——后续任何读取都不解除暂停
    rampup.browse_tier(db, config, bj(d3))
    assert rampup.is_risk_paused(db) is True
    assert "M5" in (rampup.risk_pause_reason(db) or "")

    # 风控日结算：风控当日回第 1 档；其后的首个无风控日仍保持首档（翻倍依据
    # 是"前一个已完结试运行日无风控"），再下一个无风控日翻倍 [H2]
    assert rampup.browse_tier(db, config, bj(d3)).tier == 1
    assert rampup.browse_tier(db, config, bj(date(2026, 11, 12))).tier == 2

    # 恢复唯一路径：用户决策
    rampup.clear_risk_pause(db, decided_by="user")
    assert rampup.is_risk_paused(db) is False
    assert rampup.risk_pause_info(db)["resolved_by"] == "user"
    rampup.clear_risk_pause(db)  # 幂等
    assert rampup.is_risk_paused(db) is False


def test_risk_signal_event_mapping_and_str_coercion(db: Database, config: Config) -> None:
    now = bj(date(2026, 11, 9))
    rampup.handle_risk_signal(db, config, "EVICTED", now)  # 枚举 name 字符串
    rampup.handle_risk_signal(db, config, "captcha", now)  # 枚举 value 字符串
    types = [t for t, _ in events_of(db, "PAGE_EVICTED")] + [
        t for t, _ in events_of(db, "RISK_PAUSE")
    ]
    assert types == ["high", "high"]
    info = rampup.risk_pause_info(db)
    assert info is not None and info["signal"] == "CAPTCHA"  # 后发信号覆盖暂停详情


def test_risk_signal_m5_during_rampup_period(db: Database, config: Config) -> None:
    real_mode(db, config)  # 放量期（正式运行）
    event = rampup.handle_risk_signal(db, config, RiskSignal.RISK_PAGE, bj(date(2026, 11, 9)))
    assert event.event_type == "RISK_PAUSE"
    assert event.payload["m5"] is True  # 放量期间同样 M5
    assert rampup.is_risk_paused(db) is True


def test_non_risk_signals_rejected(db: Database, config: Config) -> None:
    now = bj(date(2026, 11, 9))
    for bad in (RiskSignal.PLATFORM_LIMIT, RiskSignal.LOGIN_EXPIRED, RiskSignal.ADAPTER_BROKEN):
        with pytest.raises(ValueError, match="非风控类"):
            rampup.handle_risk_signal(db, config, bad, now)
    with pytest.raises(ValueError, match="未知风控信号"):
        rampup.handle_risk_signal(db, config, "bogus", now)  # type: ignore[arg-type]


def test_risk_pause_clear_only_by_explicit_call(db: Database, config: Config) -> None:
    """M5 结构保证：除 clear_risk_pause 外无任何路径清除暂停（grep 复核见报告）。"""
    now = bj(date(2026, 11, 9))
    rampup.handle_risk_signal(db, config, RiskSignal.EVICTED, now)
    # 大量其它状态机操作后仍暂停
    rampup.browse_tier(db, config, bj(date(2026, 11, 10)))
    rampup.record_rampup_day_if_delivered(db, config, bj(date(2026, 11, 10)))
    assert rampup.is_risk_paused(db) is True


# ---- I9/V9：Chrome 降档覆盖 ----


def test_chrome_downgrade_override_active_and_expiry(db: Database, config: Config) -> None:
    now = bj(date(2026, 11, 9), 12)
    db.execute(
        "INSERT INTO run_state (key, value_json) VALUES (?, ?)",
        (
            "chrome_downgrade",
            json.dumps(
                {
                    "active": True,
                    "since": utc_str(now - timedelta(minutes=10)),
                    "chrome_major": 157,
                }
            ),
        ),
    )
    # 激活期（含正式模式）：第 1 档覆盖
    real_mode(db, config)
    tier = rampup.browse_tier(db, config, now)
    assert (tier.tier, tier.detail_hourly, tier.detail_daily, tier.source) == (
        1,
        15,
        60,
        "chrome_downgrade",
    )
    # 30 分钟无风控 → 自动恢复配置值（V9；与 M5 无关）
    later = now + timedelta(minutes=21)
    tier = rampup.browse_tier(db, config, later)
    assert (tier.tier, tier.source, tier.detail_hourly, tier.detail_daily) == (
        0,
        "config",
        40,
        200,
    )
    stored = json.loads(
        db.query_one("SELECT value_json FROM run_state WHERE key = 'chrome_downgrade'")[
            "value_json"
        ]
    )
    assert stored["active"] is False


# ---- N2：拟发送记录与隔离查询 ----


def test_planned_send_records_and_isolation(db: Database, config: Config) -> None:
    assert dryrun.is_dry_run(db, config) is True
    id1 = dryrun.record_planned_send(db, "greeting", "J1", "digest-a")
    id2 = dryrun.record_planned_send(db, "reply", "C1", "digest-b")
    assert id2 > id1 > 0

    rows = dryrun.planned_send_list(db)
    assert len(rows) == 2
    assert all(r["state"] == "skipped_dryrun" and r["dry_run"] == 1 for r in rows)
    assert rows[0]["id"] == id2  # 时间倒序（最新在前）
    assert [r["id"] for r in dryrun.planned_send_list(db, kind="greeting")] == [id1]
    assert [r["id"] for r in dryrun.planned_send_list(db, target_ref="C1")] == [id2]
    assert [r["id"] for r in dryrun.planned_send_list(db, limit=1)] == [id2]

    # [N2] 隔离：拟发送不计入正式口径
    assert count_real_send_actions(db, kind="greeting") == 0
    assert count_real_applications(db) == 0
    assert count_real_applications(db, company_key="c-1") == 0

    db.execute("INSERT INTO applications (job_id, company_key, dry_run) VALUES ('J9', 'c-1', 0)")
    db.execute("INSERT INTO applications (job_id, company_key, dry_run) VALUES ('J8', 'c-1', 1)")
    assert count_real_applications(db) == 1
    assert count_real_applications(db, company_key="c-1") == 1
    assert count_real_applications(db, job_id="J8") == 0  # 拟发送行不算


def test_real_send_actions_counts_only_real(db: Database, config: Config) -> None:
    at = bj(date(2026, 11, 9))
    db.execute(
        "INSERT INTO send_intents (kind, target_ref, payload_digest, state, dry_run, created_at)"
        " VALUES ('reply', 'C1', 'd', 'sent', 0, ?)",
        (utc_str(at),),
    )
    db.execute(
        "INSERT INTO send_intents (kind, target_ref, payload_digest, state, dry_run, created_at)"
        " VALUES ('reply', 'C1', 'd', 'skipped_dryrun', 1, ?)",
        (utc_str(at),),
    )
    assert count_real_send_actions(db, kind="reply") == 1
    assert count_real_send_actions(db, kind="reply", target_ref="C1") == 1
    assert count_real_send_actions(db, kind="reply", target_ref="C9") == 0
    assert count_real_send_actions(db, kind="greeting") == 0
