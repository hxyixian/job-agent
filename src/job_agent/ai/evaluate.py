"""匹配评估（任务书 S3a 第 2 条；设计文档 §9.1 / §5.5 I4）。

``evaluate_job(job, resume, preferences) -> Evaluation``：

- prompt = **指令块**（角色/评分口径/输出 schema/数据块规则）+ **数据块
  隔离**（JD 与简历各自以 nonce 分隔标记包裹 [B12]，见 ai/prompt.py）；
- ``preferences`` 参数**恒在签名**（I4 契约），v1 恒空 [V2-7]；空表的
  行为 = 输入结构中不存在该字段 [N5]：不渲染任何偏好节、prompt 与
  "无此参数"逐字节一致（等价性由单测钉住）；
- 输出 schema：``{"score": 0-100, "verdict": "recommend|neutral|skip",
  "reason": str}``（Pydantic 校验，非法值触发 client 重试）；
- 失败/超时/预算耗尽 → 不抛出，返回 ``verdict="skip"`` +
  ``skipped_by="ai_error"`` 的保守结果（宁缺毋滥 [FR4.2]）——调用方
  S4c 无需 try/except，直接落 evaluations 留痕；
- 薪资不注入 AI 偏好 [R13]：JobDetail.salary_text 不进 prompt。

接口要点（给 S3b/S4c）：``skipped_by`` 的 ``low_score`` / ``verdict_skip``
归因需要 min_score 与流水线判定（设计 §7 步 6），由 S4c 回填；
``evaluate_job`` 成功时恒 ``skipped_by=None``。
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from job_agent.ai.client import AIClient, get_default_client
from job_agent.ai.errors import AIError
from job_agent.ai.prompt import (
    format_data_block_instruction,
    new_data_nonce,
    render_resume,
    wrap_untrusted,
)
from job_agent.ai.types import ChatMessage, Evaluation, Verdict
from job_agent.config import PreferenceRule
from job_agent.models import JobDetail, ResumeSnapshot

__all__ = [
    "EvalOutput",
    "build_messages",
    "evaluate_job",
]

# 数据块标签（包裹不可信的 JD / 简历文本 [B12]）
_JOB_LABEL = "职位描述与基本信息"
_RESUME_LABEL = "候选人简历"

_SYSTEM_INSTRUCTION = """\
你是简历-职位匹配评估器。请依据用户消息中数据块包裹的【{_JOB_LABEL}】与\
【{_RESUME_LABEL}】，评估候选人对该职位的匹配程度。

评分口径：
- score：0-100 整数，综合评估技能与职位要求的重合度、经验年限符合度、\
方向一致性；
- verdict：recommend（明显匹配，建议投递）/ neutral（基本匹配，可投递）\
/ skip（不匹配或存在硬伤，建议跳过）；
- reason：一句话中文理由，指出最关键的判断依据。

{data_block_rule}
{preferences_block}\
输出要求：只输出一个 JSON 对象，不要输出任何其他文字。格式：
{{"score": <0-100 整数>, "verdict": "recommend|neutral|skip", "reason": "<理由>"}}"""

_PREFERENCES_HEADER = "用户偏好规则（优先级高于默认评分口径）：\n"
_HARD_RULE = "- （硬性）{text} ——明确违反时 verdict 必须为 skip\n"
_SOFT_RULE = "- （软性）{text} ——仅影响 score 高低\n"


class EvalOutput(BaseModel):
    """模型输出的 wire schema（§9.1；score/verdict/reason）。

    extra="ignore"：模型附带多余键（如 confidence）不触发重试，
    缺字段/越界/枚举外值仍校验失败 → 重试 → :class:`AIUnparseableError`。
    """

    model_config = ConfigDict(extra="ignore")

    score: int = Field(ge=0, le=100)
    verdict: Verdict
    reason: str = ""


def _render_preferences(preferences: Sequence[PreferenceRule] | None) -> str:
    """偏好规则块；**空表/None 均返回空串**（N5：空表行为=无该字段）。"""
    if not preferences:
        return ""
    lines = [_PREFERENCES_HEADER]
    for rule in preferences:
        template = _HARD_RULE if rule.strength == "hard" else _SOFT_RULE
        lines.append(template.format(text=rule.text))
    return "".join(lines) + "\n"


def _render_job(job: JobDetail) -> str:
    """JobDetail → 数据块内的职位文本。

    注意：``salary_text`` **不进 prompt** [R13]——DOM 薪资字体加密，
    原文可能是加密字形，且薪资维度已废止、不注入 AI 偏好。
    """
    tags = "、".join(t for t in job.tags if t)
    lines = [
        f"职位：{job.title}",
        f"公司：{job.company_name}（{job.company_scale}）",
        f"经验要求：{job.experience}",
        f"职位标签：{tags if tags else '（无）'}",
        "职位描述：",
        job.jd_text,
    ]
    return "\n".join(lines)


def build_messages(
    job: JobDetail,
    resume: ResumeSnapshot,
    preferences: Sequence[PreferenceRule] | None,
    *,
    nonce: str | None = None,
) -> list[ChatMessage]:
    """构造评估 prompt（system 指令块 + user 数据块）。

    - ``preferences=None`` 与 ``preferences=[]`` 产出**完全相同**的
      消息（N5 验收口径；等价性单测钉住）；
    - ``nonce``：数据块分隔标记的一次性随机值（缺省每次生成）；
      测试与 S3b 复用时传固定值以获得确定输出。
    """
    n = new_data_nonce() if nonce is None else nonce
    system = _SYSTEM_INSTRUCTION.format(
        _JOB_LABEL=_JOB_LABEL,
        _RESUME_LABEL=_RESUME_LABEL,
        data_block_rule=format_data_block_instruction(n),
        preferences_block=_render_preferences(preferences),
    )
    user = "\n\n".join(
        [
            "请评估以下数据中职位与候选人简历的匹配度。",
            wrap_untrusted(_JOB_LABEL, _render_job(job), n),
            wrap_untrusted(_RESUME_LABEL, render_resume(resume), n),
            "请按系统指令的输出要求作答。",
        ]
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def evaluate_job(
    job: JobDetail,
    resume: ResumeSnapshot,
    preferences: list[PreferenceRule],
    *,
    client: AIClient | None = None,
) -> Evaluation:
    """评估单个职位（I4 契约签名；失败 → verdict=skip + skipped_by=ai_error）。

    - ``client`` 缺省用进程级默认客户端（get_default_client，预算随进程
      合计）；S4c/测试可显式注入；
    - 任何 :class:`AIError`（解析失败/传输失败/预算耗尽）都转换为保守
      结果返回，不向上抛 [FR4.2]——留痕给 evaluations，不中断投递循环。
    """
    effective_client = client if client is not None else get_default_client()
    messages = build_messages(job, resume, preferences)
    try:
        output = effective_client.call_json(messages, EvalOutput)
    except AIError as exc:
        return Evaluation.ai_error(f"AI 评估失败：{exc}")
    return Evaluation(score=output.score, verdict=output.verdict, reason=output.reason)
