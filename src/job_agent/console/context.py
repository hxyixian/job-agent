"""控制台运行上下文（挂在 ``app.state.console``，S5b/S5c 路由的统一依赖入口）。

经 :func:`get_context` 取用；装配见 ``console.app.create_app``。
"""

from __future__ import annotations

import queue
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request

from job_agent.config import Config, load_config
from job_agent.console.snapshot import ReadOnlyDatabase
from job_agent.core.orchestrator import EngineCommand

__all__ = ["ConsoleContext", "get_context"]


@dataclass
class ConsoleContext:
    """控制台依赖束（数据进出控制台的唯一通道）。

    - ``db``：只读快照视图（§10.1 第 4 项「GET 无副作用」的写阻断层；
      引擎状态/告警/待发箱/记录等全部呈现数据从这里读）；
    - ``command_queue``：引擎指令队列（§5.5 I6）。S5d 装配时传入
      ``orch.command_queue()``；业务路由经 ``console.commands.issue_command``
      投递，控制台侧不触碰引擎对象；
    - ``config_loader``：配置快照读取（S5b 配置页 GET/PUT 用；写回后配
      ``reload_config`` 指令让引擎重载）。
    """

    db: ReadOnlyDatabase | None = None
    command_queue: queue.Queue[EngineCommand] | None = None
    config_loader: Callable[[], Config] = load_config


def get_context(request: Request) -> ConsoleContext:
    """从 ``app.state.console`` 取控制台上下文（create_app 装配时写入）。"""
    value = request.app.state.console
    assert isinstance(value, ConsoleContext)
    return value
