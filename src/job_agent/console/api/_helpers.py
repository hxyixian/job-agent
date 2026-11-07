"""console/api 共享工具（S5b）：敏感字段打码往返 + 配置写回辅助 + G3 推算。

职责（均不重复实现业务逻辑，只做控制台层的呈现/回写适配）：

- **打码往返**（§10.1 第 5 项 / 任务书 S5b 打码规则）：
  ``ai.api_key``、``notify.feishu.webhook``、``notify.feishu.secret``、
  ``notify.email.password`` 在 GET 回显时打码为 ``****尾4位``；PUT 收到
  **打码回显原样回传**时以存储原值写回（真实密钥不含 ``****``，判定安全），
  用户提交新明文或空串（主动清空）则原样生效——"写入走原值"。
- **G3 推算**：配置页"每日实际投递上限推算" = min(daily_limit,
  browse_limits.detail_daily)（每次投递必开一个 JD 详情页，详情日限是
  硬上界）；放量期首日另按 ramp_up/browse_rampup 取更低值。
- **校验失败 → 422**：路由层自行 ``model_validate``（打码替换须发生在
  校验前），``ValidationError`` 统一转可读的 422 文案。
"""

from __future__ import annotations

import queue
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from job_agent.config import Config, default_config_path, load_config, safety_warnings
from job_agent.core.orchestrator import EngineCommand
from job_agent.db import Database

__all__ = [
    "MASK",
    "REWRITE_NOTICE",
    "SENSITIVE_PATHS",
    "ContentContext",
    "SectionModel",
    "effective_daily_delivery",
    "invalid_payload",
    "mask_secret",
    "masked_config_dict",
    "resolve_masked_payload",
]


# ---- S5b 路由依赖集（装配协议，与 S5c OpsContext 同构）----


@dataclass
class ContentContext:
    """S5b「配置与内容类 5 页」路由的依赖集（S5a 骨架 / S5d 装配注入）。

    - ``db``：与引擎线程共享的线程安全 :class:`~job_agent.db.Database`
      （内置 RLock）——S4e/S3c 服务层契约为「db 第一参数，直接调用」；
    - ``config_path``：配置文件路径（GET/PUT /api/config* 的读写落点；
      缺省 ``<应用根>/config.yaml`` [R6-2]）。读写均以文件为准（§4：
      文件被外部修改在下一循环重载时以文件为准）；
    - ``command_queue``：引擎指令队列（§5.5 I6）——配置保存后经
      ``reload_config`` 指令提示引擎立即重载（缺省 None：引擎每轮调度
      循环本就会重读文件，仅少一次即时性）；
    - ``config_loader``：当前配置快照读取（诊断路由展示当前生效值用）。
    """

    db: Database
    config_path: Path = field(default_factory=default_config_path)
    command_queue: queue.Queue[EngineCommand] | None = None
    config_loader: Callable[[], Config] = load_config


# 打码前缀（§10.1：****尾4位）
MASK = "****"

# 全量重写提示（§4 写回规则 [B14]：页面提示"手改注释会被覆盖"）
REWRITE_NOTICE = (
    "保存将全量重写 config.yaml：手写注释不会被保留（以 config.example.yaml 为注释参照）；"
    "运行期修改在下一轮调度循环生效。"
)

# 敏感字段在配置树中的路径。飞书 webhook URL 本身携带长令牌，
# 与签名 secret 一并按"webhook secret"口径打码（从保守，§10.1 意图）。
SENSITIVE_PATHS: tuple[tuple[str, ...], ...] = (
    ("ai", "api_key"),
    ("notify", "feishu", "webhook"),
    ("notify", "feishu", "secret"),
    ("notify", "email", "password"),
)


class SectionModel(BaseModel):
    """路由层请求/节模型的公共基类：未知字段报错（与配置模型一致）。"""

    model_config = ConfigDict(extra="forbid")


def mask_secret(value: str) -> str:
    """明文 → ``****尾4位``；空值原样返回（空串即"未配置"，无可打码内容）。"""
    if not value:
        return ""
    return MASK + value[-4:]


