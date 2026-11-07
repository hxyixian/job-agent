"""S1b 数据库层单元测试（全部离线，测试库一律使用临时目录，绝不写 data/agent.db）。

用标准库 unittest 编写（当前离线环境无 pytest）；pytest 亦可直接运行本文件
（``pytest tests/unit/test_db.py`` 对 unittest.TestCase 完全兼容），S1a 集成后无需改动。

覆盖任务书 S1b 验收点：
- 迁移幂等（跑两遍）与"空库/已有库"两态执行；失败整体回滚；
- WAL 生效（含持久化）；foreign_keys=ON 生效（约束拒绝 + 级联删除）；
- 每表 insert/select 冒烟（15 表，含 G1 的 skipped_dryrun、G7 的 user_label）；
- 线程并发串行化、事务回滚。
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from typing import ClassVar

from job_agent.db import Database, MigrationError, open_database, run_migrations

# src/ 导入兜底由 tests/conftest.py 统一提供（S1c 交付），此处不再重复。
# 用标准库 unittest 编写，pytest 对 unittest.TestCase 完全兼容。

# §6 的 15 张业务表 + 迁移执行器自建表
BUSINESS_TABLES = [
    "companies",
    "jobs",
    "evaluations",
    "applications",
    "send_intents",
    "conversations",
    "messages",
    "msg_routing",
    "outbox",
    "notification_events",
    "notify_dedup",
    "greeting_templates",
    "resume_snapshots",
    "job_directions",
    "run_state",
]
ALL_TABLES = [*BUSINESS_TABLES, "schema_migrations"]

TS = "2026-11-07T10:00:00.000Z"


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def seed(db: Database) -> None:
    """按外键安全顺序向全部 15 表各插一条代表性数据。"""
    db.execute(
        "INSERT INTO companies (company_key, name, mode, blocked_until, source)"
        " VALUES ('ck_1', 'Acme', 'cooldown', ?, 'applied')",
        (TS,),
    )
    db.execute(
        "INSERT INTO jobs (job_id, title, company_key, detail_json)"
        " VALUES ('j1', 'Python工程师', 'ck_1', '{}')"
    )
    db.execute(
        "INSERT INTO evaluations (job_id, score, verdict, reason, skipped_by, user_label, dry_run)"
        " VALUES ('j1', 88, 'recommend', '匹配度高', NULL, 'want', 0)"
    )
    db.execute(
        "INSERT INTO evaluations (job_id, score, verdict, reason, skipped_by, user_label, dry_run)"
        " VALUES ('j1', NULL, NULL, NULL, 'low_score', NULL, 0)"
    )
    db.execute(
        "INSERT INTO applications"
        " (job_id, company_key, greeting_version, eval_id, status, dry_run, sent_at)"
        " VALUES ('j1', 'ck_1', 1, 1, 'sent', 0, ?)",
        (TS,),
    )
    # [G1] 试运行拟发送 → state=skipped_dryrun
    db.execute(
        "INSERT INTO send_intents (kind, target_ref, payload_digest, state, dry_run)"
        " VALUES ('greeting', 'j1', 'digest-1', 'skipped_dryrun', 1)"
    )
    db.execute(
        "INSERT INTO conversations (conv_id, peer_name, job_id, is_baseline, manual_takeover,"
        " auto_sent_count, resume_sent_count, blocked_reason) "
        "VALUES ('c1', 'HR小王', 'j1', 0, 0, 2, 1, NULL)"
    )
    db.execute(
        "INSERT INTO messages (platform_msg_id, conv_id, direction, sender, msg_type, text,"
        " sent_at, is_ours_auto) VALUES ('m1', 'c1', 'in', 'hr', 'TEXT', '你好', ?, NULL)",
        (TS,),
    )
    db.execute(
        "INSERT INTO msg_routing (platform_msg_id, category, action)"
        " VALUES ('m1', 'low_risk', 'auto_reply')"
    )
    db.execute(
        "INSERT INTO outbox (conv_id, draft_text, source, state)"
        " VALUES ('c1', '您好，我对该岗位很感兴趣', 'ai', 'pending')"
    )
    db.execute(
        "INSERT INTO notification_events (event_type, level, payload_json)"
        " VALUES ('interview_invite', 'medium', '{}')"
    )
    db.execute(
        "INSERT INTO notify_dedup (event_type, window_start, count) VALUES ('daily_summary', ?, 3)",
        (TS,),
    )
    db.execute(
        "INSERT INTO greeting_templates (kind, content, version, is_current)"
        " VALUES ('active', '您好', 1, 1)"
    )
    db.execute(
        "INSERT INTO resume_snapshots (structured_json, source_pdf_hash, is_current)"
        " VALUES ('{\"education\": []}', 'sha256:ab12', 1)"
    )
    db.execute(
        "INSERT INTO job_directions (keywords_json, rationale, is_confirmed)"
        " VALUES ('[\"Python\"]', '后端方向', 1)"
    )
    db.execute("INSERT INTO run_state (key, value_json) VALUES ('dry_run', 'true')")


class DbTestCase(unittest.TestCase):
    """提供临时目录中的独立测试库（每个用例全新数据库）。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="job_agent_s1b_test_")
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.db_path = self.tmp_path / "agent.db"

    def _open(self) -> Database:
        db = open_database(self.db_path)
        self.addCleanup(db.close)
        return db


