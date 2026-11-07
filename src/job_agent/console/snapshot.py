"""DB 只读快照层（开发文档 Stage5 S5a 任务 2；§10.1 第 4 项的写阻断）。

控制台与引擎仅经「指令队列（§5.5 I6）+ DB」通信：引擎线程持有读写连接，
控制台线程只持有本模块的 :class:`ReadOnlyDatabase` 只读视图——

- 只暴露 ``query`` / ``query_one``；其余属性（execute/transaction/close 等）
  一律 ``AttributeError``（引擎的写路径不可能经控制台触发）；
- 传入的 SQL 以首个关键字白名单（SELECT / WITH）校验，写语句即使经
  query 口径混入也即刻抛 :class:`ReadAttemptError`；
- 控制台页面的全部状态呈现（引擎状态/告警/待发箱/记录看板）一律走本层，
  保证「GET 一律无副作用」可被结构性验证（S5c GET 路由同此约束）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from typing import Any

from job_agent.db import Database

__all__ = ["ReadAttemptError", "ReadOnlyDatabase", "read_state"]

# 只读语句的合法首关键字（WITH … SELECT 属只读；其余一律视为写）
_READONLY_SQL_RE = re.compile(r"^\s*(?:select|with|explain\s+query\s+plan)\b", re.IGNORECASE)


class ReadAttemptError(PermissionError):
    """控制台只读视图收到写语句（S1 加固第 4 项：GET 无副作用）。"""


class ReadOnlyDatabase:
    """:class:`job_agent.db.Database` 的只读视图（线程安全由内层 Database 保证）。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def query(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """只读查询（语句首关键字非 SELECT/WITH 即拒绝）。"""
        self._ensure_readonly(sql)
        return self._db.query(sql, parameters)

    def query_one(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Row | None:
        """只读查询单行（同样拒绝写语句）。"""
        self._ensure_readonly(sql)
        return self._db.query_one(sql, parameters)

    def _ensure_readonly(self, sql: str) -> None:
        if not _READONLY_SQL_RE.match(sql):
            raise ReadAttemptError(f"控制台只读视图拒绝非 SELECT 语句：{sql.strip()[:80]}")

    def __getattr__(self, name: str) -> Any:
        # 刻意抛 AttributeError（而非其它异常类型）：hasattr/getattr 协议
        # 只识别 AttributeError，控制台代码与第三方库的鸭子类型探测不受干扰
        raise AttributeError(
            f"控制台只读视图不允许访问 Database.{name}（写操作须经引擎指令队列，§10.1 第 4 项）"
        )


def read_state(view: ReadOnlyDatabase, key: str, default: Any = None) -> Any:
    """读取 ``run_state`` 键的 JSON 值（与 ``core.runstate.get_state`` 同构的只读版）。"""
    row = view.query_one("SELECT value_json FROM run_state WHERE key = ?", (key,))
    if row is None:
        return default
    return json.loads(row["value_json"])
