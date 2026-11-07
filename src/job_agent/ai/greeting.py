"""招呼语生成与模板版本化（设计文档 §5.5 I4 / §9.3，Stage3 S3c）。

R12 机制背景：实测确认"点击『立即沟通』后由平台自动发送用户在平台
『打招呼语』设置中的预设内容"——**主动招呼语由平台发送**（定稿后由
用户粘贴到平台设置，见 FR5.2），生成候选仍属软件职责 [FR5.1]；
**被动招呼语由软件在会话中直接发送**（FR3.7）。

对外契约（§5.5 I4）::

    generate_greeting_candidates(kind, resume, directions) -> list[str]
        # kind ∈ {"active", "passive"}；恒 3 条候选 [FR5.1]
        # AI 层失败（AIUnparseableError/AITransportError/AIBudgetExhausted）
        # 原样上抛——工作台（S5b）向用户呈现，本层不静默降级

prompt 结构（§9.3 [B12]）：system prompt 固定包含**行为红线**（不编造
经历 / 不承诺薪资数字与具体时间 / 不主动留联系方式 [FR6.2/NFR1]）+
**结构化简历事实**（经 ``ai/prompt.render_resume`` 白名单渲染，四个
白名单字段之外不进 prompt）+ **求职目标方向**；数据块统一用
``ai/prompt`` 的 nonce 包裹件（随机标记，数据无法伪造提前闭合），
不各自定义分隔符。

greeting_templates 写库路径（R6-3/F1，供 S5b 控制台复用）：
- :func:`save_greeting_template`：定稿保存 = 清同 kind 旧 current →
  插入 ``version = max+1`` 的新 current 行（每 kind 单 current 由
  ``uq_greeting_current`` 部分唯一索引库层兜底，应用层先清后插）；
  新行 ``platform_synced_hash/at`` 为 NULL——任何修改使"已同步"确认
  自动失效 [FR5.3]；
- :func:`confirm_platform_synced`：控制台"已同步到平台设置"勾选，
  写入**当前内容哈希** + 确认时间；
- :func:`is_platform_synced`：F1 判定规则
  ``platform_synced_hash == 当前内容哈希`` 即视为已同步 [R6-3]。

AI 调用：经 ``ai/_client_ref`` 延迟定位 S3a 的 ``call_json``（重试/
预算/JSON 提取→Pydantic 校验统一由其负责 [B13]，共享进程级默认客户端
即共享日预算计数）。测试用 monkeypatch 替换 ``resolve_call_json``
注入桩（TEST1 全离线）。
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from job_agent.ai import _client_ref
from job_agent.ai import prompt as prompt_mod
from job_agent.ai.types import ChatMessage
from job_agent.db import Database
from job_agent.models import JobDirection, ResumeSnapshot
from job_agent.timeutil import utcnow_str

__all__ = [
    "GREETING_KINDS",
    "GreetingCandidates",
    "GreetingKind",
    "build_greeting_messages",
    "confirm_platform_synced",
    "current_greeting",
    "generate_greeting_candidates",
    "greeting_content_hash",
    "is_platform_synced",
    "save_greeting_template",
]

# §5.5 I4：kind 取值（与 greeting_templates.kind 的 CHECK 枚举一致）
GreetingKind = Literal["active", "passive"]
GREETING_KINDS: tuple[GreetingKind, ...] = ("active", "passive")

# FR5.1：每套恒 3 条候选
CANDIDATE_COUNT = 3

# 招呼语长度建议（写入 prompt 的软约束；硬性拦截在 S3b guard 的 >500 字符规则）
_LENGTH_HINT = "每条 60~150 个字符"

# 行为红线（§9.3，system prompt 固定包含）[FR6.2/NFR1/B12]
_REDLINES = """必须遵守以下行为红线：
1. 只能使用数据块中给出的简历事实与求职方向，不得编造任何经历、技能、项目或资质；
2. 不得出现任何薪资数字或薪资承诺（期望薪资、最低可接受等一律不写）；
3. 不得出现具体日期、时间或到岗时间承诺；
4. 不得出现任何联系方式（手机号、座机、邮箱、微信号、QQ 号等）；
5. 只输出候选文本本身，不输出编号、解释或多余的引导语。"""

# kind 专属任务说明（user 消息）
_TASK_ACTIVE = """任务：生成 3 条「主动招呼语」候选。
用途：用户投递岗位时使用的固定开场模板。定稿后由用户粘贴到 Boss直聘
「打招呼语」平台设置——点击「立即沟通」时平台自动发送该预设内容
（软件零额外发送），因此候选必须是可直接作为该预设的完整消息文本。
要求：简短自荐 + 与求职目标方向最相关的 1-2 个亮点 + 表达进一步沟通意愿；"""

_TASK_PASSIVE = """任务：生成 3 条「被动招呼语」候选。
用途：HR 主动来询后，软件在会话中直接发送的第一条回复（固定模板，
需适配任意 HR 的任意开场询问——不知道对方会问什么，不要试图回答
任何具体问题）。
要求：感谢来询 + 基于简历事实的简短自荐 + 表达进一步沟通意愿；"""

_TASK_TAIL = f"""{_LENGTH_HINT}；恰好 3 条。"""


class GreetingCandidates(BaseModel):
    """AI 输出 schema：恰好 3 条非空候选（数量/空白约束在 schema 层，
    由 call_json 的"校验失败重试"统一兜底 [B13]；条目自动去首尾空白）。"""

    candidates: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]] = (
        Field(min_length=CANDIDATE_COUNT, max_length=CANDIDATE_COUNT)
    )


def _format_directions(directions: list[JobDirection]) -> str:
    """求职方向 → 数据块文本（是否仅传已确认方向由调用方决定）。"""
    if not directions:
        return "（空：用户尚未确认职位方向，请生成通用型招呼语）"
    lines = []
    for index, direction in enumerate(directions, start=1):
        keywords = "、".join(direction.keywords) if direction.keywords else "（无关键词）"
        line = f"{index}. 关键词：{keywords}"
        if direction.rationale:
            line += f" —— 依据：{direction.rationale}"
        lines.append(line)
    return "\n".join(lines)


def build_greeting_messages(
    kind: GreetingKind,
    resume: ResumeSnapshot,
    directions: list[JobDirection],
    nonce: str | None = None,
) -> list[ChatMessage]:
    """组装招呼语 prompt（§9.3：system = 红线 + 简历事实 + 方向；user = 任务）。

    - ``nonce`` 缺省每次随机生成（ai/prompt 契约）；测试可显式传入固定
      nonce 以获得可断言的标记字面值；
    - 简历事实经 ``render_resume`` 白名单渲染（四个白名单字段之外不进
      prompt，联系方式无侧信道 [B12]），简历与方向分别以 nonce 数据块
      包裹后进入 system 消息（§9.3 字面要求：system prompt 固定包含）。
    """
    if kind not in GREETING_KINDS:
        raise ValueError(f"未知招呼语类型：{kind!r}（合法值：{'/'.join(GREETING_KINDS)}）")
    n = nonce if nonce is not None else prompt_mod.new_data_nonce()
    facts_block = prompt_mod.wrap_untrusted(
        "结构化简历事实（已剔除联系方式）",
        prompt_mod.render_resume(resume) or "（简历事实为空）",
        n,
    )
    directions_block = prompt_mod.wrap_untrusted("求职目标方向", _format_directions(directions), n)
    system = "\n".join(
        [
            "你是 Boss直聘求职助手，负责为用户撰写招呼语候选。",
            _REDLINES,
            prompt_mod.format_data_block_instruction(n),
            facts_block,
            directions_block,
        ]
    )
    task = _TASK_ACTIVE if kind == "active" else _TASK_PASSIVE
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=task + _TASK_TAIL),
    ]


def generate_greeting_candidates(
    kind: GreetingKind,
    resume: ResumeSnapshot,
    directions: list[JobDirection],
) -> list[str]:
    """生成招呼语候选（§5.5 I4 [FR5.1]）。

    - ``kind``：``"active"`` 主动（投递用，R12 下最终由平台发送）/
      ``"passive"`` 被动（HR 来询回复用，软件直接发送）；
    - 返回恒 ``CANDIDATE_COUNT``（3）条候选，已去除首尾空白；
    - AI 层失败（解析/预算/超时，含 S3a 缺席的 ImportError）原样上抛，
      本层不静默降级（宁可不生成，不生成劣质候选）。
    """
    messages = build_greeting_messages(kind, resume, directions)
    # AI 异常族（AIUnparseableError/AITransportError/AIBudgetExhausted）原样上抛
    call_json = _client_ref.resolve_call_json()
    result = call_json(messages, GreetingCandidates)
    candidates = [item.strip() for item in result.candidates]
    if len(candidates) != CANDIDATE_COUNT or not all(candidates):
        # 正常路径不可达（schema 已约束恰好 3 条非空）；防御契约漂移/旁路注入
        raise ValueError(
            f"AI 返回的候选数量非法（期望恰好 {CANDIDATE_COUNT} 条）：{len(candidates)}"
        )
    return candidates


# ---- greeting_templates 写库路径（R6-3/F1；S5b 控制台的唯一写入口）----


def greeting_content_hash(content: str) -> str:
    """模板内容哈希（F1 同步确认的绑定口径：SHA-256 hex）。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def is_platform_synced(content: str, synced_hash: str | None) -> bool:
    """F1 判定规则 [R6-3]：``platform_synced_hash == 当前内容哈希`` = 已同步。

    模板任何修改 → 内容哈希变化 → 判定自动失效（无需显式清除列值）[FR5.3]。
    """
    return synced_hash is not None and synced_hash == greeting_content_hash(content)


