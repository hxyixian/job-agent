"""SiteAdapter 结构视图与 §5.2 异常分类（S4d 消费侧契约；S4a 并行交付解耦层）。

背景：S4a（adapters/base.py + mock）与 S4d 并行交付，且 ``adapters/__init__.py``
导入具体实现（mock）完成自注册——mock 子包落盘前包级导入不可用。本模块
按设计文档 §5.2 的**方法签名**定义结构化 Protocol（与 scheduler.LimitsView
对 I1 的解耦模式一致），S4a 完整落地后
``job_agent.adapters.base.SiteAdapter`` 天然结构化满足本视图，本文件与全部
调用方（messenger/orchestrator/测试桩）无需改动。

异常分类（:func:`classify_adapter_error`）同理按 §5.2 异常树的**类名**映射：
``LoginRequiredError`` / ``RiskDetectedError`` / ``AdapterBrokenError`` /
``PlatformLimitError`` / ``TransientError`` 的类名由 §5.2 契约钉死，任何
符合契约的实现（Mock / Zhipin / 测试桩抛出的同名异常）都会被正确分类；
``RiskDetectedError.signal`` 属性经 :func:`risk_signal_of` 提取（S4a 契约：
构造参数携带 ``RiskSignal``）。S4a 全部落盘后可改为静态导入异常类做
``except`` 分派，行为不变（升级说明见 messenger/orchestrator docstring）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol

from job_agent.models import Conversation, LoginState, Message, RiskSignal, SendOutcome

__all__ = [
    "AdapterAPI",
    "AdapterErrorKind",
    "TaskFn",
    "classify_adapter_error",
    "risk_signal_of",
]

#: 引擎任务体（单执行者队列元素；返回值一律为 None，结果落库不回传 [I6]）
TaskFn = Callable[[], None]

#: §5.2 异常表的分类结果（调用方行为见 §5.2 表右列）
AdapterErrorKind = Literal[
    "login_required",
    "risk_detected",
    "adapter_broken",
    "platform_limit",
    "transient",
    "other",
]

_KIND_BY_NAME: dict[str, AdapterErrorKind] = {
    "LoginRequiredError": "login_required",
    "RiskDetectedError": "risk_detected",
    "AdapterBrokenError": "adapter_broken",
    "PlatformLimitError": "platform_limit",
    "TransientError": "transient",
}


class AdapterAPI(Protocol):
    """messenger/orchestrator 消费的最小适配器面（§5.2 的结构化子集）。

    全同步（§5.2：引擎单线程串行调用）；``Message.is_ours_auto`` 由核心层
    回填，适配器不提供。S4a 的 ``SiteAdapter`` 结构化满足本视图。
    """

    def check_login_state(self) -> LoginState: ...

    def detect_risk(self) -> RiskSignal: ...

    def list_conversations(self) -> list[Conversation]: ...

    def get_messages(self, conv_id: str, since: datetime | None) -> list[Message]: ...

    def send_reply(self, conv_id: str, message: str) -> SendOutcome: ...

    def send_resume(self, conv_id: str) -> SendOutcome: ...


def classify_adapter_error(exc: BaseException) -> AdapterErrorKind:
    """按 §5.2 异常树类名分类（未知名 → "other"，调用方跳过该单元继续 [NFR2]）。"""
    return _KIND_BY_NAME.get(type(exc).__name__, "other")


def risk_signal_of(exc: BaseException) -> RiskSignal | None:
    """提取 ``RiskDetectedError.signal``；非该异常（无 signal 属性）返回 None。"""
    signal = getattr(exc, "signal", None)
    return signal if isinstance(signal, RiskSignal) else None
