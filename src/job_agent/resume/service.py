"""简历解析装配层（设计文档 §5.5 I10 / §9.5 + FR2.2/2.3/2.5，Stage4 S4e）。

S3c（resume/parser.py）已提供解析/结构化/版本化**原语**；本模块是上传
后端逻辑的**装配层**（任务书 S4e：文件校验 → 解析 → 结构化 → 版本入库 →
手动修正读写 → 职位方向生成/确认流），供 S5b 控制台（简历与方向页）与
引擎装配调用，不重复实现任何解析细节。

上传链（每步失败均**不落库**——失败的上传不产生半成品版本）::

    upload_and_parse(db, pdf_path) -> UploadResult
        # ① 文件校验（仅 .pdf）② parse_resume（pypdf/扫描件判定 [B15]）
        # ③ structure_resume（AI 结构化，双侧剔除联系方式 [B12]）
        # ④ save_resume_snapshot（旧 current 清位 [R6-3]）
        # ②③ 抛 ScannedPdfError/AIStructureError（用户可见，不重试）。

当前版本读写::

    get_current_snapshot(db) -> ResumeSnapshot | None   # AI 事实来源 [FR2.4]
    get_current_version(db) -> ResumeVersion | None     # 含 id/hash/created_at
    apply_manual_edit(db, structured) -> ResumeVersion  # 手动修正→新版本 [FR2.5]

职位方向（FR2.2/2.3，job_directions 表 + 确认流）::

    directions_generate(db) -> list[JobDirection]    # AI 候选+依据（is_confirmed=0）
    directions_confirm(db, dirs)                     # 确认流收口：整体替换已确认集
    directions_edit(db, dirs)                        # 手动编辑入口（与 confirm 同语义）
        # 两者均为"整体替换"：调用方传回终选列表，增/删/改一次到位
        # [FR2.2 确认/编辑/增删]；确认/编辑后未确认候选行一并清位；
        # 手动编辑不需要先生成（生成不是唯一入口 [FR2.3]）
    get_directions(db) -> list[JobDirection]         # 全部行（候选+已确认，插入序）
    confirmed_directions(db) -> list[JobDirection]   # 已确认集（招呼语/前置条件③输入）

换简历联动提示（FR2.3，s4 决策）::

    UploadResult.regenerate_hint: bool
        # 替换简历（此前已有 current 版本）且存在**基于旧简历的产物**
        # （已确认方向 或 已定稿招呼语）→ True。**提示不强制失效**：
        # 不删除、不修改任何方向与招呼语行，仅返回标志，由 S5b 呈现
        # "建议重新生成职位方向与招呼语候选"。
        # 事件侧：EventType 无简历域成员（§5.5 I5 枚举为权威表），
        # 故提示以**返回标志**交付、不发通知事件。

方向生成的 AI 调用：经 ``ai/_client_ref`` 复用 S3a ``call_json``（重试/
预算/JSON 提取→Pydantic 校验统一由其负责 [B13]，共享进程级默认客户端
即共享日预算计数）；最终失败包装为 :class:`AIDirectionError`（用户可见，
出路=手动编辑方向 [FR2.3]）。简历事实经 ``ai/prompt.render_resume``
白名单渲染进 nonce 数据块 [B12]，与 greeting/parser 同一包裹件。

手动修正的联系方式防御 [B12]：输入经 ``parser.strip_contact_entries``
整条剔除（输出侧规则的一致延伸——入库内容即后续 AI 上下文的事实来源，
联系方式不得经手动修正回流任何 prompt）；剔除结果即为保存结果，
S5b 回显让用户可见。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from job_agent.ai import _client_ref
from job_agent.ai import prompt as prompt_mod
from job_agent.ai.types import ChatMessage
from job_agent.db import Database
from job_agent.models import JobDirection, ResumeSnapshot
from job_agent.resume import parser

__all__ = [
    "AIDirectionError",
    "DirectionCandidates",
    "ResumeVersion",
    "UploadResult",
    "apply_manual_edit",
    "build_direction_messages",
    "confirmed_directions",
    "directions_confirm",
    "directions_edit",
    "directions_generate",
    "get_current_snapshot",
    "get_current_version",
    "get_directions",
    "upload_and_parse",
]

# FR2.2：AI 每次生成的方向组数上限（prompt 要求 3~5 组，schema 上限放宽到 8
# 容忍模型轻微越界；数量/非空约束在 schema 层由 call_json 的校验重试兜底 [B13]）
MAX_DIRECTION_GROUPS = 8
# 每组关键词数上限（prompt 要求 2~4 个；≥1 为硬约束——空关键词组无法搜索）
MAX_KEYWORDS_PER_DIRECTION = 8


class AIDirectionError(parser.ResumeError):
    """AI 生成职位方向失败 [FR2.2]：不重试；用户可手动编辑方向（生成不是唯一入口 [FR2.3]）。"""


@dataclass(frozen=True)
class ResumeVersion:
    """resume_snapshots 单行的内存形态（S5b GET /resume/structured 的数据源）。"""

    id: int
    snapshot: ResumeSnapshot
    source_pdf_hash: str | None
    created_at: str


@dataclass(frozen=True)
class UploadResult:
    """上传解析结果（S5b POST /resume/upload 的响应体来源）。

    - ``version``：新入库的 current 版本；
    - ``replaced_previous``：本次是否替换了既有简历（此前已有 current 版本）；
    - ``regenerate_hint``：换简历联动提示 [FR2.3]——``replaced_previous``
      且存在基于旧简历的产物（已确认方向或已定稿招呼语）时 True；
      **提示不强制失效**（不改动任何方向/招呼语行）。
    """

    version: ResumeVersion
    replaced_previous: bool
    regenerate_hint: bool


# ---- 当前版本读写 [FR2.4/FR2.5] ----

_VERSION_COLUMNS = (
    "SELECT id, structured_json, source_pdf_hash, created_at"
    " FROM resume_snapshots WHERE is_current=1 ORDER BY id DESC"
)


def _row_to_version(row: sqlite3.Row) -> ResumeVersion:
    """resume_snapshots 行 → ResumeVersion（structured_json 反序列化）。"""
    return ResumeVersion(
        id=int(row["id"]),
        snapshot=parser.snapshot_from_json(row["structured_json"]),
        source_pdf_hash=row["source_pdf_hash"],
        created_at=str(row["created_at"]),
    )


def _current_version_row(db: Database) -> sqlite3.Row | None:
    return db.query_one(_VERSION_COLUMNS)


def get_current_version(db: Database) -> ResumeVersion | None:
    """读取当前版本（含 id/hash/created_at）；从未上传过返回 None。"""
    row = _current_version_row(db)
    return None if row is None else _row_to_version(row)


def get_current_snapshot(db: Database) -> ResumeSnapshot | None:
    """读取当前结构化简历（AI 上下文的事实来源 [FR2.4]）；从未上传过返回 None。"""
    row = _current_version_row(db)
    return None if row is None else parser.snapshot_from_json(row["structured_json"])


def _fetch_version(db: Database, version_id: int) -> ResumeVersion:
    """按 id 取行（插入后回读 created_at 等库默认列）。"""
    row = db.query_one(
        "SELECT id, structured_json, source_pdf_hash, created_at FROM resume_snapshots WHERE id=?",
        (version_id,),
    )
    assert row is not None, f"resume_snapshots 缺少刚插入的行 id={version_id}"
    return _row_to_version(row)


# ---- 上传链（任务书 S4e：文件校验 → 解析 → 结构化 → 版本入库）----


def _resume_dependents_exist(db: Database) -> bool:
    """是否存在基于旧简历的产物（已确认方向 或 已定稿招呼语）[FR2.3]。

    仅作换简历提示的判定输入，不影响两者的存留（提示不强制失效）。
    """
    if db.query_one("SELECT 1 FROM job_directions WHERE is_confirmed=1 LIMIT 1") is not None:
        return True
    return db.query_one("SELECT 1 FROM greeting_templates WHERE is_current=1 LIMIT 1") is not None


def upload_and_parse(db: Database, pdf_path: str | Path) -> UploadResult:
    """上传 → 解析 → 结构化 → 入库为新 current 版本 [FR2.1/FR2.5，R6-3]。

    - 文件校验：仅接受 ``.pdf`` 后缀（其余直接拒绝，不尝试解析）；
    - ``parse_resume`` / ``structure_resume`` 的用户可见错误
      （ScannedPdfError/AIStructureError/ResumeError）原样上抛，**不落库**；
    - 成功路径：旧 current 清位 + 新行入库（parser.save_resume_snapshot，
      ``uq_resume_current`` 部分唯一索引库层兜底 [R6-3]）；
    - ``regenerate_hint``：换简历联动提示（见 :class:`UploadResult`）。
    """
    path = Path(pdf_path)
    if path.suffix.lower() != ".pdf":
        suffix = path.suffix or "（无后缀）"
        raise parser.ResumeError(
            f"仅支持 PDF 格式简历（.pdf）：收到 {suffix}。请导出文字版 PDF 后重新上传。"
        )
    # 提示判定在读旧状态之后、写库之前（保存前后方向/招呼语行均不变）
    replaced = _current_version_row(db) is not None
    regenerate_hint = replaced and _resume_dependents_exist(db)

    text = parser.parse_resume(path)  # ② ScannedPdfError / ResumeError
    snapshot = parser.structure_resume(text)  # ③ AIStructureError
    version_id = parser.save_resume_snapshot(db, text, snapshot)  # ④ R6-3 清位+入库
    return UploadResult(
        version=_fetch_version(db, version_id),
        replaced_previous=replaced,
        regenerate_hint=regenerate_hint,
    )


def apply_manual_edit(db: Database, structured: ResumeSnapshot) -> ResumeVersion:
    """手动修正 → 新版本 [FR2.5]；修正结果即后续 AI 上下文的事实来源。

    - 承袭当前版本的 ``source_pdf_hash``（parser.save_manual_correction）；
      无任何历史版本时 hash 为 NULL（直接手写简历）；
    - 输入经 :func:`parser.strip_contact_entries` 整条剔除联系方式 [B12]
      （入库内容会进入 AI prompt，联系方式不得经手动修正回流），
      剔除后的结果即保存结果。
    """
    cleaned = ResumeSnapshot(
        education=parser.strip_contact_entries(structured.education),
        experience=parser.strip_contact_entries(structured.experience),
        skills=parser.strip_contact_entries(structured.skills),
        projects=parser.strip_contact_entries(structured.projects),
    )
    version_id = parser.save_manual_correction(db, cleaned)
    return _fetch_version(db, version_id)


# ---- 职位方向：AI 候选生成 [FR2.2] ----


class _DirectionItem(BaseModel):
    """AI 输出的单组方向：关键词（≥1，去空白）+ 依据说明（非空）。"""

    keywords: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]] = Field(
        min_length=1, max_length=MAX_KEYWORDS_PER_DIRECTION
    )
    rationale: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DirectionCandidates(BaseModel):
    """AI 方向候选 schema：1~MAX_DIRECTION_GROUPS 组（数量约束由 call_json 校验重试兜底）。"""

    directions: list[_DirectionItem] = Field(min_length=1, max_length=MAX_DIRECTION_GROUPS)


_DIRECTION_RULES = """必须遵守以下规则：
1. 只能依据数据块中的简历事实归纳，不得编造经历、技能或资质；
2. 每组方向 = 一组可直接在招聘平台搜索框使用的关键词（2~4 个词）+
一句依据说明（指向简历中最相关的经历/技能，不超过 60 字）；
3. 关键词只写岗位/技术/领域词：不含城市（城市在任务配置中单独设置）、
不含薪资、不含任何联系方式；
4. 生成 3~5 组不同侧重的方向，输出 JSON（字段 directions，每项含
keywords 与 rationale），不输出解释或多余文字。"""

_DIRECTION_TASK = (
    "请基于数据块中的简历事实生成 3~5 组建议职位方向，输出 JSON：\n"
    '{"directions": [{"keywords": ["关键词1", "关键词2"], "rationale": "依据说明"}, …]}'
)


def build_direction_messages(
    resume: ResumeSnapshot,
    nonce: str | None = None,
) -> list[ChatMessage]:
    """组装方向生成 prompt（§9.3 同构：system = 规则 + 简历事实数据块；user = 任务）。

    - 简历事实经 ``render_resume`` 白名单渲染（四字段之外不进 prompt [B12]），
      以 nonce 数据块包裹后进入 system 消息；
    - ``nonce`` 缺省每次随机生成；测试可显式传入固定 nonce 获得可断言的
      标记字面值（ai/prompt 契约的测试入口）。
    """
    n = nonce if nonce is not None else prompt_mod.new_data_nonce()
    facts_block = prompt_mod.wrap_untrusted(
        "结构化简历事实（已剔除联系方式）",
        prompt_mod.render_resume(resume) or "（简历事实为空）",
        n,
    )
    system = "\n".join(
        [
            "你是求职方向顾问，负责为求职者提炼建议职位方向。",
            _DIRECTION_RULES,
            prompt_mod.format_data_block_instruction(n),
            facts_block,
        ]
    )
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=_DIRECTION_TASK),
    ]


def _direction_to_keywords_json(direction: JobDirection) -> str:
    return json.dumps(direction.keywords, ensure_ascii=False)


def _row_to_direction(row: sqlite3.Row) -> JobDirection:
    """job_directions 行 → JobDirection（keywords_json 反序列化）。"""
    keywords_raw = json.loads(row["keywords_json"])
    rationale_raw = row["rationale"]
    return JobDirection(
        keywords=[str(k) for k in keywords_raw],
        rationale="" if rationale_raw is None else str(rationale_raw),
        is_confirmed=bool(row["is_confirmed"]),
    )


def directions_generate(db: Database) -> list[JobDirection]:
    """AI 生成方向候选（FR2.2：多组搜索关键词 + 每组依据说明）。

    - 前置：已上传简历（无 current 版本 → :class:`parser.ResumeError`，
      文案指引先上传）；
    - AI 调用失败（解析/预算/超时，含 S3a 缺席）统一包装
      :class:`AIDirectionError`（不重试，出路=手动编辑 [FR2.3]），
      **不改动库内任何行**（旧候选/已确认方向原样保留）；
    - 成功：**替换全部未确认候选行**（旧候选清位、新候选入席，均为
      ``is_confirmed=0``）；**已确认方向不动**——重新生成不是确认的
      前置，旧确认集在用户显式确认/编辑前持续生效（提示不强制失效）。
    """
    snapshot = get_current_snapshot(db)
    if snapshot is None:
        raise parser.ResumeError("尚未上传简历：请先上传并解析简历 PDF，再生成职位方向。")

    messages = build_direction_messages(snapshot)
    try:
        call_json = _client_ref.resolve_call_json()
        result = call_json(messages, DirectionCandidates)
    except Exception as exc:  # AI 层任意失败（解析/预算/超时/S3a 缺席）均转用户可见错误
        raise AIDirectionError(
            f"AI 生成职位方向失败：{type(exc).__name__}。请稍后重试，"
            "或直接手动编辑方向（生成不是唯一入口）。"
        ) from exc

    candidates = [
        JobDirection(keywords=list(item.keywords), rationale=item.rationale, is_confirmed=False)
        for item in result.directions
    ]
    with db.transaction() as conn:
        conn.execute("DELETE FROM job_directions WHERE is_confirmed=0")
        for direction in candidates:
            conn.execute(
                "INSERT INTO job_directions (keywords_json, rationale, is_confirmed)"
                " VALUES (?, ?, 0)",
                (_direction_to_keywords_json(direction), direction.rationale),
            )
    return candidates


# ---- 职位方向：确认/编辑（FR2.2 确认/编辑/增删 + FR2.3 手动入口）----


def _replace_confirmed(db: Database, directions: Sequence[JobDirection]) -> list[JobDirection]:
    """整体替换已确认集（增/删/改一次到位），返回规范化后的已确认列表。

    - 每组关键词：去空白、按原序去重（同词重复无搜索意义）；空组直接
      :class:`ValueError`（该组无法作为搜索条件），库内已确认集不变
      （异常发生在写库前）；
    - 事务内 ``DELETE`` 全表（旧确认集 + 未确认候选一并清位）后插入
      终选列表（``is_confirmed=1``）——确认后候选行已被终选覆盖，无保留价值；
    - 允许空列表：语义为"清空全部方向"（前置条件③随之亮红，由 S5b 呈现）。
    """
    cleaned: list[JobDirection] = []
    for direction in directions:
        keywords = [k.strip() for k in direction.keywords if k and k.strip()]
        if not keywords:
            raise ValueError("每组职位方向至少需要一个搜索关键词（空组无法搜索）")
        deduped = list(dict.fromkeys(keywords))  # 保序去重
        cleaned.append(
            JobDirection(
                keywords=deduped,
                rationale=(direction.rationale or "").strip(),
                is_confirmed=True,
            )
        )
    with db.transaction() as conn:
        conn.execute("DELETE FROM job_directions")
        for direction in cleaned:
            conn.execute(
                "INSERT INTO job_directions (keywords_json, rationale, is_confirmed)"
                " VALUES (?, ?, 1)",
                (_direction_to_keywords_json(direction), direction.rationale),
            )
    return cleaned


def directions_confirm(db: Database, directions: Sequence[JobDirection]) -> list[JobDirection]:
    """确认流收口 [FR2.2]：用户对候选"确认/编辑/增删"后传回终选列表。

    调用后 ``job_directions`` 恰好等于终选列表（全部 ``is_confirmed=1``，
    未确认候选行清位）。返回值为规范化（去空白/去重）后的已确认列表。
    """
    return _replace_confirmed(db, directions)


def directions_edit(db: Database, directions: Sequence[JobDirection]) -> list[JobDirection]:
    """手动编辑入口 [FR2.3]（生成不是唯一入口）：与 :func:`directions_confirm`
    同一替换语义，独立命名便于 S5b 区分"确认 AI 候选"与"直接编辑"两路接线。
    """
    return _replace_confirmed(db, directions)


def get_directions(db: Database) -> list[JobDirection]:
    """读取全部方向行（候选 + 已确认，插入序；``is_confirmed`` 区分）。"""
    rows = db.query("SELECT keywords_json, rationale, is_confirmed FROM job_directions ORDER BY id")
    return [_row_to_direction(row) for row in rows]


def confirmed_directions(db: Database) -> list[JobDirection]:
    """读取已确认方向集（``generate_greeting_candidates`` 的求职目标输入、
    启动前置条件③"≥1 组已确认方向"的判定依据）。
    """
    rows = db.query(
        "SELECT keywords_json, rationale, is_confirmed FROM job_directions"
        " WHERE is_confirmed=1 ORDER BY id"
    )
    return [_row_to_direction(row) for row in rows]
