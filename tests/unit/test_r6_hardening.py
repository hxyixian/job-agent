"""第 6 轮审阅 R6-1~R6-5 加固测试。

覆盖：
- R6-1 非法配置 8 组全部拒绝 + 边界接受 + safety_warnings；
- R6-2 路径锚定应用根（换工作目录不变）；
- R6-3 库层约束（正式投递唯一/双 current 拒绝/F1 同步列/skipped_dryrun 绑定）；
- R6-4 timeutil（北京 07:59 与 08:01 属同一天）；
- R6-5 __main__ 冒烟（返回 0 且生成 logs/app.log）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from job_agent.config import Config, app_root, default_config_path, resolve_path, safety_warnings
from job_agent.db import open_database
from job_agent.timeutil import CST, beijing_date_str, day_start_utc


class R61ConfigValidation(unittest.TestCase):
    """评审构造的 8 组非法配置 + 边界。"""

    def _must_reject(self, patch: dict) -> None:
        base = Config().model_dump(mode="json")
        merged = json.loads(json.dumps(base))
        for key_path, value in patch.items():
            node = merged
            keys = key_path.split(".")
            for k in keys[:-1]:
                node = node[k]
            node[keys[-1]] = value
        with self.assertRaises(ValidationError):
            Config.model_validate(merged)

    def test_console_host_not_configurable(self) -> None:
        self._must_reject({"console.host": "0.0.0.0"})

    def test_min_interval_negative(self) -> None:
        self._must_reject({"delivery.min_interval_s.detail": -5})

    def test_delay_range_zero(self) -> None:
        self._must_reject({"delivery.action_delay_range_s": [0, 0]})

    def test_hourly_limit_negative(self) -> None:
        self._must_reject({"delivery.hourly_limit": -1})

    def test_min_score_out_of_range(self) -> None:
        self._must_reject({"ai.min_score": 150})

    def test_feishu_levels_missing_high(self) -> None:
        self._must_reject({"notify.feishu.levels": ["low"]})

    def test_adapter_typo(self) -> None:
        self._must_reject({"site.adapter": "typo"})

    def test_resume_limit_over_one(self) -> None:
        self._must_reject({"messaging.resume_auto_send_limit_per_conv": 5})

    def test_boundaries_accepted(self) -> None:
        cfg = Config.model_validate(
            {
                "delivery": {"min_interval_s": {"detail": 1, "search": 1}},
                "ai": {"min_score": 0},
            }
        )
        self.assertEqual(cfg.ai.min_score, 0)

    def test_safety_warnings(self) -> None:
        cfg = Config.model_validate({"delivery": {"min_interval_s": {"detail": 10}}})
        warns = safety_warnings(cfg)
        self.assertTrue(any("detail=10" in w for w in warns))
        self.assertEqual(safety_warnings(Config()), [])


class R62AppRootPaths(unittest.TestCase):
    def test_paths_anchor_to_app_root(self) -> None:
        root = app_root()
        self.assertEqual(default_config_path(), root / "config.yaml")
        self.assertEqual(resolve_path("data/agent.db"), root / "data" / "agent.db")

    def test_cwd_independence(self) -> None:
        before = resolve_path("data/agent.db")
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                self.assertEqual(resolve_path("data/agent.db"), before)
            finally:
                os.chdir(cwd)

    def test_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["JOB_AGENT_HOME"] = tmp
            try:
                self.assertEqual(app_root(), Path(tmp).resolve())
            finally:
                del os.environ["JOB_AGENT_HOME"]


class R63DbConstraints(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="job_agent_r6_")
        self.addCleanup(self._tmp.cleanup)
        self.db = open_database(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.db.close)  # LIFO：先关连接再删临时目录（Windows 文件占用）

    def test_unique_real_application(self) -> None:
        sql = "INSERT INTO applications (job_id, company_key, dry_run) VALUES (?, ?, 0)"
        self.db.execute(sql, ("j1", "c1"))
        with self.assertRaises(Exception):  # noqa: B017 - sqlite3.IntegrityError 等库层异常
            self.db.execute(sql, ("j1", "c2"))
        # 试运行行不受约束
        self.db.execute(
            "INSERT INTO applications (job_id, company_key, dry_run) VALUES ('j1', 'c1', 1)"
        )

    def test_single_current_greeting(self) -> None:
        ins = "INSERT INTO greeting_templates (kind, content, version, is_current) VALUES (?,?,?,1)"
        self.db.execute(ins, ("active", "v1", 1))
        with self.assertRaises(Exception):  # noqa: B017
            self.db.execute(ins, ("active", "v2", 2))

    def test_single_current_resume(self) -> None:
        self.db.execute(
            "INSERT INTO resume_snapshots (structured_json, is_current) VALUES ('{}', 1)"
        )
        with self.assertRaises(Exception):  # noqa: B017
            self.db.execute(
                "INSERT INTO resume_snapshots (structured_json, is_current) VALUES ('{}', 1)"
            )

    def test_greeting_sync_columns(self) -> None:
        self.db.execute_returning_id(
            "INSERT INTO greeting_templates (kind, content, version, is_current,"
            " platform_synced_hash, platform_synced_at)"
            " VALUES ('active', 'text', 1, 1, 'h1', '2026')"
        )
        row = self.db.query_one(
            "SELECT platform_synced_hash, platform_synced_at"
            " FROM greeting_templates WHERE kind='active'"
        )
        self.assertEqual(row["platform_synced_hash"], "h1")

    def test_skipped_dryrun_binding(self) -> None:
        with self.assertRaises(Exception):  # noqa: B017
            self.db.execute(
                "INSERT INTO send_intents (kind, target_ref, payload_digest, state, dry_run)"
                " VALUES ('reply', 'c1', 'd', 'skipped_dryrun', 0)"
            )
        with self.assertRaises(Exception):  # noqa: B017
            self.db.execute(
                "INSERT INTO send_intents (kind, target_ref, payload_digest, state, dry_run)"
                " VALUES ('reply', 'c1', 'd', 'pending', 1)"
            )


class R64Timeutil(unittest.TestCase):
    def test_beijing_day_boundary(self) -> None:
        """北京 07:59 与 08:01（UTC 前一天 23:59 / 当天 00:01）属同一天。"""
        early = datetime(2026, 11, 8, 7, 59, tzinfo=CST)
        late = datetime(2026, 11, 8, 8, 1, tzinfo=CST)
        self.assertEqual(day_start_utc(early), day_start_utc(late))

    def test_day_start_value(self) -> None:
        now = datetime(2026, 11, 8, 2, 0, tzinfo=CST)  # 北京 11-08 02:00
        self.assertEqual(day_start_utc(now), "2026-11-07T16:00:00.000Z")

    def test_utc_midnight_split(self) -> None:
        """北京日分界：23:59 与次日 00:01 分属两天。"""
        late_night = datetime(2026, 11, 8, 23, 59, tzinfo=CST)
        next_morning = datetime(2026, 11, 9, 0, 1, tzinfo=CST)
        self.assertNotEqual(beijing_date_str(late_night), beijing_date_str(next_morning))
        self.assertNotEqual(day_start_utc(late_night), day_start_utc(next_morning))


class R65MainSmoke(unittest.TestCase):
    def test_main_banner_and_log(self) -> None:
        """子进程运行入口：退出码 0 + 生成 logs/app.log（锚定应用根）[R6-2/R6-5]。"""
        log_dir = app_root() / "logs"
        marker = log_dir / "app.log"
        existed = marker.exists()
        proc = subprocess.run(
            [sys.executable, "-m", "job_agent"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=tempfile.gettempdir(),  # 换工作目录启动 [R6-2]
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("job-agent", proc.stdout)
        self.assertTrue(marker.exists())
        if not existed:
            marker.unlink(missing_ok=True)  # 清理本次冒烟产物


if __name__ == "__main__":
    unittest.main()
