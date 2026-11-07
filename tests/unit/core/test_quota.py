"""S2b 配额单元测试（设计文档 §5.5 I1 / §6 计数推算 [G1] / §7 浏览 [G3]）。

覆盖任务书 S2b 验收点：
- 计数口径：state IN (pending, sent, unknown, skipped_dryrun)，不分 dry_run
  [G1]；failed 不计；
- unknown 计入限额（T4-1 崩溃恢复后不突破上限）；
- 同日切换 dry_run/正式模式，额度连续累计；
- 小时窗口 = UTC 滑动 60 分钟（时间差）；日窗口 = 北京自然日
  （起点 day_start_utc，禁止用 UTC 日期）[R6-4]——北京 07:59 与 08:01
  属于同一天；
- 浏览计数（详情/翻页，不分模式 [G3]）与事件修剪；
- min_interval_s 配置读取。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from job_agent.config import Config
from job_agent.core.quota import Quota, record_browse, used_quota
from job_agent.core.state_machine import SendStateMachine
from job_agent.db import Database, open_database
from job_agent.timeutil import CST


def insert_intent(
    db: Database,
    *,
    state: str = "sent",
    dry_run: int = 0,
    created_at: str,
    kind: str = "greeting",
) -> int:
    """直接插行，完全控制 created_at / state / dry_run（保证确定性）。"""
    return db.execute_returning_id(
        "INSERT INTO send_intents (kind, target_ref, payload_digest, state, dry_run, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (kind, "ref", "digest", state, dry_run, created_at),
    )


class DeliveryQuotaTests(unittest.TestCase):
    """used()：投递限额推算 [G1/R6-4]。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="job_agent_s2b_q_")
        self.addCleanup(self._tmp.cleanup)
        self.db = open_database(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.db.close)  # LIFO：先关连接再删临时目录（Windows 文件占用）
        self.quota = Quota(self.db, Config())

    def test_counted_states_regardless_of_mode(self) -> None:
        """四种状态计入、failed 不计、dry_run 不过滤 [G1]。"""
        now = datetime(2026, 11, 9, 4, 0, tzinfo=UTC)  # 北京 12:00
        at = "2026-11-09T03:30:00.000Z"  # 北京 11:30（同在小时/日两窗内）
        insert_intent(self.db, state="pending", created_at=at)
        insert_intent(self.db, state="sent", created_at=at)
        insert_intent(self.db, state="unknown", created_at=at)
        insert_intent(self.db, state="skipped_dryrun", dry_run=1, created_at=at)
        insert_intent(self.db, state="failed", created_at=at)  # 唯一不计
        self.assertEqual(self.quota.used("daily", now), 4)
        self.assertEqual(self.quota.used("hourly", now), 4)  # 同时也在 60min 窗内

    def test_kind_agnostic(self) -> None:
        """greeting/reply/resume 一并计入（含被动招呼 [D0]）。"""
        now = datetime(2026, 11, 9, 4, 0, tzinfo=UTC)
        at = "2026-11-09T03:30:00.000Z"
        insert_intent(self.db, kind="greeting", created_at=at)
        insert_intent(self.db, kind="reply", created_at=at)
        insert_intent(self.db, kind="resume", created_at=at)
        self.assertEqual(self.quota.used("hourly", now), 3)

    def test_unknown_after_crash_recovery_counted(self) -> None:
        """T4-1 集成：begin → 崩溃恢复（pending→unknown）→ 仍计入限额。"""
        sm = SendStateMachine(self.db)
        sm.begin("greeting", "job_1", "d", dry_run=False)
        sm.begin("reply", "c1", "d", dry_run=False)
        sm.startup_recover()  # 两笔 pending → unknown
        now = datetime.now(UTC)
        self.assertEqual(self.quota.used("hourly", now), 2)
        self.assertEqual(self.quota.used("daily", now), 2)

    def test_mode_switch_same_day_quota_continuous(self) -> None:
        """同日切 dry_run：额度连续累计 [G1/N2 边界]。"""
        # 上午试运行（skipped_dryrun）、下午正式（sent），同属北京 11-09
        insert_intent(
            self.db, state="skipped_dryrun", dry_run=1, created_at="2026-11-09T02:00:00.000Z"
        )  # 北京 10:00
        insert_intent(
            self.db, state="sent", dry_run=0, created_at="2026-11-09T06:00:00.000Z"
        )  # 北京 14:00
        now = datetime(2026, 11, 9, 7, 0, tzinfo=UTC)  # 北京 15:00
        self.assertEqual(self.quota.used("daily", now), 2)

    def test_hourly_sliding_window_boundary(self) -> None:
        """滑动 60 分钟窗口：端点计入（>=），早 1 秒不计；与自然日无关。"""
        now = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
        insert_intent(self.db, created_at="2026-11-09T11:00:00.000Z")  # 恰 60min 前
        insert_intent(self.db, created_at="2026-11-09T10:59:59.000Z")  # 60min+1s 前
        insert_intent(self.db, created_at="2026-11-09T11:59:59.000Z")  # 窗内
        self.assertEqual(self.quota.used("hourly", now), 2)

    def test_daily_window_beijing_0759_and_0801_same_day(self) -> None:
        """[R6-4] 北京 07:59（UTC 前日 23:59）与 08:01（UTC 当日 00:01）同一天。

        若误用 UTC 日期划分，UTC 前日 23:59 的记录会被漏计（只剩 1）。
        """
        insert_intent(self.db, created_at="2026-11-08T23:59:00.000Z")  # 北京 11-09 07:59
        insert_intent(self.db, created_at="2026-11-09T00:01:00.000Z")  # 北京 11-09 08:01
        now = datetime(2026, 11, 9, 0, 2, tzinfo=UTC)  # 北京 11-09 08:02
        self.assertEqual(self.quota.used("daily", now), 2)
        # 跨过 UTC 午夜的前一时刻查询：两笔已属同一（北京）日，仍然计 2
        self.assertEqual(self.quota.used("daily", datetime(2026, 11, 8, 23, 59, 30, tzinfo=UTC)), 2)

    def test_daily_rolls_at_beijing_midnight(self) -> None:
        """北京 23:59 与次日 00:01 分属两天（跨日窗口正确滚动）。"""
        insert_intent(self.db, created_at="2026-11-08T15:59:00.000Z")  # 北京 11-08 23:59
        insert_intent(self.db, created_at="2026-11-08T16:00:00.000Z")  # 北京 11-09 00:00
        just_after = datetime(2026, 11, 8, 16, 1, tzinfo=UTC)  # 北京 11-09 00:01
        self.assertEqual(self.quota.used("daily", just_after), 1)  # 仅北京 11-09 那笔
        before = datetime(2026, 11, 8, 15, 59, 30, tzinfo=UTC)  # 北京 11-08 23:59:30
        # 北京 11-08 的窗口起点 = UTC 11-07T16:00Z：两笔均计入（含"未来"的
        # 00:00 那笔——created_at >= 日起点即计入，对时钟回拨防御性从严）
        self.assertEqual(self.quota.used("daily", before), 2)

    def test_naive_now_treated_as_utc(self) -> None:
        """naive datetime 按 UTC 处理（与 timeutil 契约一致）。"""
        insert_intent(self.db, created_at="2026-11-09T03:00:00.000Z")
        self.assertEqual(self.quota.used("hourly", datetime(2026, 11, 9, 3, 30)), 1)

    def test_beijing_tz_now_accepted(self) -> None:
        """带北京时区的 now 同样正确换算。"""
        insert_intent(self.db, created_at="2026-11-09T02:00:00.000Z")  # 北京 10:00
        now = datetime(2026, 11, 9, 12, 0, tzinfo=CST)  # 北京中午 = UTC 04:00
        self.assertEqual(self.quota.used("daily", now), 1)

    def test_invalid_window_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.quota.used("weekly", datetime(2026, 11, 9, tzinfo=UTC))  # type: ignore[arg-type]


