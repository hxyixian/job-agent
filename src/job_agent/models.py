"""领域模型（设计文档 §5.1 —— 本模块是 models 的唯一权威定义方）。

下游（S2b 状态机/对账、S2c 调度、S4 adapters、S5 console）一律
``from job_agent.models import ...``，不得各自重复定义；字段增删以 §5.1 为准。

要点：
- JobRef 薪资仅保留 ``salary_text`` 展示位 [R13]：DOM 层薪资全线字体加密
  （v13 实测），原文（可能是加密字形）只作记录展示，不参与任何规则、
  不注入 AI 偏好；解析字段已删，``salary_overlap`` 判定已废止；
- ``Message.is_ours_auto`` 由核心层与 send_intents 匹配回填 [G6]，
  适配器不提供（恒为 None 交由上层补全）；
- ``MsgType`` / ``SendOutcome`` 的枚举值与 migrations/0001_init.sql 的
  CHECK 枚举对齐（messages.msg_type 为大写名称、send_intents.state 为
  小写），入库直接写 ``.value`` 即合法；
- ``LoginState`` / ``RiskSignal`` 语义见设计文档 §5.2（含 EVICTED /
  SECURITY_CHECK 的判定来源 [B6,B7,G2,H1]），定义按 Stage2 任务书 S2a
  落在本模块，adapters/base.py 复用；
- ``ResumeText`` / ``ResumeSnapshot`` / ``JobDirection``：§5.5 I4/I10
  所需类型，由 Stage3 S3c 落入本模块（I10 的提供方是 S3c）；
  ``evaluate_job``（S3a）等调用方一律从本模块导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, StrEnum
from typing import Literal

__all__ = [
    "Conversation",
    "JobDetail",
    "JobDirection",
    "JobRef",
    "LoginState",
    "Message",
    "MsgType",
    "ResumeSnapshot",
    "ResumeText",
    "RiskSignal",
    "SendOutcome",
]


@dataclass
class JobRef:
    """列表级岗位卡（搜索结果 / 会话关联岗位的最小信息集）。"""

    job_id: str  # 平台岗位 ID（去重与缓存键）
    title: str
    company_name: str
    company_key: str  # 去重键：优先平台公司/品牌 ID，后备规范化公司名 [B9]
    # 薪资原文（**可能是加密字形**）：仅供记录展示，不参与任何规则 [R13/F3]；解析字段已删
    salary_text: str | None
    tags: list[str]  # 列表标签（含福利标签）
    list_snippet: str  # 列表页摘要


@dataclass
class JobDetail(JobRef):
    """详情级岗位：JobRef 全字段 + 详情页补充信息。"""

    jd_text: str  # 职位描述全文
    experience: str  # 经验要求原文
    company_scale: str  # 公司规模原文
    detail_url: str


@dataclass
class Conversation:
    """会话列表项（一个 HR 会话）。"""

    conv_id: str
    peer_name: str  # HR 名称（入库脱敏由导出层负责）
    job_ref: JobRef | None  # 关联岗位 [B8]；None=取不到
    last_message_at: datetime
    unread: int


class MsgType(StrEnum):
    """消息形态（§5.1 str 枚举；值与 messages.msg_type 的 CHECK 枚举一致，大写名称）。"""

    TEXT = "TEXT"
    IMAGE = "IMAGE"
    VOICE = "VOICE"
    FILE = "FILE"
    JOB_CARD = "JOB_CARD"
    RESUME_REQUEST_CARD = "RESUME_REQUEST_CARD"
    CONTACT_REQUEST_CARD = "CONTACT_REQUEST_CARD"
    INTERVIEW_CARD = "INTERVIEW_CARD"
    SYSTEM = "SYSTEM"


@dataclass
class Message:
    """单条消息（多模态消息的 text 为空或说明文字）。"""

    platform_msg_id: str
    conv_id: str
    direction: Literal["in", "out"]
    sender: Literal["hr", "me", "system"]
    msg_type: MsgType
    text: str
    sent_at: datetime

    # 【核心层计算，适配器不提供 [G6]】我方消息与 send_intents 按
    # payload_digest + 时间窗（±10min）匹配；匹配不上 = None →
    # 按人工消息处理（转人工，保守）
    is_ours_auto: bool | None = None


class SendOutcome(StrEnum):
    """发送结果（§5.1 str 枚举；值与 send_intents.state 的 CHECK 枚举一致，小写）。"""

    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"
    PENDING = "pending"
    SKIPPED_DRYRUN = "skipped_dryrun"


class LoginState(Enum):
    """登录态（设计文档 §5.2，SiteAdapter.check_login_state 的返回）。"""

    LOGGED_IN = "logged_in"
    LOGGED_OUT = "logged_out"
    UNKNOWN = "unknown"


class RiskSignal(Enum):
    """风控信号（设计文档 §5.2 [B6,B7,G2,H1]）。

    - EVICTED：URL 变为 about:blank 或非我方发起的异常跳转；
    - SECURITY_CHECK：URL 含 _security_check/验证类路径，或页面出现
      "安全验证、环境异常、验证码"类文字，或导航后停留页非目标页连续 2 次。
    """

    NONE = "none"
    CAPTCHA = "captcha"
    RISK_PAGE = "risk_page"
    LOGIN_EXPIRED = "login_expired"
    ADAPTER_BROKEN = "adapter_broken"
    PLATFORM_LIMIT = "platform_limit"
    EVICTED = "evicted"
    SECURITY_CHECK = "security_check"


# ---- 简历与方向（§5.5 I4/I10 所需类型；定义方=S3c，见模块 docstring 的
# "唯一权威定义方"约定）----


@dataclass
class ResumeText:
    """pypdf 提取的简历全文（§5.5 I10：``parse_resume`` 输出 / ``structure_resume`` 输入）。

    ``source_pdf_hash``：源 PDF 文件字节流的 SHA-256（十六进制），
    随版本写入 ``resume_snapshots.source_pdf_hash``，用于溯源比对。
    """

    text: str
    source_pdf_hash: str | None = None


@dataclass
class ResumeSnapshot:
    """结构化简历（§5.5 I10 / §9.5：education/experience/skills/projects 四字段）。

    - 作为 AI 上下文的事实来源（招呼语候选/回复生成/匹配评估）[FR2.4]；
    - 每字段为字符串数组（每条一句概括），**联系方式已在结构化时剔除** [B12]；
    - 版本化入 ``resume_snapshots``，手动修正生成新版本 [FR2.5]。
    """

    education: list[str] = field(default_factory=list)
    experience: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)


@dataclass
class JobDirection:
    """职位方向（FR2.2：每组 = 搜索关键词 + 依据说明；``job_directions`` 表的内存形态）。

    ``generate_greeting_candidates``（§5.5 I4）以已确认方向为求职目标输入；
    方向为空表时生成通用型候选（不报错）。
    """

    keywords: list[str] = field(default_factory=list)
    rationale: str = ""
    is_confirmed: bool = False
