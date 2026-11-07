"""简历解析：pypdf 提取 → AI 结构化 → 版本化入库（设计文档 §5.5 I10 / §9.5，Stage3 S3c）。

对外契约（I10）::

    parse_resume(pdf_path) -> ResumeText
        # pypdf 提取全文；文本 < 200 字符 → raise ScannedPdfError（明确文案 [B15]）
    structure_resume(text) -> ResumeSnapshot
        # AI 结构化 {education, experience, skills, projects}，剔除联系方式字段 [B12]；
        # AI 失败 → raise AIStructureError（不重试，走手动修正 [FR2.5]）

``ScannedPdfError`` / ``AIStructureError`` 均为**用户可见错误**：message 即
控制台展示文案，调用方不再包装、不自动重试。

联系方式剔除 [B12]（双侧防御，均为确定性规则、可离线测试）：
- **输入侧** :func:`redact_contact_lines`——发给 AI 的简历副本先整行剔除
  命中联系方式模式的行（§9.5"给 AI 的简历副本去除联系方式字段"）；
- **输出侧** :func:`strip_contact_entries`——AI 返回的结构化条目再整条剔除
  命中联系方式模式的条目。策略保守：宁可多删、不漏任何联系方式。

版本化 [FR2.5/B14]（resume_snapshots，R6-3 单 current）：
- :func:`save_resume_snapshot`：解析+结构化成功后入库（清旧 current →
  插新 current，``uq_resume_current`` 部分唯一索引库层兜底）；
- :func:`save_manual_correction`：控制台手动修正生成**新版本**（承袭当前
  版本的 ``source_pdf_hash`` 溯源）；
- :func:`current_resume_snapshot`：读取当前版本（AI 上下文的事实来源 [FR2.4]）。

AI 调用：经 ``ai/_client_ref`` 延迟定位 S3a 的 ``call_json``（JSON 提取
→Pydantic 校验→重试≤2 统一由其负责 [B13]，共享进程级默认客户端即共享
日预算计数；本层对最终失败包装为 ``AIStructureError``，不再重试）。
数据块用 ``ai/prompt`` 的 nonce 包裹件（随机标记，简历全文无法伪造提前
闭合）。测试用 monkeypatch 替换 ``resolve_call_json`` 注入桩（TEST1 全离线）。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path

from pydantic import BaseModel
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from job_agent.ai import _client_ref
from job_agent.ai import prompt as prompt_mod
from job_agent.ai.types import ChatMessage
from job_agent.db import Database
from job_agent.models import ResumeSnapshot, ResumeText

__all__ = [
    "MIN_TEXT_CHARS",
    "AIStructureError",
    "ResumeError",
    "ScannedPdfError",
    "StructuredResume",
    "build_structure_messages",
    "current_resume_snapshot",
    "parse_resume",
    "redact_contact_lines",
    "save_manual_correction",
    "save_resume_snapshot",
    "snapshot_from_json",
    "snapshot_to_json",
    "strip_contact_entries",
    "structure_resume",
]

# §9.5 [B15]：提取文本少于该字符数判定为扫描件/图片型 PDF
MIN_TEXT_CHARS = 200

# 扫描件报错文案（含 §9.5 权威短语"图片型 PDF 不支持，请提供文字版"）
_SCANNED_MESSAGE = (
    "简历解析失败：该 PDF 疑似扫描件（仅提取到 {count} 个字符，需 ≥{limit}）。"
    "图片型 PDF 不支持，请提供文字版简历 PDF。"
)

# ---- 联系方式匹配模式（B12；保守剔除：宁可多删，不漏联系方式）----
# 手机号（含 3-4-4 分隔写法：138-1234-5678 / 138 1234 5678 / 13812345678）
_MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d[-\s]?\d{4}[-\s]?\d{4}(?!\d)")
# 座机（区号-号码：010-12345678 / 0755-1234567）
_LANDLINE_RE = re.compile(r"(?<!\d)\d{3,4}-\d{7,8}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 微信号：中文标签或英文称呼后跟 6-20 位字母开头账号（不含 wx/vx 简写，避免误杀如 wxpython）
_WECHAT_RE = re.compile(
    r"(?:微信|wechat|weixin)\s*(?:号)?\s*[:：]?\s*[a-zA-Z][-_a-zA-Z0-9]{5,19}",
    re.IGNORECASE,
)
# QQ 号：标签后跟 5-11 位数字（首位非 0）
_QQ_RE = re.compile(r"(?:qq|扣扣)\s*(?:号)?\s*[:：]?\s*[1-9]\d{4,10}(?!\d)", re.IGNORECASE)
# 显式联系方式字段标签（带冒号）：手机：/电话：/邮箱：/微信：/QQ号：/联系方式：/E-mail:
_CONTACT_LABEL_RE = re.compile(
    r"(?:手机|电话|联系方式|电子?邮箱|e-?mail|微信|wechat|weixin|vx|wx|qq|扣扣)"
    r"\s*(?:号)?\s*[:：]",
    re.IGNORECASE,
)

_CONTACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    _MOBILE_RE,
    _LANDLINE_RE,
    _EMAIL_RE,
    _WECHAT_RE,
    _QQ_RE,
    _CONTACT_LABEL_RE,
)


def _contains_contact(text: str) -> bool:
    """文本是否命中任一联系方式模式（含显式字段标签）。"""
    return any(pattern.search(text) for pattern in _CONTACT_PATTERNS)


def redact_contact_lines(text: str) -> str:
    """输入侧剔除 [B12]：整行删除命中联系方式模式的行（发给 AI 的简历副本）。

    简历中的联系方式几乎总是独立成行（"电话：138…" / "邮箱：a@b.com"），
    整行剔除可同时去掉标签与值；误删的代价（少一行正文）远小于
    联系方式进入 prompt 后被 AI 泄露的风险。
    """
    kept = [line for line in text.splitlines() if not _contains_contact(line)]
    return "\n".join(kept).strip()


def strip_contact_entries(entries: list[str]) -> list[str]:
    """输出侧剔除 [B12]：整条删除 AI 结构化结果中命中联系方式模式的条目。

    同时过滤空白条目。保守策略：条目一旦包含联系方式（如
    "电话：13812345678"），整条丢弃——合法的经历/技能条目极少内嵌
    手机号/邮箱，即便误删也不影响求职事实的主干。
    """
    return [e.strip() for e in entries if e.strip() and not _contains_contact(e)]


class ResumeError(Exception):
    """简历处理失败基类（用户可见：message 即控制台文案，不自动重试）。"""


class ScannedPdfError(ResumeError):
    """图片型/扫描件 PDF [B15]：提取文本 < ``MIN_TEXT_CHARS``。"""


class AIStructureError(ResumeError):
    """AI 结构化失败 [FR2.5]：不重试；用户可在控制台手动修正（生成新版本）。"""


class StructuredResume(BaseModel):
    """AI 结构化输出 schema（§9.5 四字段；联系方式由输出侧规则再剔除）。"""

    education: list[str]
    experience: list[str]
    skills: list[str]
    projects: list[str]


_STRUCTURE_SYSTEM_PROMPT = (
    "你是简历结构化助手。把数据块中的简历全文提取为 JSON，字段固定为四类：\n"
    "education（教育）/ experience（工作经历）/ skills（技能）/ projects（项目），"
    "每个字段的值为字符串数组，每条用一句话概括。\n"
    "必须遵守：\n"
    "1. 只依据简历原文归纳，不得编造或补充原文没有的信息；\n"
    "2. 严禁在任何输出字段中包含联系方式：手机号、座机、邮箱、微信号、QQ 号、"
    '通讯地址、"联系方式"类字段一律不得出现（原文有则直接忽略）。'
)


def build_structure_messages(text: ResumeText, nonce: str | None = None) -> list[ChatMessage]:
    """组装结构化 prompt（指令块 system + 数据块 user）[B12]。

    - ``nonce`` 缺省每次随机生成（ai/prompt 契约）；测试可显式传入固定
      nonce 以获得可断言的标记字面值；
    - 简历全文为**已剔除联系方式行**的副本（§9.5"给 AI 的简历副本去除
      联系方式字段"），以 nonce 数据块包裹，只出现在 user 侧。
    """
    n = nonce if nonce is not None else prompt_mod.new_data_nonce()
    redacted = redact_contact_lines(text.text) or "（简历正文为空）"
    system = "\n".join([_STRUCTURE_SYSTEM_PROMPT, prompt_mod.format_data_block_instruction(n)])
    user = (
        prompt_mod.wrap_untrusted("简历全文（已剔除联系方式行）", redacted, n)
        + "\n请输出上述四类字段的结构化 JSON。"
    )
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=user),
    ]


def parse_resume(pdf_path: str | Path) -> ResumeText:
    """pypdf 提取简历全文（§5.5 I10）。

    - 文件不存在 / 无法解析 / 已加密：抛 :class:`ResumeError`（明确文案）；
    - 提取文本（去首尾空白后）< ``MIN_TEXT_CHARS``（200）字符：抛
      :class:`ScannedPdfError`——文案含"图片型 PDF 不支持，请提供文字版"
      [B15/§9.5]；
    - 成功返回 :class:`ResumeText`，``source_pdf_hash`` = 源文件字节流的
      SHA-256 hex（入 resume_snapshots.source_pdf_hash 溯源）。
    """
    path = Path(pdf_path)
    if not path.is_file():
        raise ResumeError(f"简历文件不存在：{path}")
    data = path.read_bytes()
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ResumeError("简历 PDF 已加密，无法提取文字，请提供未加密的文字版 PDF。")
        text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except PdfReadError as exc:
        raise ResumeError(f"简历 PDF 读取失败（文件可能损坏或不是 PDF）：{exc}") from exc

    if len(text) < MIN_TEXT_CHARS:
        raise ScannedPdfError(_SCANNED_MESSAGE.format(count=len(text), limit=MIN_TEXT_CHARS))
    return ResumeText(text=text, source_pdf_hash=hashlib.sha256(data).hexdigest())


def structure_resume(text: ResumeText) -> ResumeSnapshot:
    """AI 结构化（§5.5 I10 / §9.5）。

    - 输入侧已 :func:`redact_contact_lines`（发给 AI 的副本去联系方式 [B12]）；
    - AI 调用失败（解析/预算/超时等，含 call_json 重试耗尽）统一包装为
      :class:`AIStructureError`（不重试，message 为用户可见文案，原始异常
      保留在 ``__cause__``）；
    - 输出侧 :func:`strip_contact_entries` 再剔除条目级联系方式 [B12]。
    """
    messages = build_structure_messages(text)
    try:
        call_json = _client_ref.resolve_call_json()
        result = call_json(messages, StructuredResume)
    except Exception as exc:  # AI 层任意失败（解析/预算/超时/S3a 缺席）均转用户可见错误
        raise AIStructureError(
            f"AI 简历结构化失败：{type(exc).__name__}。请稍后重试，或直接在控制台手动修正简历。"
        ) from exc
    return ResumeSnapshot(
        education=strip_contact_entries(result.education),
        experience=strip_contact_entries(result.experience),
        skills=strip_contact_entries(result.skills),
        projects=strip_contact_entries(result.projects),
    )


# ---- 版本化入库 [FR2.5/B14]（resume_snapshots，R6-3 单 current）----


def snapshot_to_json(snapshot: ResumeSnapshot) -> str:
    """ResumeSnapshot → structured_json 文本（UTF-8 中文原样，键序稳定）。"""
    return json.dumps(asdict(snapshot), ensure_ascii=False, indent=2, sort_keys=True)


def snapshot_from_json(raw: str) -> ResumeSnapshot:
    """structured_json 文本 → ResumeSnapshot（round-trip 反序列化）。"""
    data = json.loads(raw)
    return ResumeSnapshot(
        education=list(data.get("education", [])),
        experience=list(data.get("experience", [])),
        skills=list(data.get("skills", [])),
        projects=list(data.get("projects", [])),
    )


def _insert_new_current(
    conn: sqlite3.Connection,
    structured_json: str,
    source_pdf_hash: str | None,
) -> int:
    """事务内插入新 current 版本（先清旧 current——R6-3 单 current 约束）。"""
    conn.execute("UPDATE resume_snapshots SET is_current=0 WHERE is_current=1")
    cursor = conn.execute(
        "INSERT INTO resume_snapshots (structured_json, source_pdf_hash, is_current)"
        " VALUES (?, ?, 1)",
        (structured_json, source_pdf_hash),
    )
    return int(cursor.lastrowid or 0)


def save_resume_snapshot(db: Database, text: ResumeText, snapshot: ResumeSnapshot) -> int:
    """解析+结构化成功后入库为新 current 版本，返回新行 id。

    R6-3：``uq_resume_current`` 部分唯一索引保证全表至多一个 current；
    本函数在事务内先清旧 current 再插新行。
    """
    with db.transaction() as conn:
        return _insert_new_current(conn, snapshot_to_json(snapshot), text.source_pdf_hash)


def save_manual_correction(db: Database, snapshot: ResumeSnapshot) -> int:
    """手动修正生成新版本 [FR2.5]，返回新行 id。

    承袭当前版本的 ``source_pdf_hash``（修正基于同一份 PDF 的结构化结果，
    溯源不变）；无任何历史版本时（直接手写简历）hash 为 NULL。
    """
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT source_pdf_hash FROM resume_snapshots WHERE is_current=1"
        ).fetchone()
        inherited: str | None = row["source_pdf_hash"] if row is not None else None
        return _insert_new_current(conn, snapshot_to_json(snapshot), inherited)


def current_resume_snapshot(db: Database) -> ResumeSnapshot | None:
    """读取当前版本（AI 上下文的事实来源 [FR2.4]）；从未解析过返回 None。"""
    row = db.query_one(
        "SELECT structured_json FROM resume_snapshots WHERE is_current=1 ORDER BY id DESC"
    )
    if row is None:
        return None
    return snapshot_from_json(row["structured_json"])
