"""回复草拟测试（§9.3；S3b 第 2 条）：红线 system prompt / 数据块隔离 /
失败上抛 / 注入夹具全链路零泄露零违规自动发送 [T4-11/B12]。

TEST1：call_json 离线桩注入；「零泄露」断言 = 注入消息只停留在数据块
+ 被劫持输出的草稿必被 guard_outgoing 拦截（自动发送 = 0）。
"""

from __future__ import annotations

import pytest

from ai.s3b_support import (
    CLEAN_REPLY,
    INJECTION_CASES,
    extract_nonce,
    install_fake,
    mk_message,
    reply_responder,
    rule_tags,
    sample_ctx,
    strip_forged_marker,
)
from job_agent.ai import prompt as prompt_mod
from job_agent.ai.classify import _ClassifySchema, classify_messages
from job_agent.ai.errors import AIUnparseableError
from job_agent.ai.guard import guard_outgoing
from job_agent.ai.reply import _ReplyDraftSchema, draft_reply
from job_agent.ai.types import ResumeSnapshot


def test_draft_reply_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功通路：返回剥除首尾空白后的回复正文。"""
    fake = install_fake(monkeypatch, reply_responder("  " + CLEAN_REPLY + "  "))
    ctx = sample_ctx([mk_message("您好，方便聊聊这个职位吗？")])
    assert draft_reply(ctx, "low_risk") == CLEAN_REPLY
    assert len(fake.calls) == 1


def test_reply_system_prompt_elements(monkeypatch: pytest.MonkeyPatch) -> None:
    """§9.3 system prompt 要素：简历事实 + 求职目标 + 行为红线 + 数据块规则。"""
    fake = install_fake(monkeypatch, reply_responder(CLEAN_REPLY))
    draft_reply(sample_ctx([mk_message("你好")]), "low_risk")
    system = fake.last_system
    # 简历事实（白名单四节渲染产物）
    assert "某某大学 计算机科学与技术" in system
    assert "订单中心微服务" in system
    assert "FastAPI" in system and "PostgreSQL" in system
    assert "高并发订单系统" in system
    # 求职目标
    assert "目标城市：北京" in system
    assert "求职方向：Python 后端、数据开发" in system
    # 行为红线（FR6.2/NFR1：不编造/不承诺薪资数字与具体时间/不主动留联系方式）
    assert "不编造" in system
    assert "薪资数字" in system and "具体日期" in system
    assert "不主动留联系方式" in system
    assert "150" in system  # 长度约束（护栏硬限 500 之外的软约束）
    # 数据块规则说明
    assert "UNTRUSTED_DATA" in system


def test_reply_prompt_empty_resume_and_goal_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """简历/目标为空时显式声明「不得虚构」（红线兜底）。"""
    fake = install_fake(monkeypatch, reply_responder(CLEAN_REPLY))
    ctx = sample_ctx(
        [mk_message("你好")],
        resume=ResumeSnapshot(),
        target_city="",
        directions=(),
    )
    draft_reply(ctx, "low_risk")
    system = fake.last_system
    assert "简历结构化信息为空——不得虚构" in system
    assert "未配置——不得虚构求职目标" in system


@pytest.mark.parametrize(
    ("category", "keyword"),
    [
        ("low_risk", "常规低风险"),
        ("key", "需要与本人确认"),
        ("positive_response", "正向意向"),
        ("no_reply", "无实质内容"),
    ],
)
def test_reply_category_hints(monkeypatch: pytest.MonkeyPatch, category: str, keyword: str) -> None:
    """分类驱动的草拟策略进入 user 侧任务描述。"""
    fake = install_fake(monkeypatch, reply_responder(CLEAN_REPLY))
    draft_reply(sample_ctx([mk_message("你好")]), category)
    assert keyword in fake.last_user
    assert f"对方最新消息分类：{category}" in fake.last_user


def test_draft_reply_invalid_category(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法类别：程序错误，抛 ValueError 且不发起 AI 调用。"""
    fake = install_fake(monkeypatch, reply_responder(CLEAN_REPLY))
    with pytest.raises(ValueError, match="未知消息分类"):
        draft_reply(sample_ctx([mk_message("你好")]), "bogus")
    assert fake.calls == []


