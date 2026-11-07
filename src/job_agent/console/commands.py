"""控制台 → 引擎指令投递（§5.5 I6；队列消费方 orchestrator 已交付）。

业务路由（S5b/S5c）对引擎的一切操作动作（start/stop/pause/resume/
relaunch_browser/login_mode/reload_config/approve_outbox/skip_outbox）都经
本模块入队，**不得**直接调用引擎对象；执行结果由引擎线程写
``run_state.last_command``，控制台经 ``GET /api/status`` 轮询获取。
"""

from __future__ import annotations

import queue
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from job_agent.core.orchestrator import COMMANDS, EngineCommand

__all__ = ["issue_command"]


def issue_command(
    command_queue: queue.Queue[EngineCommand],
    cmd: str,
    payload: dict[str, Any] | None = None,
) -> EngineCommand:
    """构造并投递一条 :class:`EngineCommand`（queue.Queue 线程安全）。

    - ``cmd`` 必须是 ``orchestrator.COMMANDS`` 的成员（拼写错误即刻抛
      ``ValueError``，防静默无效指令）；
    - ``req_id`` = uuid4、``issued_at`` = 当前 UTC（tz-aware，与引擎
      ``scheduler.utc_clock`` 口径一致）——§5.5 I6 必填字段在前；
    - ``payload`` 如 ``{"outbox_id": 7}`` / 登录窗口关闭标记
      ``{"closed": true}`` / 配置节内容（I6 示例）。
    """
    if cmd not in COMMANDS:
        raise ValueError(f"未知引擎指令：{cmd!r}（合法值：{', '.join(COMMANDS)}）")
    command = EngineCommand(
        # cmd 已逐字校验 ∈ COMMANDS（即 EngineCommand.cmd 的 Literal 值域）
        cmd=cast("Any", cmd),
        req_id=str(uuid.uuid4()),
        issued_at=datetime.now(UTC),
        payload=payload if payload is not None else {},
    )
    command_queue.put(command)
    return command