def _is_masked(value: Any) -> bool:
    """值是否为打码回显（真实密钥不含 ``****``）。"""
    return isinstance(value, str) and MASK in value


def _config_value(config: Config, path: tuple[str, ...]) -> str:
    """按路径取配置树中的当前存储值（敏感路径均为 str 叶子）。"""
    obj: Any = config
    for key in path:
        obj = getattr(obj, key)
    return obj if isinstance(obj, str) else ""


def resolve_masked_payload(
    payload: dict[str, Any],
    current: Config,
    prefix: tuple[str, ...] = (),
) -> dict[str, Any]:
    """把 payload 中的打码回显替换为当前存储原值（就地修改并返回）。

    ``prefix``：payload 根对应的配置子树——整树 ``()``、notify 节
    ``("notify",)``、ai 节 ``("ai",)``、feishu 渠道 ``("notify", "feishu")`` 等。
    仅当 payload 命中敏感路径且该处的值仍是打码回显时替换；新明文与
    空串（用户主动清空）原样保留。
    """
    for path in SENSITIVE_PATHS:
        if len(path) <= len(prefix) or path[: len(prefix)] != prefix:
            continue
        rel = path[len(prefix) :]
        node: Any = payload
        for key in rel[:-1]:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if isinstance(node, dict) and rel[-1] in node and _is_masked(node[rel[-1]]):
            node[rel[-1]] = _config_value(current, path)
    return payload


def masked_config_dict(config: Config) -> dict[str, Any]:
    """配置树 → 打码后的可回显 dict（``model_dump(mode="json")``，
    polling.schedule 的 None 保持 ``same_as_delivery`` 标记、db_path 为 posix 串）。"""
    data: dict[str, Any] = config.model_dump(mode="json")
    for path in SENSITIVE_PATHS:
        node: Any = data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = mask_secret(str(node[path[-1]]))
    return data


def effective_daily_delivery(config: Config) -> dict[str, Any]:
    """[G3] "每日实际投递上限推算"：安全优先于投递量。

    - 常规日：``min(daily_limit, browse_limits.detail_daily)``——每次投递
      必先打开 JD 详情页（§7 步 3），详情日浏览上限构成硬上界；评估
      通过率会进一步压低实际值（提示语照此表述）；
    - 首日（放量期 + 试运行浏览放量叠加）：
      ``min(ramp_up.daily_limit, browse_rampup 首日详情日限)``。
    """
    d = config.delivery
    detail_daily = d.browse_limits.detail_daily
    normal_cap = min(d.daily_limit, detail_daily)
    first_day_detail = (
        d.browse_rampup.first_day.detail_daily if d.browse_rampup.enabled else detail_daily
    )
    first_day_cap = min(d.ramp_up.daily_limit, first_day_detail)
    note = (
        f"按详情日浏览上限 {detail_daily} 次/天与评估通过率推算，"
        f"每日实际投递量可能低于配置的 {d.daily_limit}——安全优先于投递量。"
    )
    return {
        "configured_daily_limit": d.daily_limit,
        "browse_detail_daily": detail_daily,
        "effective_daily_cap": normal_cap,
        "rampup_first_day_cap": first_day_cap,
        "note": note,
    }


def derived_payload(config: Config) -> dict[str, Any]:
    """GET /api/config 的派生信息：安全警告 [R6-1] + G3 推算 + 重写提示 [B14]。"""
    return {
        "safety_warnings": safety_warnings(config),
        "effective_daily_delivery": effective_daily_delivery(config),
        "rewrite_notice": REWRITE_NOTICE,
    }


def invalid_payload(exc: ValidationError) -> HTTPException:
    """``ValidationError`` → 422（可读中文拼接：字段路径 + 消息）。"""
    parts = [
        f"{'.'.join(str(loc) for loc in err['loc']) or '根节点'}: {err['msg']}"
        for err in exc.errors()
    ]
    return HTTPException(status_code=422, detail="；".join(parts))
