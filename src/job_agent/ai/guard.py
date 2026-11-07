"""发送护栏（设计文档 §9.4；任务书 Stage3 S3b 第 3 条）——纯规则、同步可测。

契约（§5.5 I4）::

    guard_outgoing(text: str) -> GuardResult   # .ok / .reasons

自动发送（§8.2 low_risk 路径）前检查，命中任一规则产生一条
``GuardViolation(reason)``（``ok=False`` → 降级待发箱并标注原因 [B12]）：

① 联系方式正则：手机号 / 邮箱 / 微信号（含交换微信意图）/ QQ号；
② 薪资数字承诺：「期望XX k」「最低XX」类 cue+数字模式；
③ 具体日期时间 + 到岗语义：消息中同时出现具体日期 token（今天/明天/
   周X/X号/X月X号/月底…）与到岗语义 token（到岗/入职/报到/上班…）；
④ 长度 > 500 字符。

设计取向——**宁可误拦，不可漏放**：误拦只是降级待发箱（人工放行），
漏放则违规内容被自动发送（T4-11 安全底线）。因此：

- 规则③只要求两类 token 共现（不限定语序/句距）：「到岗时间可以协商」
  这类无具体日期的正常表述不拦；「上班地点在2号线」这类「数字+号」的
  地址写法通过日期 token 的负向排除（号线/号楼/号路…）避免误拦；
- 正则保守匹配（手机号带边界与分隔变体、微信号需 cue 词等），
  覆盖常见变体而非穷举（中文数字谐音等极端变体不在 v1 范围）。

本模块不依赖 ai 包其他模块（纯 stdlib），可在任何层直接调用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "MAX_OUTGOING_CHARS",
    "GuardResult",
    "GuardViolation",
    "guard_outgoing",
]

#: 规则④：自动发送文本长度上限（>500 违规）
MAX_OUTGOING_CHARS = 500

# ---- 规则①：联系方式 ----

# 大陆手机号：11 位（1[3-9] 开头），可带 +86/86 前缀；前后无数字防长串误命中
_PHONE_PLAIN_RE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")
# 分隔变体：138-0013-8000 / 138 0013 8000
_PHONE_SPACED_RE = re.compile(r"(?<!\d)1[3-9]\d[-\s]?\d{4}[-\s]?\d{4}(?!\d)")

# 邮箱：本地部@域.后缀（多级域名支持）
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

# 微信号：cue 词（微信/weixin/wechat/vx/wx/v信/威信）+ 号码形态 ID，
# 或「加/换/交换/留/发/报 + cue」的交换意图，或「cue + 聊/沟通/联系」
_WECHAT_ID = r"[a-zA-Z][-_a-zA-Z0-9]{5,19}"
_WECHAT_CUE = r"(?i:weixin|wechat|vx|wx|v信)|微信|威信"
_WECHAT_RE = re.compile(
    rf"(?<![a-zA-Z0-9])(?:{_WECHAT_CUE})(?:\s*(?:号|ID|id)?\s*[:：]?\s*{_WECHAT_ID})"
    rf"|(?:加|换|留|交换|发|报)[^\n]{{0,6}}(?:{_WECHAT_CUE})"
    rf"|(?:{_WECHAT_CUE})[^\n]{{0,6}}(?:聊|沟通|联系|详聊)"
)

# QQ号：cue 词（QQ/Q群/扣扣/企鹅）+ 5-11 位数字
_QQ_RE = re.compile(r"(?<![a-zA-Z0-9])(?i:qq|q群|扣扣|企鹅)\s*(?:号|群)?\s*[:：]?\s*\d{5,11}(?!\d)")

# ---- 规则②：薪资数字承诺 ----

_SALARY_UNIT = r"(?:k|K|w|W|千|万|元|块)"
# 数字不得再接数字（防「接受18号」回溯成「接受1」命中）或这些量词
# （「至少3年经验」「希望10月入职」「接受18号面试」不是薪资承诺）
_SALARY_EXCL = r"(?![\d年个次人条天位分钟月周点号日时折])"
_SALARY_CUE = (
    r"(?:期望|最低|至少|不低于|不少于|要价|报价|谈到|开到|接受|希望"
    r"|月薪|年薪|薪资|工资|待遇|报酬|到手)"
)
_SALARY_RE = re.compile(
    # cue（+可选薪资词）+ 数字 + 可选单位
    rf"{_SALARY_CUE}(?:薪资|工资|待遇|薪水|报酬|价)?[^\d\n]{{0,6}}\d+(?:\.\d+)?\s*{_SALARY_UNIT}?{_SALARY_EXCL}"
    # 弱 cue（要/得）须带单位：「我要12k」
    rf"|(?:要|得)[^\d\n]{{0,4}}\d+(?:\.\d+)?\s*{_SALARY_UNIT}{_SALARY_EXCL}"
    # 数字在前：「15k以上都可以」
    rf"|\d+(?:\.\d+)?\s*[kKwW]\s*(?:以上|以下|起)?\s*(?:也)?(?:都)?"
    r"(?:可以|接受|能行|行|没问题|OK|ok|考虑|谈)"
)

# ---- 规则③：具体日期 + 到岗语义 ----

# 具体日期 token；「数字+号/日」后接 线/楼/路… 为地址写法，排除
_DATE_TOKEN_RE = re.compile(
    r"今天|明天|后天|大后天"
    r"|(?:下|本)周[一二三四五六日天]|下(?:周|个月)|周[一二三四五六日天]|周末"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*[号日](?![线楼栋室路层座])"
    r"|\d{1,2}\s*[号日](?![线楼栋室路层座])"
    r"|月底|月初|月中|年底|年初"
)
# 到岗语义 token
_ONBOARD_TOKEN_RE = re.compile(r"到岗|入职|报到|上岗|上班|开始工作")

_REASON_CONTACT_PHONE = "[contact/phone] 规则①：疑似包含手机号"
_REASON_CONTACT_EMAIL = "[contact/email] 规则①：疑似包含邮箱"
_REASON_CONTACT_WECHAT = "[contact/wechat] 规则①：疑似包含微信号或交换微信意图"
_REASON_CONTACT_QQ = "[contact/qq] 规则①：疑似包含QQ号"
_REASON_SALARY = "[salary] 规则②：疑似薪资数字承诺"
_REASON_ONBOARD = "[onboard_time] 规则③：疑似具体日期+到岗时间承诺"


@dataclass(frozen=True)
class GuardViolation:
    """单条违规（任务书：命中任一规则 → GuardViolation(reason)）。

    reason 以 ``[规则tag] 规则①-④：说明`` 开头，供待发箱标注与测试断言。
    """

    reason: str


@dataclass(frozen=True)
class GuardResult:
    """护栏检查结果（§5.5 I4：.ok / .reasons）。

    ``ok`` 由 reasons 推导（无违规即 True），两者恒一致。
    """

    reasons: tuple[GuardViolation, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.reasons


def guard_outgoing(text: str) -> GuardResult:
    """对拟自动发送文本执行四规则检查（§9.4；收集全部命中，非短路）。"""
    violations: list[GuardViolation] = []
    if _PHONE_PLAIN_RE.search(text) or _PHONE_SPACED_RE.search(text):
        violations.append(GuardViolation(_REASON_CONTACT_PHONE))
    if _EMAIL_RE.search(text):
        violations.append(GuardViolation(_REASON_CONTACT_EMAIL))
    if _WECHAT_RE.search(text):
        violations.append(GuardViolation(_REASON_CONTACT_WECHAT))
    if _QQ_RE.search(text):
        violations.append(GuardViolation(_REASON_CONTACT_QQ))
    if _SALARY_RE.search(text):
        violations.append(GuardViolation(_REASON_SALARY))
    if _DATE_TOKEN_RE.search(text) and _ONBOARD_TOKEN_RE.search(text):
        violations.append(GuardViolation(_REASON_ONBOARD))
    if len(text) > MAX_OUTGOING_CHARS:
        violations.append(
            GuardViolation(f"[too_long] 规则④：长度 {len(text)}>{MAX_OUTGOING_CHARS}")
        )
    return GuardResult(tuple(violations))