def save_greeting_template(db: Database, kind: GreetingKind, content: str) -> int:
    """定稿保存（版本链 + R6-3 单 current），返回新行 id。

    事务内三步：清同 kind 旧 current → ``version = max(version)+1`` →
    插入新 current 行。新行 ``platform_synced_hash/at`` 为 NULL（未同步），
    故任何重新定稿都使同步确认失效 [FR5.3]；``uq_greeting_current``
    部分唯一索引在库层兜底"每 kind 至多一个 current"。
    """
    if kind not in GREETING_KINDS:
        raise ValueError(f"未知招呼语类型：{kind!r}（合法值：{'/'.join(GREETING_KINDS)}）")
    if not content.strip():
        raise ValueError("招呼语内容不能为空")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE greeting_templates SET is_current=0 WHERE kind=? AND is_current=1", (kind,)
        )
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM greeting_templates WHERE kind=?", (kind,)
        ).fetchone()
        next_version = int(row["v"]) + 1
        cursor = conn.execute(
            "INSERT INTO greeting_templates (kind, content, version, is_current)"
            " VALUES (?, ?, ?, 1)",
            (kind, content, next_version),
        )
        return int(cursor.lastrowid or 0)


def current_greeting(db: Database, kind: GreetingKind) -> sqlite3.Row | None:
    """读取指定 kind 的当前模板行（无定稿返回 None）。

    调用方用 :func:`is_platform_synced` 判定 F1 同步态：
    ``is_platform_synced(row["content"], row["platform_synced_hash"])``。
    """
    if kind not in GREETING_KINDS:
        raise ValueError(f"未知招呼语类型：{kind!r}（合法值：{'/'.join(GREETING_KINDS)}）")
    return db.query_one(
        "SELECT id, kind, content, version, created_at, is_current,"
        " platform_synced_hash, platform_synced_at"
        " FROM greeting_templates WHERE kind=? AND is_current=1",
        (kind,),
    )


def confirm_platform_synced(db: Database, template_id: int) -> None:
    """记录"已同步到平台设置"确认 [FR5.3]：写入当前内容哈希 + 确认时间。

    哈希与**确认时的内容**绑定——此后内容被重新定稿（新版本行）时，
    新行列为 NULL，确认自然失效。
    """
    row = db.query_one("SELECT content FROM greeting_templates WHERE id=?", (template_id,))
    if row is None:
        raise ValueError(f"招呼语模板不存在：id={template_id}")
    db.execute(
        "UPDATE greeting_templates SET platform_synced_hash=?, platform_synced_at=? WHERE id=?",
        (greeting_content_hash(row["content"]), utcnow_str(), template_id),
    )
