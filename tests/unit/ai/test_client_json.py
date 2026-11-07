"""S3a JSON 提取边界测试（任务书 S3a 第 3 条：markdown 包裹/前后杂文/非法）。

全部针对 client 的纯函数与 call_json 全链路（注入假 transport），
不发真实请求 [TEST1]。
"""

from __future__ import annotations

from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field

from job_agent.ai.client import AIClient, extract_json_candidates, parse_json_object
from job_agent.ai.errors import AIUnparseableError
from job_agent.ai.types import ChatMessage
from job_agent.config import AIConfig


class Sample(BaseModel):
    """client 测试用目标 schema（与 evaluate 的 wire schema 同构，独立定义）。"""

    score: int = Field(ge=0, le=100)
    verdict: Literal["recommend", "neutral", "skip"]
    reason: str = ""


def make_client(
    transport_responses: list[Any],
    *,
    budget: int = 1000,
) -> tuple[AIClient, FakeTransport]:
    """假 transport 客户端：responses 逐次弹出（Exception 则抛出）。"""
    transport = FakeTransport(transport_responses)
    client = AIClient(
        AIConfig(api_key="test-key", daily_budget_calls=budget),
        transport=transport,
        backoff_base_s=0.0,
        sleep=lambda _s: None,
    )
    return client, transport


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


# ---- 纯函数：代码块提取 ----


def test_markdown_json_fence_with_surrounding_prose() -> None:
    """```json 围栏 + 前后杂文 → 取围栏内容。"""
    text = (
        "好的，评估如下：\n```json\n"
        '{"score": 80, "verdict": "neutral", "reason": "ok"}\n'
        "```\n希望有帮助。"
    )
    obj = parse_json_object(text)
    assert obj == {"score": 80, "verdict": "neutral", "reason": "ok"}


def test_markdown_fence_without_language_tag() -> None:
    text = '```\n{"score": 1, "verdict": "skip", "reason": "r"}\n```'
    assert parse_json_object(text)["score"] == 1


def test_bare_json_with_prose_before_and_after() -> None:
    """裸 JSON 前后都有杂文（无围栏）→ 平衡扫描兜底。"""
    text = '评估结果是：\n{"score": 60, "verdict": "neutral", "reason": "基本匹配"}\n以上供参考。'
    assert parse_json_object(text)["verdict"] == "neutral"


def test_first_valid_object_wins_among_multiple() -> None:
    text = '前文 {"noise": true} 后文 {"score": 5, "verdict": "skip", "reason": "x"} 尾注'
    assert parse_json_object(text) == {"noise": True}


def test_invalid_fence_falls_back_to_bare_json_outside() -> None:
    """围栏内是解释文字、合法 JSON 在围栏外 → 全文平衡扫描兜底命中。"""
    text = (
        "```json\n这里应该是 JSON 但模型写了说明文字\n```\n"
        '{"score": 7, "verdict": "skip", "reason": "围栏外"}'
    )
    assert parse_json_object(text)["reason"] == "围栏外"


def test_multiple_fences_first_invalid_second_valid() -> None:
    text = (
        '```json\n不是 JSON\n```\n```json\n{"score": 3, "verdict": "skip", "reason": "第二块"}\n```'
    )
    assert parse_json_object(text)["reason"] == "第二块"


def test_candidates_order_fences_before_bare() -> None:
    candidates = extract_json_candidates('```json\n{"a": 1}\n```\n尾部 {"b": 2}')
    assert candidates[0] == '{"a": 1}'


# ---- 纯函数：字符串内的花括号/引号 ----


def test_braces_inside_string_values_do_not_break_scan() -> None:
    text = '说明 {"score": 50, "verdict": "neutral", "reason": "包含 } 与 { 的理由"} 备注'
    obj = parse_json_object(text)
    assert obj["reason"] == "包含 } 与 { 的理由"


def test_escaped_quotes_inside_strings() -> None:
    text = '{"score": 9, "verdict": "skip", "reason": "he said \\"hi {ok}\\""}'
    assert parse_json_object(text)["reason"] == 'he said "hi {ok}"'


# ---- 纯函数：非法输入 ----


def test_pure_prose_raises() -> None:
    with pytest.raises(AIUnparseableError):
        parse_json_object("抱歉，我无法完成这个任务。")


def test_top_level_array_degrades_to_first_object() -> None:
    """顶层数组包裹对象 → 退化取首个对象（仍需过 schema 校验）。"""
    obj = parse_json_object('[{"score": 1}, {"score": 2}]')
    assert obj == {"score": 1}


def test_non_object_json_rejected() -> None:
    """纯标量/纯数组/纯文本（无任何对象）→ 无法提取，抛异常。"""
    for text in ("[1, 2, 3]", '"一段文字"', "12345", "true"):
        with pytest.raises(AIUnparseableError):
            parse_json_object(text)


def test_empty_output_raises() -> None:
    with pytest.raises(AIUnparseableError):
        parse_json_object("   \n  ")


def test_unterminated_json_rejected() -> None:
    with pytest.raises(AIUnparseableError):
        parse_json_object('{"score": 40, "verdict": "neu')


def test_single_quoted_json_rejected_strictly() -> None:
    """json.loads 严格模式：单引号对象非法（靠重试与换模型，不做修复）。"""
    with pytest.raises(AIUnparseableError):
        parse_json_object("{'score': 10, 'verdict': 'skip', 'reason': 'x'}")


def test_error_message_contains_snippet() -> None:
    with pytest.raises(AIUnparseableError, match="输出片段"):
        parse_json_object("完全不是 JSON 的一段长文本" * 5)


# ---- call_json 全链路 ----


def test_call_json_returns_validated_model() -> None:
    client, transport = make_client(
        ['```json\n{"score": 82, "verdict": "recommend", "reason": "技能重合"}\n```']
    )
    out = client.call_json(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], Sample
    )
    assert out == Sample(score=82, verdict="recommend", reason="技能重合")
    # 消息原样送达传输层
    assert transport.calls == [
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    ]


def test_call_json_schema_violation_raises_after_retries() -> None:
    """score 越界（>100）→ Pydantic 校验失败 → 重试耗尽 → AIUnparseableError。"""
    client, transport = make_client(['{"score": 150, "verdict": "skip", "reason": "x"}'] * 3)
    with pytest.raises(AIUnparseableError, match="Sample"):
        client.call_json([{"role": "user", "content": "u"}], Sample)
    assert len(transport.calls) == 3


def test_call_json_verdict_out_of_enum_rejected() -> None:
    client, _ = make_client(['{"score": 10, "verdict": "excellent", "reason": "x"}'] * 3)
    with pytest.raises(AIUnparseableError):
        client.call_json([{"role": "user", "content": "u"}], Sample)


def test_call_json_extra_keys_tolerated() -> None:
    """多余键不触发失败（wire schema extra=ignore 语义由各 schema 自定）。"""
    client, _ = make_client(['{"score": 1, "verdict": "skip", "reason": "r", "confidence": 0.9}'])
    out = client.call_json([{"role": "user", "content": "u"}], Sample)
    assert out.score == 1


def test_call_json_missing_reason_defaults() -> None:
    client, _ = make_client(['{"score": 2, "verdict": "neutral"}'])
    out = client.call_json([{"role": "user", "content": "u"}], Sample)
    assert out.reason == ""