class BrowseQuotaTests(unittest.TestCase):
    """browse_used()/record_browse()：浏览计数 [G3]。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="job_agent_s2b_b_")
        self.addCleanup(self._tmp.cleanup)
        self.db = open_database(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.db.close)
        self.quota = Quota(self.db, Config())

    def test_detail_hourly_and_daily_windows(self) -> None:
        now = datetime(2026, 11, 9, 4, 0, tzinfo=UTC)  # 北京 12:00（当日起点=UTC 11-08T16:00Z）
        self.quota.record_browse("detail", now - timedelta(minutes=30))  # 窗内
        self.quota.record_browse("detail", now - timedelta(minutes=60))  # 恰在端点
        self.quota.record_browse("detail", now - timedelta(minutes=90))  # 今日但出小时窗
        self.quota.record_browse("detail", now - timedelta(minutes=1))  # 窗内
        self.assertEqual(self.quota.browse_used("detail_hourly", now), 3)
        self.assertEqual(self.quota.browse_used("detail_daily", now), 4)

    def test_daily_excludes_yesterday_after_rollover(self) -> None:
        """前一日事件在跨北京日后不再计入日窗；小时窗跨日滑动照常计入。"""
        self.quota.record_browse(
            "detail", datetime(2026, 11, 8, 15, 30, tzinfo=UTC)
        )  # 北京 11-08 23:30
        self.quota.record_browse(
            "detail", datetime(2026, 11, 8, 23, 45, tzinfo=UTC)
        )  # 北京 11-09 07:45
        now = datetime(2026, 11, 9, 0, 30, tzinfo=UTC)  # 北京 11-09 08:30
        self.quota.record_browse("detail", now)
        # 日窗：昨夜 23:30（北京 11-08）已出窗，07:45/08:30（北京 11-09）计入
        self.assertEqual(self.quota.browse_used("detail_daily", now), 2)
        # 小时窗：[-60min, now] 覆盖北京 07:45（30min 前）与 08:30，滑动跨 UTC 午夜无碍
        self.assertEqual(self.quota.browse_used("detail_hourly", now), 2)

    def test_search_pages_counted_separately(self) -> None:
        """翻页计数独立于详情（search_page_hourly [G3]）。"""
        now = datetime(2026, 11, 9, 4, 0, tzinfo=UTC)
        self.quota.record_browse("search", now - timedelta(minutes=5))
        self.quota.record_browse("search", now - timedelta(minutes=10))
        self.quota.record_browse("detail", now - timedelta(minutes=5))
        self.assertEqual(self.quota.browse_used("search_page_hourly", now), 2)
        self.assertEqual(self.quota.browse_used("detail_hourly", now), 1)

    def test_pruning_keeps_storage_bounded(self) -> None:
        """前一日事件被修剪：run_state 不无限膨胀（当日事件保留供日窗）。"""
        base = datetime(2026, 11, 9, 4, 0, tzinfo=UTC)  # 北京 11-09 12:00
        # 50 笔北京 11-08 晚间事件（前一自然日，日窗不再需要）
        for i in range(50):
            self.quota.record_browse("detail", base - timedelta(hours=13, minutes=i))
        self.quota.record_browse("detail", base)  # 北京 11-09 当日
        row = self.db.query_one(
            "SELECT value_json FROM run_state WHERE key = 'browse_events_detail'"
        )
        events = json.loads(row["value_json"])
        # 前一日事件全部修剪，只保留当日这条
        self.assertEqual(len(events), 1)
        self.assertEqual(self.quota.browse_used("detail_daily", base), 1)

    def test_invalid_browse_window_rejected(self) -> None:
        now = datetime(2026, 11, 9, 4, 0, tzinfo=UTC)
        with self.assertRaises(ValueError):
            self.quota.browse_used("detail_weekly", now)  # type: ignore[arg-type]


class MinIntervalTests(unittest.TestCase):
    """min_interval_s()：操作级最小间隔 [H2]。"""

    def _make_quota(self, cfg: Config) -> Quota:
        tmp = tempfile.TemporaryDirectory(prefix="job_agent_s2b_mi_")
        self.addCleanup(tmp.cleanup)
        db = open_database(Path(tmp.name) / "t.db")
        self.addCleanup(db.close)  # LIFO：先关连接再删临时目录（Windows 文件占用）
        return Quota(db, cfg)

    def test_defaults(self) -> None:
        quota = self._make_quota(Config())
        self.assertEqual(quota.min_interval_s("detail"), 30.0)
        self.assertEqual(quota.min_interval_s("search"), 30.0)

    def test_custom_config(self) -> None:
        cfg = Config.model_validate({"delivery": {"min_interval_s": {"detail": 45, "search": 60}}})
        quota = self._make_quota(cfg)
        self.assertEqual(quota.min_interval_s("detail"), 45.0)
        self.assertEqual(quota.min_interval_s("search"), 60.0)

    def test_invalid_action_rejected(self) -> None:
        quota = self._make_quota(Config())
        with self.assertRaises(ValueError):
            quota.min_interval_s("scroll")  # type: ignore[arg-type]


class CurrentLimitsTests(unittest.TestCase):
    """current_limits()：折算组合（H2 档位 / 放量期日限；折算本体在 S2d）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="job_agent_s2b_cl_")
        self.addCleanup(self._tmp.cleanup)
        self.db = open_database(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.db.close)  # LIFO：先关连接再删临时目录（Windows 文件占用）
        self.quota = Quota(self.db, Config())

    def test_dry_run_first_day_tier(self) -> None:
        """试运行首日：浏览=第 1 档（15/60），投递日限=配置值（未进放量期）。"""
        now = datetime(2026, 11, 9, 4, 0, tzinfo=UTC)
        limits = self.quota.current_limits(now)
        self.assertEqual(limits.detail_hourly, 15)
        self.assertEqual(limits.detail_daily, 60)
        self.assertEqual(limits.daily_limit, 100)  # 配置值（dry_run 期间无放量折算）
        self.assertEqual(limits.hourly_limit, 25)  # 时限不参与折算
        self.assertEqual(limits.search_page_hourly, 30)  # 翻页无档位

    def test_after_dry_run_disabled_delivery_daily_folded(self) -> None:
        """关闭试运行 → 放量期：日限=ramp_up.daily_limit（只收紧），浏览回配置值。"""
        from job_agent.core.dryrun import set_dry_run

        set_dry_run(self.db, Config(), False)
        now = datetime(2026, 11, 9, 4, 0, tzinfo=UTC)
        limits = self.quota.current_limits(now)
        self.assertEqual(limits.daily_limit, 10)  # min(ramp_up 10, 配置 100)
        self.assertEqual(limits.detail_hourly, 40)  # 非试运行日：配置值直读
        self.assertEqual(limits.detail_daily, 200)

    def test_module_level_entry(self) -> None:
        import job_agent.core.quota as quota_mod

        quota_mod.configure(self.db, Config())
        limits = quota_mod.current_limits(datetime(2026, 11, 9, 4, 0, tzinfo=UTC))
        self.assertIsInstance(limits, quota_mod.Limits)
        self.assertEqual(limits.detail_hourly, 15)