class MigrationTests(DbTestCase):
    def test_migrations_apply_on_empty_db(self) -> None:
        applied = run_migrations(self.db_path)
        self.assertEqual(applied, [1])
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(table_names(conn), set(ALL_TABLES))
        finally:
            conn.close()

    def test_migrations_idempotent_rerun(self) -> None:
        run_migrations(self.db_path)
        applied_again = run_migrations(self.db_path)
        self.assertEqual(applied_again, [])
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(table_names(conn), set(ALL_TABLES))
            versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
            self.assertEqual(versions, [1])
        finally:
            conn.close()

    def test_migrations_on_existing_db_preserve_data(self) -> None:
        db = self._open()
        seed(db)
        db.close()
        applied = run_migrations(self.db_path)  # 已有库（含数据）再执行
        self.assertEqual(applied, [])
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM run_state").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0], 2)
        finally:
            conn.close()

    def test_failed_migration_rolls_back_atomically(self) -> None:
        mig = self.tmp_path / "fake_migrations"
        mig.mkdir()
        (mig / "0001_ok.sql").write_text("CREATE TABLE t1 (x INTEGER);", encoding="utf-8")
        (mig / "0002_bad.sql").write_text(
            "CREATE TABLE t2 (x INTEGER);\nINSERT INTO t2 VALUES (1);\n"
            "INSERT INTO no_such_table VALUES (1);",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(MigrationError, "0002_bad"):
            run_migrations(self.db_path, mig)
        conn = sqlite3.connect(self.db_path)
        try:
            names = table_names(conn)
            self.assertIn("t1", names)  # 0001 已提交
            self.assertNotIn("t2", names)  # 0002 整体回滚（含已执行的 CREATE/INSERT）
            versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
            self.assertEqual(versions, [1])
        finally:
            conn.close()

    def test_duplicate_migration_version_rejected(self) -> None:
        mig = self.tmp_path / "fake_migrations"
        mig.mkdir()
        (mig / "0001_a.sql").write_text("CREATE TABLE t1 (x INTEGER);", encoding="utf-8")
        (mig / "0001_b.sql").write_text("CREATE TABLE t2 (x INTEGER);", encoding="utf-8")
        with self.assertRaisesRegex(MigrationError, "duplicate"):
            run_migrations(self.db_path, mig)

    def test_missing_migrations_dir_rejected(self) -> None:
        with self.assertRaisesRegex(MigrationError, "not found"):
            run_migrations(self.db_path, self.tmp_path / "nope")


class PragmaTests(DbTestCase):
    def test_wal_mode_enabled_and_persistent(self) -> None:
        run_migrations(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(str(mode).lower(), "wal")  # journal_mode 持久化于库文件
        finally:
            conn.close()
        db = self._open()  # 每次连接重新应用 PRAGMA
        self.assertEqual(str(db.query_one("PRAGMA journal_mode")[0]).lower(), "wal")

    def test_foreign_keys_pragma_and_enforcement(self) -> None:
        db = self._open()
        self.assertEqual(db.query_one("PRAGMA foreign_keys")[0], 1)
        # conv_id 外键：向不存在会话插消息必须被拒绝
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO messages (platform_msg_id, conv_id, direction, sender, msg_type,"
                " text, sent_at) VALUES ('mX', 'no_such_conv', 'in', 'hr', 'TEXT', 'x', ?)",
                (TS,),
            )

    def test_foreign_key_cascade_delete(self) -> None:
        db = self._open()
        seed(db)
        db.execute("DELETE FROM conversations WHERE conv_id = 'c1'")
        self.assertEqual(db.query_one("SELECT COUNT(*) FROM messages")[0], 0)
        self.assertEqual(db.query_one("SELECT COUNT(*) FROM msg_routing")[0], 0)
        self.assertEqual(db.query_one("SELECT COUNT(*) FROM outbox")[0], 0)


class TableSmokeTests(DbTestCase):
    # (表, 关键列, 关键值, 预期行数)
    CASES: ClassVar[list[tuple[str, str, str, int]]] = [
        ("companies", "company_key", "ck_1", 1),
        ("jobs", "job_id", "j1", 1),
        ("evaluations", "job_id", "j1", 2),
        ("applications", "job_id", "j1", 1),
        ("send_intents", "target_ref", "j1", 1),
        ("conversations", "conv_id", "c1", 1),
        ("messages", "platform_msg_id", "m1", 1),
        ("msg_routing", "platform_msg_id", "m1", 1),
        ("outbox", "conv_id", "c1", 1),
        ("notification_events", "event_type", "interview_invite", 1),
        ("notify_dedup", "event_type", "daily_summary", 1),
        ("greeting_templates", "kind", "active", 1),
        ("resume_snapshots", "source_pdf_hash", "sha256:ab12", 1),
        ("job_directions", "keywords_json", '["Python"]', 1),
        ("run_state", "key", "dry_run", 1),
    ]

    def test_table_insert_select_smoke(self) -> None:
        """15 张表逐表 insert/select 冒烟（subTest 逐表报告）。"""
        db = self._open()
        seed(db)
        for table, key_col, key_value, expected in self.CASES:
            with self.subTest(table=table):
                rows = db.query(f"SELECT * FROM {table} WHERE {key_col} = ?", (key_value,))
                self.assertEqual(len(rows), expected)
                self.assertTrue(len(rows[0]) > 0)

    def test_g1_skipped_dryrun_and_g7_user_label_roundtrip(self) -> None:
        """[G1] send_intents.state=skipped_dryrun 与 [G7] evaluations.user_label 落库可回读。"""
        db = self._open()
        seed(db)
        intent = db.query_one("SELECT state, dry_run FROM send_intents WHERE target_ref = 'j1'")
        self.assertEqual(intent["state"], "skipped_dryrun")
        self.assertEqual(intent["dry_run"], 1)
        labeled = db.query_one("SELECT user_label FROM evaluations WHERE user_label IS NOT NULL")
        self.assertEqual(labeled["user_label"], "want")
        unlabeled = db.query_one(
            "SELECT user_label FROM evaluations WHERE skipped_by = 'low_score'"
        )
        self.assertIsNone(unlabeled["user_label"])  # NULL=未标注

    def test_open_database_yields_ready_schema(self) -> None:
        db = self._open()
        names = {
            r["name"]
            for r in db.query(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        self.assertEqual(names, set(ALL_TABLES))
        db.execute("INSERT INTO run_state (key, value_json) VALUES ('k', 'v')")
        self.assertEqual(db.query_one("SELECT value_json FROM run_state WHERE key='k'")[0], "v")


class DatabaseConcurrencyTests(DbTestCase):
    def test_threaded_writes_serialized(self) -> None:
        """单连接 + check_same_thread=False + 锁：多线程并发写不丢不乱。"""
        db = self._open()
        errors: list[Exception] = []

        def worker(tid: int) -> None:
            try:
                for i in range(25):
                    db.execute(
                        "INSERT INTO run_state (key, value_json) VALUES (?, ?)",
                        (f"t{tid}_k{i}", str(i)),
                    )
            except Exception as exc:  # pragma: no cover - 仅失败时收集
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(db.query_one("SELECT COUNT(*) FROM run_state")[0], 100)

    def test_transaction_rollback_on_error(self) -> None:
        db = self._open()
        seed(db)
        with self.assertRaisesRegex(RuntimeError, "boom"), db.transaction() as conn:
            conn.execute("INSERT INTO run_state (key, value_json) VALUES ('tx1', '1')")
            raise RuntimeError("boom")
        self.assertIsNone(db.query_one("SELECT key FROM run_state WHERE key = 'tx1'"))
        self.assertEqual(db.query_one("SELECT COUNT(*) FROM run_state")[0], 1)  # seed 数据未受影响


if __name__ == "__main__":
    unittest.main(verbosity=2)
