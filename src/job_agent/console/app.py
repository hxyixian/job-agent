"""控制台 FastAPI 装配（设计文档 §10 / 开发文档 Stage5 SubStage S5a）。

装配清单（六项加固的落点）：

1. 仅监听 127.0.0.1 —— ``config.ConsoleConfig.host`` Literal 锁定 +
   :func:`serve_console` 取 ``config.console`` 装配 uvicorn；
2. Host 校验中间件（∈{localhost,127.0.0.1} 否则 403）；
3. 非 GET 要求 ``X-Job-Agent: console`` 头（缺失/不符 403）；
4. GET 无副作用 —— 路由只经 ``snapshot.ReadOnlyDatabase`` 读库；
5. 敏感字段打码（****尾4位）—— ``hardening.mask_sensitive_fields`` 对
   全部 JSON 响应生效（S5b/S5c 配置页兜底）；
6. 不注册 CORSMiddleware + OPTIONS 预检一律 405 + Origin 存在时空或同源。

另含：异常统一 JSON 信封、静态目录挂载（S4/S5b/S5c 页面）、
uvicorn 副线程封装（:class:`ConsoleServer`，S5d 装配用）。

S5d 装配示例::

    orch = Orchestrator(db, adapter, ...)
    server = serve_console(config, db=db, command_queue=orch.command_queue())
    server.start()            # uvicorn 副线程，仅监听 127.0.0.1
    ...
    server.stop()             # 优雅退出（SIGINT/TERM 处理中调用）
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles

from job_agent import __version__
from job_agent.config import Config, default_config_path, load_config
from job_agent.console.api import register_api
from job_agent.console.context import ConsoleContext
from job_agent.console.hardening import ConsoleHardeningMiddleware
from job_agent.console.snapshot import ReadOnlyDatabase
from job_agent.core.orchestrator import EngineCommand
from job_agent.db import Database

__all__ = [
    "DEFAULT_STATIC_DIR",
    "ConsoleServer",
    "create_app",
    "serve_console",
]

logger = logging.getLogger(__name__)

#: 静态页面目录（S4/S5b/S5c 交付九页；当前为占位 index）
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "static"

# HTTP 状态 → 机器可读错误码（统一 JSON 信封 {"error": {"code", "message"}}）
_HTTP_CODE_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    500: "internal_error",
}


def _code_for_status(status_code: int) -> str:
    return _HTTP_CODE_BY_STATUS.get(status_code, f"http_{status_code}")


# ---- 异常统一 JSON ----


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        message: Any = exc.detail if isinstance(exc.detail, str) else jsonable_encoder(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": _code_for_status(exc.status_code), "message": message}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "请求参数校验失败",
                    "details": jsonable_encoder(exc.errors()),
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, _exc: Exception) -> JSONResponse:
        logger.exception("控制台未处理异常（%s %s）", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {"code": "internal_error", "message": "服务器内部错误（详情见服务端日志）"}
            },
        )


# ---- 静态目录挂载 ----


def _mount_static(app: FastAPI, directory: Path) -> None:
    """挂载 ``/static``（目录存在时；页面由 S4/S5b/S5c 交付）+ ``/`` 入口重定向。"""
    if not directory.is_dir():
        logger.warning("静态目录不存在，跳过挂载：%s", directory)
        return
    app.mount("/static", StaticFiles(directory=str(directory), html=True), name="static")

    @app.get("/", include_in_schema=False)
    def _index() -> RedirectResponse:
        return RedirectResponse(url="/static/", status_code=307)


# ---- 应用工厂 ----


def create_app(
    *,
    db: Database | None = None,
    command_queue: queue.Queue[EngineCommand] | None = None,
    config_loader: Callable[[], Config] = load_config,
    static_dir: str | Path | None = None,
    config_path: str | Path | None = None,
) -> FastAPI:
    """构造控制台应用（六项加固 + 异常统一 JSON + 静态挂载 + /api 路由）。

    - ``db``：引擎共享的 Database 句柄（内部包成只读视图，控制台侧无写口）；
      提供 ``db`` 时同时装配 S5b「配置与内容类 5 页」路由（写路径按 S4e
      契约直接调用 resume/greeting 服务层，见 ``api.content``）；
    - ``command_queue``：引擎指令队列注入口——S5d 传 ``orch.command_queue()``；
      S5b/S5c 业务路由经 ``console.commands.issue_command`` 投递（§5.5 I6）；
    - ``config_path``：S5b 配置页的读写落点（缺省 ``<应用根>/config.yaml``）；
    - ``static_dir``：缺省 ``console/static``。
    """
    app = FastAPI(
        title="job-agent 控制台",
        version=__version__,
        # 本地专用加固面：不暴露 OpenAPI/docs 端点（§10.1 最小暴露原则）
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.console = ConsoleContext(
        db=ReadOnlyDatabase(db) if db is not None else None,
        command_queue=command_queue,
        config_loader=config_loader,
    )
    # 唯一自研中间件：Host 校验 / OPTIONS 拒绝 / Origin 校验 /
    # X-Job-Agent 头 / JSON 响应打码（§10.1 第 2/3/5/6 项）
    app.add_middleware(ConsoleHardeningMiddleware)
    _register_exception_handlers(app)
    content_router: APIRouter | None = None
    if db is not None:
        from job_agent.console.api._helpers import ContentContext
        from job_agent.console.api.content import create_content_router

        content_router = create_content_router(
            ContentContext(
                db=db,
                config_path=Path(config_path) if config_path is not None else default_config_path(),
                command_queue=command_queue,
                config_loader=config_loader,
            )
        )
    register_api(app, content=content_router)
    _mount_static(app, Path(static_dir) if static_dir is not None else DEFAULT_STATIC_DIR)
    return app


# ---- uvicorn 副线程（开发文档 Stage5 S5a「FastAPI 装配（uvicorn 副线程）」）----


class ConsoleServer:
    """uvicorn 副线程封装（S5d 装配：start → wait_until_ready → … → stop）。"""

    def __init__(self, app: FastAPI, *, host: str, port: int, log_level: str = "warning") -> None:
        self.uvicorn_config = uvicorn.Config(
            app, host=host, port=port, log_level=log_level, access_log=False
        )
        self.server = uvicorn.Server(self.uvicorn_config)
        self._thread = threading.Thread(target=self.server.run, name="console-uvicorn", daemon=True)
        self._started = False

    @property
    def host(self) -> str:
        return str(self.uvicorn_config.host)

    @property
    def port(self) -> int:
        return int(self.uvicorn_config.port)

    def start(self) -> None:
        """启动 uvicorn 副线程（守护线程；重复启动抛 RuntimeError）。"""
        if self._started:
            raise RuntimeError("ConsoleServer 已启动")
        self._started = True
        self._thread.start()

    def wait_until_ready(self, timeout: float = 10.0) -> bool:
        """等待 uvicorn 完成启动（S5d 启动自检；超时/线程早退返回 False）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.server.started:
                return True
            if self._started and not self._thread.is_alive():
                return False
            time.sleep(0.05)
        return self.server.started

    def stop(self, timeout: float = 10.0) -> None:
        """请求退出并等待副线程结束（S5d 优雅退出：停引擎→停控制台→flush 日志）。"""
        self.server.should_exit = True
        if self._started:
            self._thread.join(timeout=timeout)

    def join(self, timeout: float | None = None) -> None:
        """等待副线程自然结束（不触发退出）。"""
        self._thread.join(timeout=timeout)


def serve_console(
    config: Config,
    *,
    db: Database | None = None,
    command_queue: queue.Queue[EngineCommand] | None = None,
    config_loader: Callable[[], Config] | None = None,
    log_level: str = "warning",
) -> ConsoleServer:
    """按配置构造控制台服务（只构造不启动；host/port 取 ``config.console``）。

    host 由 ``ConsoleConfig.host`` 的 Literal 锁定为 ``127.0.0.1``
    （§10.1 第 1 项：不开放配置，0.0.0.0 会把控制台暴露到局域网 [R6-1]）。
    """
    app = create_app(
        db=db,
        command_queue=command_queue,
        config_loader=config_loader if config_loader is not None else (lambda: config),
    )
    return ConsoleServer(
        app, host=config.console.host, port=config.console.port, log_level=log_level
    )
