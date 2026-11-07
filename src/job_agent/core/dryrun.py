"""试运行模式与拟发送数据隔离（§5.5 I9 之 ``is_dry_run`` + [N2] 隔离规则）。

职责（Stage2 S2d 任务书第 3 条）：
- **模式标志**：``run_state`` 的 ``dry_run`` 键是当前模式的唯一权威存储；
  首次读取（键缺失）以 ``config.delivery.dry_run`` 播种并落库（出厂默认
  True [FR3.9]），此后运行期切换只改 ``run_state``，配置文件仅在种子期起
  作用——避免"改配置→静默改模式"与运行态脱钩；
- **拟发送记录写入**：``record_planned_send`` 落 ``send_intents`` 一行
  ``state='skipped_dryrun', dry_run=1``（与 §6/I2 的 dry_run 分支同构，
  0001 迁移的 CHECK 保证该两字段严格绑定，结构上不可能污染正式发送记录）；
- **隔离查询助手** [N2]：拟发送清单（控制台"正式运行将发出什么"）只查
  ``dry_run=1`` 行；去重/统计（applications）只查 ``dry_run=0`` 行——
  "试运行不进 FR3.6 去重名单、FR8 投递记录、统计看板与 FR6.3 会话自动
  发送计数"的查询侧落点；会话计数的写侧隔离由
  :class:`job_agent.core.session.ConversationState.record_auto_send`
  在试运行模式下不计数保证。

模式切换的联动（不在本纯逻辑层执行的部分）：
- 关闭试运行 → :func:`set_dry_run` 调用 ``rampup.handle_dry_run_disabled``
  启动放量期（§13"dry_run 由用户关闭 → remaining_days=3"）；**重拍会话
  基线 [N2/B3]** 需要 adapter 读站点会话列表，属 S4d/S5c 编排职责；
- 限额口径 [G1]：``send_intents`` 计数**不分模式**（skipped_dryrun 计入
  小时/日限额），该口径属 S2b quota，与本模块的隔离查询互不冲突。
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from job_agent.config import Config
from job_agent.db import Database
from job_agent.timeutil import utcnow_str

from . import runstate

__all__ = [
    "KEY_DRY_RUN",
    "count_real_applications",
    "is_dry_run",
    "planned_send_list",
    "record_planned_send",
    "set_dry_run",
]

# run_state 键：当前模式（JSON bool）。S5c 控制台/引擎指令经 set_dry_run 切换。
KEY_DRY_RUN = "dry_run"

# send_intents 拟发送行（I2/G1 口径）：状态与 dry_run 标志由迁移 CHECK 绑定
_PLANNED_STATE = "skipped_dryrun"
_REAL_STATES = ("pending", "sent", "unknown")  # 正式发送动作的"已发生"口径（failed 不计）


def is_dry_run(db: Database, config: Config) -> bool:
    """当前模式 [I9]。键缺失时以配置播种（见模块 docstring），此后以 run_state 为准。"""
    stored = runstate.get_state(db, KEY_DRY_RUN)
    if stored is None:
        seeded = bool(config.delivery.dry_run)
        runstate.set_state(db, KEY_DRY_RUN, seeded)
        return seeded
    return bool(stored)


def set_dry_run(db: Database, config: Config, enabled: bool) -> None:
    """切换模式并落实 §13 联动：关闭试运行 → 启动放量期（幂等，见 rampup 模块）。

    重拍会话基线 [N2/B3] 由 S4d/S5c 编排（需 adapter），不在本层执行。
    """
    runstate.set_state(db, KEY_DRY_RUN, bool(enabled))
    if not enabled:
        # 延迟导入（rampup 依赖本模块的 is_dry_run，避免模块级循环依赖）：
        # 关闭试运行 → 启动放量期 [§13]
        import job_agent.core.rampup as rampup

        rampup.handle_dry_run_disabled(db, config)


def record_planned_send(
    db: Database,
    kind: Literal["greeting", "reply", "resume"],
    target_ref: str,
    payload_digest: str,
) -> int:
    """写入一条拟发送记录（``skipped_dryrun``）并返回 intent_id [I2 dry_run 分支同构]。

    只会产出 ``dry_run=1`` 行：结构上无法写入正式发送记录（迁移 CHECK）。
    created_at 取当前时刻；S4c 试运行分支在"判定通过、即将发送"处调用。
    """
    return db.execute_returning_id(
        "INSERT INTO send_intents (kind, target_ref, payload_digest, state, dry_run, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (kind, target_ref, payload_digest, _PLANNED_STATE, utcnow_str()),
    )


def planned_send_list(
    db: Database,
    *,
    kind: str | None = None,
    target_ref: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """拟发送清单（试运行清单页"正式运行将发出什么"数据源）[FR3.9/N2]。

    只返回 ``state='skipped_dryrun'``（即 ``dry_run=1``）的行，按时间倒序。
    """
    sql = (
        "SELECT id, kind, target_ref, payload_digest, state, dry_run, created_at "
        "FROM send_intents WHERE state = ?"
    )
    params: list = [_PLANNED_STATE]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    if target_ref is not None:
        sql += " AND target_ref = ?"
        params.append(target_ref)
    sql += " ORDER BY created_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return db.query(sql, params)


def count_real_applications(
    db: Database,
    *,
    company_key: str | None = None,
    job_id: str | None = None,
) -> int:
    """正式投递记录数 [N2：统计/去重口径只看 dry_run=0 行]。"""
    sql = "SELECT COUNT(*) AS n FROM applications WHERE dry_run = 0"
    params: list = []
    if company_key is not None:
        sql += " AND company_key = ?"
        params.append(company_key)
    if job_id is not None:
        sql += " AND job_id = ?"
        params.append(job_id)
    row = db.query_one(sql, params)
    return int(row["n"]) if row is not None else 0


def count_real_send_actions(
    db: Database,
    *,
    kind: str,
    target_ref: str | None = None,
    states: tuple[str, ...] = _REAL_STATES,
) -> int:
    """正式发送动作数（``dry_run=0``，默认只数已发生态）[N2 隔离查询]。

    供 S4/S5 做"排除试运行"的会话/岗位级发送统计；限额口径（含
    skipped_dryrun、不分模式）在 S2b quota，勿混用。
    """
    placeholders = ", ".join("?" for _ in states)
    sql = (
        f"SELECT COUNT(*) AS n FROM send_intents WHERE dry_run = 0 AND kind = ? "
        f"AND state IN ({placeholders})"
    )
    params: list = [kind, *states]
    if target_ref is not None:
        sql += " AND target_ref = ?"
        params.append(target_ref)
    row = db.query_one(sql, params)
    return int(row["n"]) if row is not None else 0