class ModuleLevelApiTests(unittest.TestCase):
    """I1 契约签名的模块级入口（configure 装配后可用）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="job_agent_s2b_mod_")
        self.addCleanup(self._tmp.cleanup)
        self.db = open_database(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.db.close)

    def test_module_functions_delegate_to_configured_instance(self) -> None:
        import job_agent.core.quota as quota_mod

        insert_intent(self.db, created_at="2026-11-09T02:00:00.000Z")
        quota_mod.configure(self.db, Config())
        now = datetime(2026, 11, 9, 2, 30, tzinfo=UTC)
        self.assertEqual(used_quota("hourly", now), 1)
        self.assertEqual(quota_mod.browse_used("detail_daily", now), 0)
        record_browse("detail", now)
        self.assertEqual(quota_mod.browse_used("detail_daily", now), 1)
        self.assertEqual(quota_mod.min_interval_s("search"), 30.0)

    def test_not_configured_raises(self) -> None:
        import job_agent.core.quota as quota_mod

        saved = quota_mod._default
        quota_mod._default = None  # 测试内复位模块装配状态（还原于 finally）
        try:
            with self.assertRaises(RuntimeError):
                quota_mod.used_quota("daily", datetime(2026, 11, 9, tzinfo=UTC))
        finally:
            quota_mod._default = saved


if __name__ == "__main__":
    unittest.main()
