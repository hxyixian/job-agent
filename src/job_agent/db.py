"""SQLite 访问层：连接管理 + 迁移执行器（设计文档 §6，Stage1 S1b）。

职责：
- 连接管理：每连接设置 ``PRAGMA journal_mode=WAL``、``PRAGMA foreign_keys=ON``
  （另附 ``synchronous=NORMAL``（WAL 推荐搭配）与 ``busy_timeout``，均幂等）。
- 迁移执行器 :func:`run_migrations`：按文件名序号升序应用 ``migrations/NNNN_*.sql``，
  ``schema_migrations`` 表记录已应用版本；每个迁移独占一个事务（BEGIN IMMEDIATE
  → 逐句执行 → 记录版本 → COMMIT），失败整体回滚；已应用版本跳过，故幂等。
- 线程策略（任务书 S1b）：引擎线程**单连接** + ``check_same_thread=False`` +
  :class:`threading.RLock` 串行化全部访问。:class:`Database` 封装该策略，
  引擎与控制台共享同一实例；``transaction()`` 在整个事务期间持锁，
  避免其它线程的语句混入未提交事务。

时间戳约定（与 migrations/0001_init.sql 头注释一致）：ISO-8601 UTC 文本，
用 :func:`utcnow` 生成，与 SQLite ``strftime('%Y-%m-%dT%H:%M:%fZ','now')``
默认值同构（字典序 == 时间序，可直接比较/排序）。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_MIGRATIONS_DIR",
    "Database",
    "MigrationError",
    "connect",
    "open_database",
    "run_migrations",
    "utcnow",
]

# 迁移脚本随包分发：src/job_agent/migrations/
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_MIGRATION_FILE_PREFIX_LEN = 4  # NNNN_name.sql 中的序号宽度


class MigrationError(Exception):
    """迁移发现/应用失败（目录缺失、序号重复、SQL 执行错误等）。"""


def utcnow() -> str:
    """当前时刻的 ISO-8601 UTC 文本（毫秒精度，与 SQL 默认值同构）。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """按 §6 设置每连接 PRAGMA（journal_mode 持久化于库文件，其余每连接生效）。"""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # PRAGMA 结果无需缓存游标
    conn.commit()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）SQLite 连接并应用 §6 PRAGMA。

    父目录不存在时自动创建（如首次运行的 ``data/``）。
    注意：连接以 ``check_same_thread=False`` 打开，跨线程使用必须由调用方加锁
    （见 :class:`Database`）。
    """
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        check_same_thread=False,
        isolation_level=None,  # 自动提交模式；事务由 transaction()/run_migrations 显式管理
        timeout=5.0,
    )
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


def _split_statements(sql_text: str) -> list[str]:
    """把 SQL 脚本切成单条语句。

    用 :func:`sqlite3.complete_statement` 累积判断，能正确跳过字符串字面量与
    注释中的分号。残余非空片段（末条语句未带分号）作为最后一条语句返回。
    """
    statements: list[str] = []
    buffer = ""
    for line in sql_text.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            stmt = buffer.strip()
            if stmt:
                statements.append(stmt)
            buffer = ""
    remainder = buffer.strip()
    if remainder:
        statements.append(remainder)
    return statements


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    """发现 NNNN_*.sql 并按序号升序返回 (version, path)；序号重复即报错。"""
    if not migrations_dir.is_dir():
        raise MigrationError(f"migrations directory not found: {migrations_dir}")
    found: dict[int, Path] = {}
    for entry in sorted(migrations_dir.iterdir()):
        name = entry.name
        if not (entry.is_file() and name.endswith(".sql")):
            continue
        digits = name[:_MIGRATION_FILE_PREFIX_LEN]
        if not (digits.isdigit() and name[_MIGRATION_FILE_PREFIX_LEN] == "_"):
            continue  # 非迁移命名的 .sql 忽略
        version = int(digits)
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version:04d}: {found[version].name} vs {name}"
            )
        found[version] = entry
    if not found:
        raise MigrationError(f"no NNNN_*.sql migrations found in {migrations_dir}")
    return sorted(found.items())


def run_migrations(
    db_path: str | Path,
    migrations_dir: str | Path | None = None,
) -> list[int]:
    """把迁移按序号应用到 ``db_path``，返回本次实际应用的版本列表。

    幂等：已在 ``schema_migrations`` 记录的版本跳过；空库与已迁移库两态均可执行。
    每个迁移独占事务：SQL 全部成功 + 版本记录写入后才 COMMIT，任一句失败则
    ROLLBACK 并抛 :class:`MigrationError`（该迁移及其后版本均不应用）。
    """
    directory = Path(migrations_dir) if migrations_dir is not None else DEFAULT_MIGRATIONS_DIR
    migrations = _discover_migrations(directory)

    conn = connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied = {
            int(row["version"]) for row in conn.execute("SELECT version FROM schema_migrations")
        }
        applied_now: list[int] = []
        for version, path in migrations:
            if version in applied:
                continue
            statements = _split_statements(path.read_text(encoding="utf-8"))
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, path.name, utcnow()),
                )
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK")
                raise MigrationError(
                    f"migration {path.name} failed and was rolled back: {exc}"
                ) from exc
            conn.execute("COMMIT")
            applied_now.append(version)
        return applied_now
    finally:
        conn.close()


class Database:
    """线程安全的单连接 SQLite 门面（引擎线程 + 控制台线程共用）。

    所有操作在 :class:`threading.RLock` 保护下串行执行；连接以
    ``check_same_thread=False`` 打开（见 :func:`connect`）。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._conn = connect(db_path)
        self._lock = threading.RLock()

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        """执行单条写语句（自动提交模式下即时生效），返回受影响行数 [R6-6]。

        刻意不返回游标：游标取数发生在锁外，会破坏串行化保证。
        需要取数据的一律走 query / query_one。
        """
        with self._lock:
            cursor = self._conn.execute(sql, parameters)
            return cursor.rowcount

    def execute_returning_id(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        """执行 INSERT 并返回 lastrowid（同样不暴露游标）[R6-6]。"""
        with self._lock:
            cursor = self._conn.execute(sql, parameters)
            return int(cursor.lastrowid or 0)

    def executemany(self, sql: str, seq_of_parameters: Sequence[Sequence[Any]]) -> None:
        """批量执行单条语句。"""
        with self._lock:
            self._conn.executemany(sql, seq_of_parameters)

    def query(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """查询并一次性取回全部行。"""
        with self._lock:
            return self._conn.execute(sql, parameters).fetchall()

    def query_one(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Row | None:
        """查询单行，无结果返回 None。"""
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, parameters).fetchone()
            return row

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务：期间持锁，异常 ROLLBACK，正常 COMMIT。

        例::

            with db.transaction() as conn:
                conn.execute("INSERT INTO run_state(key, value_json) VALUES (?, ?)", (k, v))
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def close(self) -> None:
        """关闭连接（幂等）。"""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def open_database(
    db_path: str | Path,
    migrations_dir: str | Path | None = None,
) -> Database:
    """应用迁移后打开长连接 ：class:`Database`（启动装配用便捷入口）。"""
    run_migrations(db_path, migrations_dir)
    return Database(db_path)
