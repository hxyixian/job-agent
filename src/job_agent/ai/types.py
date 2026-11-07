"""AI 层契约类型：OpenAI 兼容消息与评估结果（设计文档 §5.5 I4）。

类型归属（避免重复定义，models.py 是领域模型的唯一权威定义方）：

- ``ResumeSnapshot`` / ``ResumeText`` / ``JobDirection`` —— 已由 S3c 落在
  :mod:`job_agent.models`（§5.1 权威文件），本模块 **re-export**
  :class:`ResumeSnapshot`（S3b reply/classify 按 ``ai.types`` 路径导入同样
  成立），不重复定义；
- ``Evaluation`` —— §5.5 I4 引用但 models.py 未含，由本模块定义
  （S3a 交付，S4c/S5b 从这里或 ``job_agent.ai`` 导入）；
- ``ChatMessage`` —— AI 层私有的消息结构（call_json 输入单位）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

# re-export：权威定义在 models.py（S3c 交付）；ai 层各模块与 S3b 从
# ai.types / job_agent.models 任一路径导入得到的是同一个类
from job_agent.models import ResumeSnapshot

__all__ = [
    "ChatMessage",
    "Evaluation",
    "ResumeSnapshot",
    "Verdict",
]

# 评估判定（§9.1：recommend=建议投递 / neutral=基本匹配 / skip=不匹配跳过）
Verdict = Literal["recommend", "neutral", "skip"]

# skipped_by 留痕枚举（§9.1；与 evaluations 表列一致）：
#   low_score      score < min_score（S4c 判定回填）
#   verdict_skip   模型 verdict=skip（S4c 判定回填）
#   ai_error       AI 失败/超时/预算耗尽（evaluate_job 直接返回）
#   preference_hard 硬性偏好违反（v2 启用，V2-7）
SKIPPED_BY_AI_ERROR = "ai_error"


class ChatMessage(TypedDict):
    """OpenAI 兼容 chat 消息（call_json 的输入单位；role 受限三值）。"""

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass
class Evaluation:
    """单职位评估结果（evaluations 表行映射，§9.1）。

    - 成功路径：``skipped_by=None``——``low_score`` / ``verdict_skip`` 的
      归因需要 ``ai.min_score`` 与流水线判定（设计 §7 步 6：verdict != skip
      ∧ score >= min_score，矛盾从保守），属 S4c 职责，不在本层掺入；
    - 失败路径（失败/超时/预算耗尽）：``verdict="skip"`` +
      ``skipped_by="ai_error"`` [FR4.2]，score=0（宁缺毋滥）。
    """

    score: int  # 0-100
    verdict: Verdict
    reason: str
    skipped_by: str | None = None

    @classmethod
    def ai_error(cls, reason: str) -> Evaluation:
        """AI 失败时的保守结果：跳过该职位并留痕 [FR4.2/§9.1]。"""
        return cls(score=0, verdict="skip", reason=reason, skipped_by=SKIPPED_BY_AI_ERROR)
