"""S3a 日预算熔断测试（任务书 S3a 第 3 条 + 设计 §9.1 [B13]）。

覆盖：计数口径（每次尝试各计 1）、达限抛 AIBudgetExhausted、
北京自然日翻转重置、AI_BUDGET_EXHAUSTED 事件（名称与 §5.5 I5 / §11
的 medium 级别一致）。全部离线。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field

from job_agent.ai.client import EVENT_AI_BUDGET_EXHAUSTED, AIClient
from job_agent.ai.errors import AIBudgetExhausted, AIUnparseableError
from job_agent.ai.types import ChatMessage
from job_agent.config import AIConfig
from job_agent.core.events import EVENT_LEVELS
from job_agent.timeutil import CST


class Sample(BaseModel):
    score: int = Field(ge=0, le=100)
    verdict: Literal["recommend", "neutral", "skip"]
    reason: str = ""


class FakeTransport:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[list[ChatMessage]] = []

    def __call__(self, messages: list[ChatMessage]) -> str:
        self.calls.append(list(messages))
        item = self.responses.pop(0) if self.responses else GOOD
        if isinstance(item, Exception):
            raise item
        return item


GOOD = '{"score": 10, "verdict": "skip", "reason": "r"}'
BAD = "杂文"
MSG: list[ChatMessage] = [{"role": "user", "content": "评估"}]


def make_client(
    transport: FakeTransport,
    *,
    budget: int,
    events: list[str] | None = None,
    times: list[datetime] | None = None,
) -> AIClient:
    return AIClient(
        AIConfig(api_key="test-key", daily_budget_calls=budget),
        transport=transport,
        backoff_base_s=0.0,
        sleep=lambda _s: None,
        now_fn=(lambda: times.pop(0)) if times is not None else (lambda: datetime.now(UTC)),
        on_event=events.append if events is not None else None,
    )


def test_event_name_matches_i5_registry() -> None:
    """事件名与 core/events.EVENT_LEVELS 成员一致（ai 不 import core，
    拼写契约由此测试钉住）。"""
    assert EVENT_AI_BUDGET_EXHAUSTED in EVENT_LEVELS
    assert EVENT_LEVELS[EVENT_AI_BUDGET_EXHAUSTED] == "medium"  # §11：AI 预算耗尽=medium


def test_budget_exhausted_at_limit() -> None:
    transport = FakeTransport([GOOD] * 10)
    client = make_client(transport, budget=3)
    for _ in range(3):
        assert client.call_json(MSG, Sample).score == 10
    assert client.calls_today == 3
    with pytest.raises(AIBudgetExhausted, match="B13"):
        client.call_json(MSG, Sample)
    assert len(transport.calls) == 3  # 熔断后不再发请求


def test_budget_exhausted_during_retry_sequence() -> None:
    """预算在重试途中耗尽 → 抛预算异常（优先于解析失败，保守可操作）。"""
    transport = FakeTransport([BAD] * 5)
    client = make_client(transport, budget=1)
    with pytest.raises(AIBudgetExhausted):
        client.call_json(MSG, Sample)
    assert len(transport.calls) == 1  # 第 2 次尝试被熔断拦下
    assert client.calls_today == 1


def test_budget_counted_across_calls() -> None:
    """跨多次 call_json 调用累计（评估+分类+生成合计 [B13] 的基础）。"""
    transport = FakeTransport([GOOD] * 10)
    client = make_client(transport, budget=100)
    client.call_json(MSG, Sample)
    client.call_json(MSG, Sample)
    assert client.calls_today == 2


def test_beijing_day_rollover_resets_counter() -> None:
    """北京自然日翻转 [R6-4]：新的一天计数清零、熔断解除。"""
    t1 = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    # 22:16 UTC = 北京次日 06:16，跨过北京自然日边界
    t2 = t1 + timedelta(hours=13, minutes=16)
    transport = FakeTransport([GOOD] * 10)
    client = make_client(transport, budget=1, times=[t1, t1, t1, t2])
    client.call_json(MSG, Sample)  # t1：占满预算
    with pytest.raises(AIBudgetExhausted):
        client.call_json(MSG, Sample)  # t1：熔断
    client.call_json(MSG, Sample)  # t2：新的一天，重置后放行
    assert client.calls_today == 1


def test_beijing_boundary_semantics() -> None:
    """UTC 15:59:59 与 16:00:00 分属北京两个自然日（00:00 边界）。"""
    before_midnight = datetime(2026, 11, 7, 15, 59, 59, tzinfo=UTC)  # 北京 23:59:59
    after_midnight = datetime(2026, 11, 7, 16, 0, 0, tzinfo=UTC)  # 北京 11-08 00:00
    assert before_midnight.astimezone(CST).strftime("%Y-%m-%d") != after_midnight.astimezone(
        CST
    ).strftime("%Y-%m-%d")
    transport = FakeTransport([GOOD] * 10)
    client = make_client(
        transport,
        budget=1,
        times=[before_midnight, before_midnight, before_midnight, after_midnight],
    )
    client.call_json(MSG, Sample)
    with pytest.raises(AIBudgetExhausted):
        client.call_json(MSG, Sample)
    client.call_json(MSG, Sample)  # 跨界后重置


def test_exhaustion_event_fired_once_per_day() -> None:
    events: list[str] = []
    transport = FakeTransport([GOOD] * 10)
    client = make_client(transport, budget=1, events=events)
    client.call_json(MSG, Sample)
    with pytest.raises(AIBudgetExhausted):
        client.call_json(MSG, Sample)
    with pytest.raises(AIBudgetExhausted):
        client.call_json(MSG, Sample)
    assert events == [EVENT_AI_BUDGET_EXHAUSTED]  # 只在首次熔断时通知一次


def test_no_event_without_hook_or_exhaustion() -> None:
    events: list[str] = []
    transport = FakeTransport([GOOD])
    client = make_client(transport, budget=10, events=events)
    client.call_json(MSG, Sample)
    assert events == []


def test_budget_guard_precedes_transport_retry() -> None:
    """传输错误的重试序列中预算耗尽 → 以预算异常收尾（可操作信号优先）。"""
    transport = FakeTransport([ConnectionError("x")] * 5)
    client = make_client(transport, budget=1)
    with pytest.raises(AIBudgetExhausted):
        client.call_json(MSG, Sample)
    assert len(transport.calls) == 1  # 第 2 次尝试被熔断拦下


def test_unparseable_error_is_not_budget_error() -> None:
    """预算充足时解析失败走 AIUnparseableError（两异常不混淆）。"""
    transport = FakeTransport([BAD] * 3)
    client = make_client(transport, budget=100)
    with pytest.raises(AIUnparseableError):
        client.call_json(MSG, Sample)
