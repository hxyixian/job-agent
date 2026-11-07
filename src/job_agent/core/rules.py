"""投递前置判定规则（设计文档 §5.5 I3；Stage2 S2a，纯函数、无 IO）。

供 S4c 投递流水线在列表/详情过滤步组合调用（设计文档 §7 步 2/步 4）：

- :func:`exclusion_match`：公司黑名单 + 排除组关键词命中（FR3.2 启发式，
  存在误杀可能，配置页已注明）；
- :func:`is_anonymous`：匿名公司卡识别（"某知名企业"类模式 [v13]）；
- :func:`dedup_check`：同公司去重判定（FR3.6 两模式：永久拉黑 / 冷却 N 天）。

契约说明：

- ``salary_overlap`` 已按 [R13] 废止（DOM 薪资字体加密，维度取消），不得再引入；
- 本模块不读数据库、不做网络、不取时钟——companies 行与当前时刻由调用方
  查得后作为参数传入（判定函数 100% 纯函数，供 S4c 组合与离线测试）；
- ``dedup_check`` 返回 :class:`DedupDecision`（§5.5 I3：``dataclass(allowed,
  reason)``，``reason`` 为稳定 snake_case 码，供日志与"为何停"面板）；
  实现了 ``__bool__``（== ``allowed``），任务书 ``-> bool`` 的布尔用法
  （``if dedup_check(...)``）同样成立。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from job_agent.config import ExcludeGroup
from job_agent.models import JobDetail, JobRef

__all__ = [
    "CompaniesRow",
    "DedupDecision",
    "DedupMode",
    "dedup_check",
    "exclusion_match",
    "is_anonymous",
]

# 去重模式取值与 config.DedupConfig.mode 一致
# （companies.mode 列存的是 permanent / cooldown，见 migrations/0001_init.sql）
DedupMode = Literal["permanent_blacklist", "cooldown"]


class CompaniesRow(Protocol):
    """companies 表行的最小结构契约（sqlite3.Row 与 dict[str, Any] 均满足）。"""

    def __getitem__(self, key: str) -> Any: ...


# 匿名公司卡的公司名占位前缀：Boss直聘匿名卡公司名以"某"开头
# （"某知名企业""某互联网金融公司"等模式 [v13]）
_ANONYMOUS_NAME_PREFIX = "某"


@dataclass
class DedupDecision:
    """去重判定结果（§5.5 I3）。

    ``reason`` 取值：``empty_key`` / ``no_record`` / ``permanent`` /
    ``cooldown_active`` / ``cooldown_expired`` / ``cooldown_until_invalid``。
    """

    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        """兼容任务书 ``dedup_check(...) -> bool`` 用法：真值即放行。"""
        return self.allowed


def is_anonymous(job: JobRef | JobDetail) -> bool:
    """匿名公司卡判定 [v13]。

    匿名卡无真实公司名可判：投递流水线按 ``skipped_by=anonymous_company``
    跳过（§7 步 2），:func:`exclusion_match` 对其直接放行（不参与黑名单/
    关键词判定，避免对占位文案误判）。空公司名不视为匿名（无"某"前缀）。
    """
    return job.company_name.strip().startswith(_ANONYMOUS_NAME_PREFIX)


def _norm(text: str) -> str:
    """判定用归一化：去首尾空白 + casefold（关键词命中不区分大小写）。"""
    return text.strip().casefold()


def _scope_texts(job: JobRef | JobDetail, scope: str) -> list[str]:
    """scope 名 → 待匹配文本集。

    - ``jd_text`` 仅 JobDetail 有值；JobRef 无该字段 → 空集（该 scope 不参与）；
    - 未知 scope（V2-7 可扩展，配置层为自由字符串）→ 空集忽略，不报错。
    """
    if scope == "company_name":
        return [job.company_name]
    if scope == "jd_text":
        jd_text: str | None = getattr(job, "jd_text", None)
        return [jd_text] if jd_text else []
    if scope == "tags":
        return list(job.tags)
    return []


def exclusion_match(
    job: JobRef | JobDetail,
    groups: list[ExcludeGroup],
    company_blacklist: list[str],
) -> bool:
    """公司黑名单 ∧ 排除组关键词是否命中（True = 该岗位应跳过，FR3.2）。

    - 公司黑名单：条目（归一化后非空）**包含于**公司名即命中——启发式包含
      匹配而非精确相等（公司全名常带"有限公司"等后缀变体，精确相等会让
      黑名单几乎不可用；误杀可能由 FR3.2 在配置页注明）；
    - 排除组：仅 ``kind="preset"`` 且 ``enabled=True`` 参与 [G9]（custom 组
      V2-7 才开放）；关键词在组内任一 ``scope``（company_name / jd_text /
      tags）文本中包含即命中；空白关键词忽略（空串包含于任何文本，会造成
      全量误杀）；
    - 匿名公司卡直接放行（返回 False）：由流水线按 anonymous_company 跳过
      （§7 步 2 [v13]），黑名单同样不对其执行。
    """
    if is_anonymous(job):
        return False

    name = _norm(job.company_name)
    for entry in company_blacklist:
        normalized = _norm(entry)
        if normalized and normalized in name:
            return True

    for group in groups:
        if group.kind != "preset" or not group.enabled:
            continue
        keywords = [kw for kw in map(_norm, group.keywords) if kw]
        if not keywords:
            continue
        texts = [_norm(text) for scope in group.scope for text in _scope_texts(job, scope)]
        if any(kw in text for kw in keywords for text in texts):
            return True
    return False


def _row_value(row: CompaniesRow, key: str) -> Any:
    """按键取行值；键缺失返回 None（调用方 SELECT 未带全列时不崩溃）。"""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _parse_utc(text: Any) -> datetime | None:
    """解析时间戳文本（ISO-8601，0001 迁移约定为 UTC）；缺失/非法返回 None。

    naive 值按 UTC 解释（db.py / timeutil.py 的存储契约均为 UTC）。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def dedup_check(
    company_key: str,
    companies_row: CompaniesRow | None,
    mode: DedupMode,
    now: datetime,
) -> DedupDecision:
    """同公司去重判定（FR3.6，"以数据库记录为准"；纯函数，行由调用方查得）。

    - ``company_key`` 为空 / ``companies_row`` 为 None → 放行
      （无从去重 / 新公司，去重键生成是适配器侧 [B9] 的职责）；
    - 拉黑语义以**行内 mode**（登记时语义，FR3.6"以数据库记录为准"）优先；
      行 mode 缺失或非法时回落到传入的当前配置 ``mode``
      （``permanent_blacklist``→permanent、``cooldown``→cooldown）；
    - permanent：有记录即拦截；
    - cooldown：``blocked_until`` 晚于 ``now`` 即拦截；已到期放行
      （边界：``now == blocked_until`` 视为到期）；``blocked_until`` 缺失或
      无法解析 → 保守拦截（宁可少投，不重复投）；
    - ``now`` 与 ``blocked_until`` 均归一到 UTC 再比较（naive ``now`` 视为
      UTC，timeutil 契约）；冷却天数本身在登记时折算进 ``blocked_until``，
      本函数不读 ``cooldown_days`` 配置。
    """
    if not company_key.strip():
        return DedupDecision(True, "empty_key")
    if companies_row is None:
        return DedupDecision(True, "no_record")

    row_mode = _row_value(companies_row, "mode")
    if row_mode in ("permanent", "cooldown"):
        semantics: str = row_mode
    else:
        semantics = "cooldown" if mode == "cooldown" else "permanent"
    if semantics == "permanent":
        return DedupDecision(False, "permanent")

    blocked_until = _parse_utc(_row_value(companies_row, "blocked_until"))
    if blocked_until is None:
        return DedupDecision(False, "cooldown_until_invalid")
    now_utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    if now_utc < blocked_until:
        return DedupDecision(False, "cooldown_active")
    return DedupDecision(True, "cooldown_expired")
