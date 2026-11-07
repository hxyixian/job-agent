"""诊断路由：通知测试发送 + AI 连通性测试（§10.2「通知配置 / AI 配置」行）。

``POST /api/notify/test``
- ``mode="fake"``（**默认，假端点**）：不发出任何网络请求——按当前（或
  表单待存）渠道配置渲染将发送的消息正文并返回，用于保存前预览；
- ``mode="real"``：经对应 Notifier（FeishuNotifier/EmailNotifier）真实投递
  一条合成 ``CONSOLE_TEST`` 消息（不落库、不走通知中心路由，不污染事件流）；
- ``config`` 可选：携带页面未保存的表单值先行测试（打码回显原样回传时
  以存储原值替换），缺省用已保存配置。

``POST /api/ai/test``
- ``config`` 可选：测试未保存的表单值（一次性 :class:`AIClient`，不共享
  进程预算）；缺省测试**当前生效**客户端（``get_default_client``——与引擎
  共享实例即共享日预算计数 [B13]）；
- 探测请求经 ``call_json`` 发一条最小 JSON 指令（``{"pong": true}``），
  成功返回时延；``AIError`` 族（网络/认证/预算/解析）转 ``ok=false`` 文案。
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, ValidationError

from job_agent.ai.client import AIClient, get_default_client
from job_agent.ai.errors import AIError
from job_agent.ai.types import ChatMessage
from job_agent.config import AIConfig, EmailConfig, FeishuConfig, load_config
from job_agent.notify.base import EventRecord, Notifier
from job_agent.notify.email import EmailNotifier
from job_agent.notify.feishu import FeishuNotifier, format_event_text
from job_agent.timeutil import utcnow_str

from ._helpers import ContentContext, SectionModel, invalid_payload, resolve_masked_payload

__all__ = ["attach_diagnostics_routes"]

# 连通性探测超时（短于业务默认 60s，测试按钮不应长时间悬挂）
_AI_TEST_TIMEOUT_S = 20.0

# 合成测试事件（不在 §5.5 I5 枚举内：仅作渠道正文渲染，不经通知中心路由）
_TEST_EVENT_TYPE = "CONSOLE_TEST"


class NotifyTestPayload(SectionModel):
    """POST /notify/test 请求体：channel + mode（fake 默认）+ 可选表单配置。"""

    channel: Literal["feishu", "email"]
    mode: Literal["fake", "real"] = "fake"
    config: dict[str, Any] | None = None


class AiTestPayload(SectionModel):
    """POST /ai/test 请求体：可选待测配置（缺省测当前生效客户端）。"""

    config: dict[str, Any] | None = None


class _ProbeReply(BaseModel):
    """连通性探测的最小应答 schema。"""

    pong: bool = Field(default=False)


_PROBE_MESSAGES: list[ChatMessage] = [
    ChatMessage(
        role="user",
        content='连通性测试：请只回复 JSON {"pong": true}，不要输出其它内容。',
    )
]


def _test_record(channel: str) -> EventRecord:
    """合成测试事件（level=high：邮件渠道仅收 high，测试对两渠道可见）。"""
    return EventRecord(
        event_type=_TEST_EVENT_TYPE,
        level="high",
        payload={"console_test": True, "channel": channel},
        id=None,
        created_at=utcnow_str(),
    )


def _render_email_body(record: EventRecord) -> str:
    """邮件渠道假端点预览正文（与 EmailNotifier.send 构造的正文同构）。"""
    return (
        f"事件：{record.event_type}\n"
        f"级别：{record.level}\n"
        f"时间（UTC）：{record.created_at}\n\n"
        f"payload：\n"
        f"{json.dumps(record.payload, ensure_ascii=False, sort_keys=True, indent=2)}\n"
    )


def attach_diagnostics_routes(router: APIRouter, ctx: ContentContext) -> None:
    """注册诊断路由（通知测试发送 + AI 连通性测试；由 content.create_content_router 调用）。"""
    config_path = ctx.config_path

    @router.post("/notify/test")
    def post_notify_test(payload: NotifyTestPayload) -> dict[str, Any]:
        """测试发送：fake（默认）渲染正文不联网；real 经渠道真实投递。"""
        current = load_config(config_path)
        record = _test_record(payload.channel)
        base: dict[str, Any]
        if payload.channel == "feishu":
            base = current.notify.feishu.model_dump(mode="json")
            prefix: tuple[str, ...] = ("notify", "feishu")
        else:
            base = current.notify.email.model_dump(mode="json")
            prefix = ("notify", "email")
        if payload.config is not None:
            merged: dict[str, Any] = {
                **base,
                **resolve_masked_payload(dict(payload.config), current, prefix=prefix),
            }
        else:
            merged = base

        # 分渠道校验 + 装配（在各自分支内构造，保持具体类型供 mypy 收窄）
        notifier: Notifier
        preview: str
        try:
            if payload.channel == "feishu":
                notifier = FeishuNotifier(FeishuConfig.model_validate(merged))
                preview = format_event_text(record)
            else:
                notifier = EmailNotifier(EmailConfig.model_validate(merged))
                preview = (
                    f"主题：[job-agent][{record.level}] {record.event_type}\n"
                    f"收件人：{merged.get('to', '')!s}\n\n" + _render_email_body(record)
                )
        except ValidationError as exc:
            raise invalid_payload(exc) from exc

        try:
            configured = notifier.configured
            if payload.mode == "fake":
                return {
                    "ok": True,
                    "channel": payload.channel,
                    "mode": "fake",
                    "configured": configured,
                    "formatted": preview,
                    "error": None,
                }
            if not configured:
                return {
                    "ok": False,
                    "channel": payload.channel,
                    "mode": "real",
                    "configured": False,
                    "formatted": None,
                    "error": "渠道未配置（缺少 webhook / SMTP 必填参数），未发送。",
                }
            notifier.send(record)
            return {
                "ok": True,
                "channel": payload.channel,
                "mode": "real",
                "configured": True,
                "formatted": None,
                "error": None,
            }
        except Exception as exc:  # 渠道失败转用户可见文案，不上抛 500
            return {
                "ok": False,
                "channel": payload.channel,
                "mode": payload.mode,
                "configured": notifier.configured,
                "formatted": None,
                "error": f"发送失败：{type(exc).__name__}: {exc}",
            }
        finally:
            notifier.close()

    @router.post("/ai/test")
    def post_ai_test(payload: AiTestPayload) -> dict[str, Any]:
        """AI 连通性测试：最小 JSON 探测（经 call_json，含重试与预算语义）。"""
        current = load_config(config_path)
        client: AIClient
        if payload.config is not None:
            resolved = resolve_masked_payload(dict(payload.config), current, prefix=("ai",))
            try:
                section = AIConfig.model_validate(resolved)
            except ValidationError as exc:
                raise invalid_payload(exc) from exc
            try:
                # 一次性客户端：测试未保存的表单值；sleep 置空避免按钮重试退避悬挂
                client = AIClient(section, timeout_s=_AI_TEST_TIMEOUT_S, sleep=lambda _s: None)
            except AIError as exc:
                return {"ok": False, "model": section.model, "error": str(exc)}
        else:
            client = get_default_client()
        started = time.monotonic()
        try:
            reply = client.call_json(_PROBE_MESSAGES, _ProbeReply)
        except Exception as exc:  # AIError 族 + 意外异常都转 ok=false
            return {
                "ok": False,
                "model": current.ai.model,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "ok": True,
            "model": current.ai.model,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "pong": reply.pong,
            "error": None,
        }
