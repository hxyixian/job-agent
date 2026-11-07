"""本地 Web 控制台（设计文档 §10；开发文档 Stage5）。

S5a 交付：FastAPI 装配 + 六项安全加固（§10.1）+ 引擎指令队列注入口 +
DB 只读快照层 + health/status 只读端点 + uvicorn 副线程封装。
业务页面与端点由 S5b/S5c 追加（路由注册：``console.api.register_api``）。
"""

from job_agent.console.app import ConsoleServer, create_app, serve_console
from job_agent.console.commands import issue_command
from job_agent.console.context import ConsoleContext, get_context
from job_agent.console.hardening import ConsoleHardeningMiddleware, mask_sensitive_fields
from job_agent.console.snapshot import ReadAttemptError, ReadOnlyDatabase, read_state

__all__ = [
    "ConsoleContext",
    "ConsoleHardeningMiddleware",
    "ConsoleServer",
    "ReadAttemptError",
    "ReadOnlyDatabase",
    "create_app",
    "get_context",
    "issue_command",
    "mask_sensitive_fields",
    "read_state",
    "serve_console",
]