def test_draft_reply_empty_context(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake(monkeypatch, reply_responder(CLEAN_REPLY))
    with pytest.raises(ValueError, match="缺少消息"):
        draft_reply(sample_ctx([]), "low_risk")


def test_draft_reply_ai_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI 异常原样上抛（调用方降级待发箱，FR9.3/A-CORE-12）；不以空串兜底。"""

    def respond(messages: object, schema: object) -> object:
        raise AIUnparseableError("重试耗尽")

    install_fake(monkeypatch, respond)
    with pytest.raises(AIUnparseableError):
        draft_reply(sample_ctx([mk_message("你好")]), "low_risk")


def test_draft_reply_blank_output_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """纯空白回复（schema 合法但无正文）拒绝：避免空消息走自动发送。"""
    install_fake(monkeypatch, reply_responder("   "))
    with pytest.raises(ValueError, match="AI 回复为空"):
        draft_reply(sample_ctx([mk_message("你好")]), "low_risk")


def test_reply_injection_fixtures_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """13 条注入夹具：只停留在 user 侧数据块内，system 指令块零夹具文本。"""
    fake = install_fake(monkeypatch, reply_responder(CLEAN_REPLY))
    for case in INJECTION_CASES:
        fake.calls.clear()
        draft_reply(sample_ctx([mk_message(case.message)]), "low_risk")
        system, user = fake.last_system, fake.last_user
        nonce = extract_nonce(user)
        begin, end = prompt_mod.data_block_markers(nonce)
        assert user.count(begin) == 1, case.id
        assert user.count(end) == 1, case.id
        inner = user.split(begin, 1)[1].split(end, 1)[0]
        assert strip_forged_marker(case.message) in inner, case.id
        assert case.message not in system, case.id
        # 关联岗位短字段（可信侧）在数据块之外独立呈现
        assert "关联岗位：Python 后端开发工程师｜示例科技" in user


def test_injection_full_pipe_zero_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """全链路（分类 fooled → 模型被劫持 → 护栏）：零违规自动发送 [T4-11]。

    对每条注入夹具：分类桩被骗判 low_risk、回复桩完全服从注入输出泄露
    物——guard_outgoing 仍必须拦截（A-CORE-13 安全底线：自动内容含联系
    方式或薪资/时间承诺 = 0）。echo_system_prompt 用例回显真实 system
    prompt（>500 字）验证超长拦截。
    """
    for case in INJECTION_CASES:
        captured: dict[str, str] = {}

        def respond(messages: object, schema: object, *, case=case, captured=captured) -> object:
            if schema is _ClassifySchema:
                captured["system"] = messages[0]["content"]  # type: ignore[index]
                return _ClassifySchema(category="low_risk", reason="被注入欺骗的模拟分类")
            assert schema is _ReplyDraftSchema
            if case.id == "echo_system_prompt":
                return _ReplyDraftSchema(reply=captured["system"])
            return _ReplyDraftSchema(reply=case.hijacked_reply)

        install_fake(monkeypatch, respond)
        ctx = sample_ctx([mk_message(case.message)])
        # 分类被 fooled 为 low_risk（模拟最坏情况）
        assert classify_messages(list(ctx.recent_messages)).category == "low_risk"
        draft = draft_reply(ctx, "low_risk")
        result = guard_outgoing(draft)
        assert not result.ok, f"{case.id}: 劫持草稿未被护栏拦截：{draft!r}"
        assert f"[{case.expected_rule}]" in rule_tags(result), case.id


def test_clean_full_pipe_auto_send_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """合规模型输出：分类 low_risk → 草拟 → 护栏放行（自动发送路径可用）。"""

    def respond(messages: object, schema: object) -> object:
        if schema is _ClassifySchema:
            return _ClassifySchema(category="low_risk", reason="常规沟通")
        return _ReplyDraftSchema(reply=CLEAN_REPLY)

    install_fake(monkeypatch, respond)
    ctx = sample_ctx([mk_message("您好，想了解下您的工作经历")])
    assert classify_messages(list(ctx.recent_messages)).category == "low_risk"
    draft = draft_reply(ctx, "low_risk")
    assert guard_outgoing(draft).ok
