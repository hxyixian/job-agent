"""S3a ``call_json`` 契约的延迟定位器（设计文档 §5.5 I4 / 任务书 S3a 第 1 条）。

背景：S3a（ai/client.py）与 S3b 并行交付，交付时点不确定。本模块是
S3b（classify/reply）到统一调用入口 ``job_agent.ai.client.call_json`` 的
**唯一**连接方式：

- 不静态 import ``job_agent.ai.client``——S3a 缺席时静态引用（含
  TYPE_CHECKING 引用）会让 mypy 全仓报 import-not-found，故以 Protocol
  固定契约签名 + importlib 运行期定位（语义等同 TYPE_CHECKING 引用，
  但两类状态下均保持类型检查绿）；
- 不复制/不实现 client 的任何逻辑（重试/预算/JSON 提取均属 S3a）；
- TEST1 离线测试不触真实客户端：测试用 monkeypatch 替换本模块的
  ``resolve_call_json`` 注入桩（见 tests/unit/ai/s3b_support.py）。

S3a 落地后本模块无需改动；若 S3a 最终未提供模块级 ``call_json``（契约
偏差），``resolve_call_json`` 抛 AttributeError/ModuleNotFoundError，由
调用方按各自的失败契约处理（classify 兜底 key、reply 上抛转待发箱）。
"""

from __future__ import annotations

import importlib
from typing import Protocol, TypeVar

from pydantic import BaseModel

from job_agent.ai.types import ChatMessage

__all__ = [
    "CallJsonFn",
    "resolve_call_json",
]

ModelT = TypeVar("ModelT", bound=BaseModel)

_CLIENT_MODULE = "job_agent.ai.client"


class CallJsonFn(Protocol):
    """§5.5 I4 / 任务书 S3a 的统一调用入口签名。

    ``call_json(messages, schema)``：请求 → 文本提取 JSON（代码块/裸 JSON
    兜底）→ Pydantic 校验 → 失败重试 ≤2 → 仍失败抛 AIUnparseableError；
    预算耗尽抛 AIBudgetExhausted；请求层错误抛 AITransportError。
    """

    def __call__(self, messages: list[ChatMessage], schema: type[ModelT]) -> ModelT: ...


def resolve_call_json() -> CallJsonFn:
    """运行期定位 ``job_agent.ai.client.call_json``（S3a 契约入口）。

    Raises:
        ImportError / AttributeError: S3a 未交付或入口名不符——
        classify 捕获后兜底 key，reply 向上传播由调用方降级待发箱。
    """
    module = importlib.import_module(_CLIENT_MODULE)
    fn: CallJsonFn = module.call_json
    return fn
