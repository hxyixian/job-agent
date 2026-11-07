"""``run_state`` 表 JSON 读写助手（§6 run_state：key PK, value_json）。

S2d 内部共用小工具：session（greeting 核验标记）/ rampup（放量进度、风控
暂停、浏览档位）/ dryrun（模式标志）的状态全部以 JSON 值落在 ``run_state``，
键名常量由各模块自定义并导出，供 S4/S5 装配与控制台读取。

约定：
- 值一律 JSON 序列化（``ensure_ascii=False, sort_keys=True``，确定性输出）；
- 读不到返回调用方给定的 default（不区分"键不存在"与"值为 null"，状态键
  不写 null）；
- 单键读写各为一条 SQL（SQLite 原子），跨键一致性由调用方在
  ``Database.transaction()`` 内保证。
"""

from __future__ import annotations

import json
from typing import Any

from job_agent.db import Database

__all__ = ["get_state", "set_state"]


def get_state(db: Database, key: str, default: Any = None) -> Any:
    """读取 run_state 键的 JSON 值；键不存在返回 default。"""
    row = db.query_one("SELECT value_json FROM run_state WHERE key = ?", (key,))
    if row is None:
        return default
    return json.loads(row["value_json"])


def set_state(db: Database, key: str, value: Any) -> None:
    """写入 run_state 键（UPSERT，全量替换该键的 JSON 值）。"""
    db.execute(
        "INSERT INTO run_state (key, value_json) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
        (key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
    )
