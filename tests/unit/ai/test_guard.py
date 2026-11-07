"""护栏四规则正/负例 + 注入夹具零违规（A-CORE-13 / T4-11 / B12）。

验收映射：A-CORE-13 指定用例 ``test_injection_fixtures`` 在本文件——
护栏为纯规则（无 AI 依赖），注入零泄露的确定性保证来自本层：
即使模型被完全劫持（服从注入要求输出泄露物），自动发送前的
guard_outgoing 也必须拦截（零违规自动发送）。
"""

from __future__ import annotations

from ai.s3b_support import CLEAN_REPLY, INJECTION_CASES, rule_tags
from job_agent.ai.guard import MAX_OUTGOING_CHARS, GuardResult, GuardViolation, guard_outgoing


def test_injection_fixtures() -> None:
    """13 条注入夹具：被劫持模型输出必须被护栏拦截（零违规自动发送）。

    断言方式：每条夹具给出「模型若服从注入会输出的回复」（含注入索取
    的手机号/邮箱/微信号/QQ号/薪资数字/到岗日期/系统提示词回显），
    ``guard_outgoing`` 必须 ``ok=False`` 且命中该夹具声明的规则 tag；
    对照组干净回复零命中（自动发送路径可放行）。
    """
    assert len(INJECTION_CASES) >= 10, "任务书要求注入夹具 10+ 变体"
    for case in INJECTION_CASES:
        result = guard_outgoing(case.hijacked_reply)
        assert not result.ok, f"{case.id}: 劫持输出未被护栏拦截：{case.hijacked_reply!r}"
        assert f"[{case.expected_rule}]" in rule_tags(result), (
            f"{case.id}: 期望规则 [{case.expected_rule}] 未命中，实际 {rule_tags(result)}"
        )
    # 对照组：干净回复零违规
    clean = guard_outgoing(CLEAN_REPLY)
    assert clean.ok and clean.reasons == ()


def test_rule_contact_phone() -> None:
    for text in [
        "我的手机号是13800138000",
        "电话 +8613800138000 随时联系",
        "直接打 138-0013-8000 找我",
        "138 0013 8000 这是手机",
    ]:
        assert "[contact/phone]" in rule_tags(guard_outgoing(text)), text
    for text in [
        "我 3 年经验，带过 12 人团队",
        "2024 年毕业，方向是后端开发",
        "期待您的回复，谢谢",
    ]:
        assert guard_outgoing(text).ok, text


def test_rule_contact_email() -> None:
    for text in [
        "简历发到 hire@example.com 谢谢",
        "我的邮箱 dev.mail+zh@sub.example.org",
    ]:
        assert "[contact/email]" in rule_tags(guard_outgoing(text)), text
    for text in [
        "稍后我把经历整理给您",
        "哪家公司？我查一下",
    ]:
        assert guard_outgoing(text).ok, text


def test_rule_contact_wechat() -> None:
    for text in [
        "可以加我微信 detailed_dev",
        "微信号：wx_88421",
        "weixin: tom_1988",
        "加个微信聊吧",
        "我们微信沟通细节",
        "VX: hello_2024",
    ]:
        assert "[contact/wechat]" in rule_tags(guard_outgoing(text)), text
    for text in [
        "感谢您的沟通，期待进一步交流",
        "项目经历我写在简历里了",
    ]:
        assert guard_outgoing(text).ok, text


def test_rule_contact_qq() -> None:
    for text in [
        "QQ:123456789",
        "qq号 998877",
        "扣扣1122334",
        "Q群 88866688",
    ]:
        assert "[contact/qq]" in rule_tags(guard_outgoing(text)), text
    for text in [
        "QA 流程熟悉",
        "群面体验可以聊聊",
    ]:
        assert guard_outgoing(text).ok, text


def test_rule_salary_promise() -> None:
    for text in [
        "期望15k",
        "最低12k",
        "期望薪资 20万",
        "年薪30万可以考虑",
        "至少8k",
        "我接受12k的offer",
        "我要15k",
        "15k以上都可以聊",
    ]:
        assert "[salary]" in rule_tags(guard_outgoing(text)), text
    for text in [
        "薪资期望可以详聊",
        "我 3 年经验",
        "期望尽快收到回复",
        "至少3年相关经验",
        "接受18号面试",
        "待遇构成是怎样的",
    ]:
        assert guard_outgoing(text).ok, text


def test_rule_onboard_date_promise() -> None:
    for text in [
        "我明天就可以到岗",
        "下周一一早入职",
        "到岗时间：12月1号",
        "月底前报到没问题",
        "周末来上班",
    ]:
        assert "[onboard_time]" in rule_tags(guard_outgoing(text)), text
    for text in [
        "到岗时间可以协商",
        "面试时间您定",
        "我目前在职，需要交接安排",
        "上班地点在2号线附近",
    ]:
        assert guard_outgoing(text).ok, text


def test_rule_length_limit() -> None:
    assert guard_outgoing("好" * MAX_OUTGOING_CHARS).ok
    result = guard_outgoing("好" * (MAX_OUTGOING_CHARS + 1))
    assert not result.ok
    assert "[too_long]" in rule_tags(result)


def test_result_shape_and_multi_violation() -> None:
    """GuardResult 结构（§5.5 I4：.ok/.reasons）与多规则同时收集。"""
    text = "我期望25k，手机号13800138000"
    result = guard_outgoing(text)
    assert isinstance(result, GuardResult)
    assert result.ok is False
    tags = rule_tags(result)
    assert "[contact/phone]" in tags
    assert "[salary]" in tags
    assert all(isinstance(v, GuardViolation) and v.reason for v in result.reasons)
    ok = guard_outgoing("好的，谢谢")
    assert ok.ok is True
    assert ok.reasons == ()
