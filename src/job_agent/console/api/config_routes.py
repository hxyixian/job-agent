"""任务/通知/AI 配置路由（§10.2「投递任务配置 / 通知配置 / AI 配置」行）。

对接 ``config.py`` 的 load_config / write_back（§4 写回规则 [B14]：控制台
保存 = **全量重写 YAML，不保留手写注释**——派生信息里带页面提示文案）：

- ``GET /api/config``：整树打码回显 + 派生信息（safety_warnings [R6-1]、
  每日实际投递上限推算 [G3]、全量重写提示 [B14]）；
- ``PUT /api/config``：整树保存（打码回显原样回传 → 以原值写回）；
- ``GET/PUT /api/config/notify``、``GET/PUT /api/config/ai``：分节读写
  （节保存 = 读当前整树 → 替换该节 → 全量重写，其余节不受影响）。

保存成功后经 ``ContentContext.command_queue`` 投递 ``reload_config`` 指令
（§5.5 I6）提示引擎立即重载；队列未装配时响应 ``reload_requested=false``
（引擎每轮调度循环本就会重读文件，仅少一次即时性）。

校验与配置模型一致（StrictModel 树：``auto_reply_limit >= 1`` [A2]、
端口/枚举/范围约束），失败统一 422。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from pydantic import ValidationError

from job_agent.config import (
    AIConfig,
    Config,
    NotifyConfig,
    load_config,
    write_back,
)
from job_agent.core.orchestrator import EngineCommand

from ._helpers import (
    ContentContext,
    derived_payload,
    invalid_payload,
    mask_secret,
    masked_config_dict,
    resolve_masked_payload,
)

__all__ = ["attach_config_routes"]

# 分节接口的节内敏感路径（与 _helpers.SENSITIVE_PATHS 对齐）
_NOTIFY_SECRETS: tuple[tuple[str, ...], ...] = (
    ("feishu", "webhook"),
    ("feishu", "secret"),
    ("email", "password"),
)
_AI_SECRETS: tuple[tuple[str, ...], ...] = (("api_key",),)


def _masked_section(
    section: AIConfig | NotifyConfig,
    secret_keys: tuple[tuple[str, ...], ...],
) -> dict[str, Any]:
    """节模型 → 打码 dict（secret_keys 为节内相对路径元组）。"""
    data: dict[str, Any] = section.model_dump(mode="json")
    for rel in secret_keys:
        node: Any = data
        for key in rel[:-1]:
            node = node[key]
        node[rel[-1]] = mask_secret(str(node[rel[-1]]))
    return data


def _request_reload(ctx: ContentContext) -> bool:
    """保存后投递 ``reload_config``（I6）；队列未装配返回 False。"""
    if ctx.command_queue is None:
        return False
    ctx.command_queue.put(
        EngineCommand(
            cmd="reload_config",
            req_id=str(uuid.uuid4()),
            issued_at=datetime.now(UTC),
            payload={},
        )
    )
    return True


def attach_config_routes(router: APIRouter, ctx: ContentContext) -> None:
    """注册配置路由（由 ``content.create_content_router`` 调用）。"""
    config_path = ctx.config_path

    @router.get("/config")
    def get_config() -> dict[str, Any]:
        """整树配置（打码回显）+ 派生信息。GET 无副作用 [S1 加固第 4 项]。"""
        config = load_config(config_path)
        return {"config": masked_config_dict(config), "derived": derived_payload(config)}

    @router.put("/config")
    def put_config(payload: dict[str, Any]) -> dict[str, Any]:
        """整树保存：打码回显 → 原值替换 → 校验 → 全量重写 [B14]。"""
        current = load_config(config_path)
        resolved = resolve_masked_payload(dict(payload), current)
        try:
            config = Config.model_validate(resolved)
        except ValidationError as exc:
            raise invalid_payload(exc) from exc
        write_back(config, config_path)
        fresh = {"config": masked_config_dict(config), "derived": derived_payload(config)}
        return {"ok": True, "reload_requested": _request_reload(ctx), **fresh}

    @router.get("/config/notify")
    def get_notify() -> dict[str, Any]:
        """通知节（飞书 webhook/secret、SMTP 密码打码回显）。"""
        config = load_config(config_path)
        return {"notify": _masked_section(config.notify, _NOTIFY_SECRETS)}

    @router.put("/config/notify")
    def put_notify(payload: dict[str, Any]) -> dict[str, Any]:
        """通知节保存：节校验（feishu.levels 必含 high 等）→ 合入整树 → 全量重写。"""
        current = load_config(config_path)
        resolved = resolve_masked_payload(dict(payload), current, prefix=("notify",))
        try:
            section = NotifyConfig.model_validate(resolved)
        except ValidationError as exc:
            raise invalid_payload(exc) from exc
        merged = current.model_copy(update={"notify": section})
        write_back(merged, config_path)
        return {
            "ok": True,
            "reload_requested": _request_reload(ctx),
            "notify": _masked_section(section, _NOTIFY_SECRETS),
        }

    @router.get("/config/ai")
    def get_ai() -> dict[str, Any]:
        """AI 节（api_key 打码回显 ****尾4位）。"""
        config = load_config(config_path)
        return {"ai": _masked_section(config.ai, _AI_SECRETS)}

    @router.put("/config/ai")
    def put_ai(payload: dict[str, Any]) -> dict[str, Any]:
        """AI 节保存：打码回显原样回传 → 原值写回；校验失败 422。"""
        current = load_config(config_path)
        resolved = resolve_masked_payload(dict(payload), current, prefix=("ai",))
        try:
            section = AIConfig.model_validate(resolved)
        except ValidationError as exc:
            raise invalid_payload(exc) from exc
        merged = current.model_copy(update={"ai": section})
        write_back(merged, config_path)
        return {
            "ok": True,
            "reload_requested": _request_reload(ctx),
            "ai": _masked_section(section, _AI_SECRETS),
        }
