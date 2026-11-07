"""S3a 重试与退避测试（任务书 S3a 第 3 条：重试 ≤2、指数退避）。

假 transport 脚本化回放（Exception=请求层失败），假 sleep 记录退避序列，
零真实请求 [TEST1]。
"""

from __future__ import annotations

from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field

from job_agent.ai.client import AIClient
from job_agent.ai.errors import AITransportError, AIUnparseableError
from job_agent.ai.types import ChatMessage
from job_agent.config import AIConfig


class Sample(BaseModel):
    """client 测试用目标 schema（与 evaluate 的 wire schema 同构）。"""

    score: int = Field(ge=0, le=100)
    verdict: Literal["recommend", "neutral", "skip"]
    reason: str = ""


class FakeTransport:
    """记录调用并按脚本回放（str=正常输出，Exception=请求层失败）。"""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[list[ChatMessage]] = []

    def __call__(self, messages: list[ChatMessage]) -> str:
        self.calls.append(list(messages))
        item = self.responses.pop(0) if self.responses else ""
        if isinstance(item, Exception):
            raise item
        return item


MSG: list[ChatMessage] = [{"role": "user", "content": "评估"}]
GOOD = '{"score": 70, "verdict": "neutral", "reason": "ok"}'
BAD = "这不是 JSON"


def make_client(transport: FakeTransport, *, base: float = 0.5) -> tuple[AIClient, list[float]]:
    sleeps: list[float] = []
    client = AIClient(
        AIConfig(api_key="test-key"),
        transport=transport,
        backoff_base_s=base,
        sleep=sleeps.append,
    )
    return client, sleeps


def test_transport_fails_twice_then_succeeds() -> None:
    transport = FakeTransport([ConnectionError("网络抖动"), TimeoutError("超时"), GOOD])
    client, sleeps = make_client(transport)
    out = client.call_json(MSG, Sample)
    assert out.score == 70
    assert len(transport.calls) == 3
    # 指数退避：base * 2^0, base * 2^1
    assert sleeps == [0.5, 1.0]


def test_transport_always_fails_raises_transport_error() -> None:
    transport = FakeTransport([ConnectionError("挂了")] * 5)
    client, sleeps = make_client(transport)
    with pytest.raises(AITransportError, match="AI 请求经 2 次重试仍失败"):
        client.call_json(MSG, Sample)
    assert len(transport.calls) == 3  # 1 次初始 + 2 次重试
    assert sleeps == [0.5, 1.0]  # 最后一次失败后不再退避


def test_parse_fails_once_then_valid() -> None:
    transport = FakeTransport([BAD, GOOD])
    client, sleeps = make_client(transport)
    assert client.call_json(MSG, Sample).verdict == "neutral"
    assert len(transport.calls) == 2
    assert sleeps == [0.5]


def test_parse_always_fails_raises_unparseable() -> None:
    transport = FakeTransport([BAD] * 5)
    client, sleeps = make_client(transport)
    with pytest.raises(AIUnparseableError, match="2 次重试仍无法解析为 Sample"):
        client.call_json(MSG, Sample)
    assert len(transport.calls) == 3
    assert len(sleeps) == 2


def test_retry_sends_identical_messages() -> None:
    transport = FakeTransport([BAD, BAD, GOOD])
    client, _ = make_client(transport)
    client.call_json(MSG, Sample)
    assert transport.calls == [MSG, MSG, MSG]


def test_transport_error_wraps_last_exception() -> None:
    cause = ConnectionError("proxy refused")
    transport = FakeTransport([cause] * 3)
    client, _ = make_client(transport)
    with pytest.raises(AITransportError) as excinfo:
        client.call_json(MSG, Sample)
    assert excinfo.value.__cause__ is cause


def test_backoff_base_zero_for_fast_tests() -> None:
    """backoff_base_s=0 时退避序列恒 0（CI 快速路径可用）。"""
    transport = FakeTransport([BAD, BAD, BAD])
    client, sleeps = make_client(transport, base=0.0)
    with pytest.raises(AIUnparseableError):
        client.call_json(MSG, Sample)
    assert sleeps == [0.0, 0.0]


def test_calls_today_counts_every_attempt() -> None:
    """重试的每次尝试各占一次预算计数（防重试循环打爆预算）。"""
    transport = FakeTransport([BAD, ConnectionError("x"), GOOD])
    client, _ = make_client(transport)
    client.call_json(MSG, Sample)
    assert client.calls_today == 3


def test_success_without_retry_has_no_sleep() -> None:
    transport = FakeTransport([GOOD])
    client, sleeps = make_client(transport)
    client.call_json(MSG, Sample)
    assert sleeps == []
