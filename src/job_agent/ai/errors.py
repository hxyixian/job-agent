"""AI 层异常分类（Stage3 S3a；S3b/S3c 复用，调用方按类处理）。

设计依据：任务书 S3a 第 1 条 + 设计文档 §9.1（失败/超时 = 该职位跳过，
宁缺毋滥 [FR4.2, B13]）。异常 → 调用方行为的映射：

=====================  =====================  ==============================
异常                    含义                    调用方行为
=====================  =====================  ==============================
AIBudgetExhausted      日预算耗尽 [B13]        停止评估与自动回复（存量草拟
                                               转人工）+ AI_BUDGET_EXHAUSTED
                                               中级通知（§5.5 I5 / §11）
AIUnparseableError     重试 ≤2 后仍无法得到     evaluate_job → verdict=skip +
                       schema 合法的 JSON       skipped_by=ai_error；
                                               classify 兜底 category="key"
AITransportError       网络/超时/认证等请求层   同上（保守跳过/兜底），
                       错误，重试 ≤2 后仍失败   不区分根因
=====================  =====================  ==============================

注意：本模块（含 ai 包）不得 import job_agent.core / adapters（设计 §2
依赖规则：ai 被各层调用但不反向依赖）；事件名以字符串常量约定，与
core/events.EVENT_LEVELS 的成员拼写一致（由单元测试钉住）。
"""

from __future__ import annotations

__all__ = [
    "AIBudgetExhausted",
    "AIError",
    "AITransportError",
    "AIUnparseableError",
]


class AIError(Exception):
    """AI 层异常基类（调用方按 §9.1 的保守语义处理）。"""


class AIBudgetExhausted(AIError):
    """当日 AI 调用数达到 ``ai.daily_budget_calls``（评估+分类+生成合计）[B13]。

    触发方在 AIClient 内部（调用前熔断）；捕获方应：当日不再发起任何
    AI 调用（评估与自动回复停摆、存量草拟转人工）并发中级通知。
    """


class AIUnparseableError(AIError):
    """模型输出经「代码块/裸 JSON 兜底提取 + Pydantic 校验 + 重试 ≤2」
    仍无法得到目标 schema 的合法对象。"""


class AITransportError(AIError):
    """请求层错误（网络/超时/代理/认证等），重试 ≤2 后仍失败。"""
