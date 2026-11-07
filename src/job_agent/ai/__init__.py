"""AI 层（Stage3）：OpenAI 兼容客户端 + 匹配评估（S3a 交付）。

公开契约（设计文档 §5.5 I4 / §9.1）：

- :class:`AIClient` / :func:`call_json`——统一调用入口（模块级 ``call_json``
  委托进程级默认客户端；JSON 提取 → Pydantic 校验 → 重试 ≤2 →
  日预算熔断 [B13]）；
- :func:`evaluate_job`——职位匹配评估（失败 → verdict=skip +
  skipped_by=ai_error [FR4.2]）；
- 类型 :class:`Evaluation`（ai.types）与 :class:`ChatMessage`；
  ``ResumeSnapshot`` 的权威定义在 :mod:`job_agent.models`（S3c 交付，
  §5.1 唯一权威约定），本包不重复定义；
- 异常 :class:`AIError` 家族（含 :class:`AIBudgetExhausted`）。

后续 SubStage 落位：S3b classify/reply/guard、S3c greeting 与简历结构化
均复用本包的 client/prompt 工具，不各自另建客户端。
"""

from __future__ import annotations

from job_agent.ai.client import (
    AIClient,
    call_json,
    get_default_client,
    parse_json_object,
    set_default_client,
)
from job_agent.ai.errors import (
    AIBudgetExhausted,
    AITransportError,
    AIUnparseableError,
)
from job_agent.ai.evaluate import EvalOutput, build_messages, evaluate_job
from job_agent.ai.prompt import (
    data_block_markers,
    format_data_block_instruction,
    new_data_nonce,
    render_resume,
    wrap_untrusted,
)
from job_agent.ai.types import ChatMessage, Evaluation, Verdict

__all__ = [
    "AIBudgetExhausted",
    "AIClient",
    "AITransportError",
    "AIUnparseableError",
    "ChatMessage",
    "EvalOutput",
    "Evaluation",
    "Verdict",
    "build_messages",
    "call_json",
    "data_block_markers",
    "evaluate_job",
    "format_data_block_instruction",
    "get_default_client",
    "new_data_nonce",
    "parse_json_object",
    "render_resume",
    "set_default_client",
    "wrap_untrusted",
]
