"""S2a 判定规则单元测试（设计文档 §5.5 I3；任务书 S2a 第 3 条）。

覆盖：排除组命中与 scope、匿名卡识别、去重两模式与过期（含边界与保守路径）。
全部纯参数调用——rules.py 不做任何 IO，数据库行为以 dict / sqlite3.Row 夹具注入。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from job_agent.config import Config, ExcludeGroup
from job_agent.core.rules import dedup_check, exclusion_match, is_anonymous
from job_agent.models import JobDetail, JobRef

NOW = datetime(2026, 11, 7, 9, 0, 0, tzinfo=UTC)


# ---- 夹具构造 ----


def make_ref(**overrides: Any) -> JobRef:
    """默认"干净"岗位卡；测试用关键字段经 overrides 覆盖。"""
    base: dict[str, Any] = {
        "job_id": "J1",
        "title": "Python 开发工程师",
        "company_name": "示例网络科技",
        "company_key": "c-example",
        "salary_text": "15-25K",  # 展示位原文（可能是加密字形，不参与规则 [R13]）
        "tags": ["五险一金", "带薪年假"],
        "list_snippet": "负责后端服务开发",
    }
    base.update(overrides)
    return JobRef(**base)


def make_detail(**overrides: Any) -> JobDetail:
    ref_fields = {
        "job_id": "J1",
        "title": "Python 开发工程师",
        "company_name": "示例网络科技",
        "company_key": "c-example",
        "salary_text": "15-25K",
        "tags": ["五险一金"],
        "list_snippet": "列表摘要",
    }
    base: dict[str, Any] = {
        **ref_fields,
        "jd_text": "岗位职责：负责后端服务的设计与开发。",
        "experience": "3-5年",
        "company_scale": "500-999人",
        "detail_url": "https://www.zhipin.com/job_detail/J1.html",
    }
    base.update(overrides)
    return JobDetail(**base)


def make_group(
    gid: str = "g1",
    *,
    kind: str = "preset",
    enabled: bool = True,
    keywords: list[str] | None = None,
    scope: list[str] | None = None,
) -> ExcludeGroup:
    """排除组夹具（结构 = config.ExcludeGroup，v1 判定仅认 preset+enabled）。"""
    return ExcludeGroup(
        id=gid,
        kind=kind,  # type: ignore[arg-type]
        enabled=enabled,
        keywords=keywords or [],
        scope=scope or ["company_name"],
    )


# ---- exclusion_match：黑名单 ----


def test_blacklist_substring_hit() -> None:
    # 包含匹配：黑名单写简称，公司名带后缀变体也能命中
    job = make_ref(company_name="中软国际科技服务有限公司")
    assert exclusion_match(job, [], ["中软国际"]) is True


def test_blacklist_case_insensitive() -> None:
    job = make_ref(company_name="Acme 网络科技")
    assert exclusion_match(job, [], ["acme"]) is True


def test_blacklist_no_hit() -> None:
    assert exclusion_match(make_ref(), [], ["字节跳动"]) is False


def test_blacklist_blank_entries_ignored() -> None:
    # 空串/纯空白条目包含于任何文本，必须忽略（否则全量误杀）
    assert exclusion_match(make_ref(), [], ["", "   "]) is False


# ---- exclusion_match：排除组命中与 scope ----


def test_group_keyword_company_name_scope_hit() -> None:
    group = make_group(keywords=["外包"], scope=["company_name"])
    assert exclusion_match(make_ref(company_name="星辰软件外包公司"), [group], []) is True


def test_group_scope_respected_keyword_outside_scope() -> None:
    # 关键词只出现在 jd_text/tags，但组 scope 仅 company_name → 不命中
    group = make_group(keywords=["实习"], scope=["company_name"])
    job = make_detail(jd_text="欢迎实习生投递", tags=["实习"])
    assert exclusion_match(job, [group], []) is False


def test_group_jd_text_scope_on_detail() -> None:
    group = make_group(keywords=["驻场开发"], scope=["jd_text"])
    job = make_detail(jd_text="本岗位需长期驻场开发")
    assert exclusion_match(job, [group], []) is True


def test_group_jd_text_scope_on_ref_no_field() -> None:
    # JobRef 无 jd_text 字段：该 scope 视为无文本，不崩溃、不命中
    group = make_group(keywords=["驻场开发"], scope=["jd_text"])
    assert exclusion_match(make_ref(), [group], []) is False


def test_group_tags_scope_hit() -> None:
    group = make_group(keywords=["猎头"], scope=["tags"])
    assert exclusion_match(make_ref(tags=["猎头职位"]), [group], []) is True


def test_group_tags_scope_ignores_company_name_text() -> None:
    group = make_group(keywords=["外包"], scope=["tags"])
    assert exclusion_match(make_ref(company_name="外包公司"), [group], []) is False


def test_group_multi_scope_any_hit() -> None:
    group = make_group(keywords=["兼职"], scope=["jd_text", "tags"])
    assert exclusion_match(make_detail(tags=["周末兼职"]), [group], []) is True


def test_group_keyword_case_insensitive() -> None:
    group = make_group(keywords=["outsourcing"], scope=["company_name"])
    assert exclusion_match(make_ref(company_name="XX Outsourcing Ltd"), [group], []) is True


def test_group_blank_keyword_ignored() -> None:
    assert exclusion_match(make_ref(), [make_group(keywords=["", "  "])], []) is False


def test_group_disabled_not_applied() -> None:
    group = make_group(keywords=["外包"], enabled=False)
    assert exclusion_match(make_ref(company_name="外包公司"), [group], []) is False


def test_group_custom_kind_not_applied_in_v1() -> None:
    # v1 仅 kind=preset 生效 [G9]；custom 组即便 enabled 也忽略
    group = make_group(kind="custom", keywords=["外包"])
    assert exclusion_match(make_ref(company_name="外包公司"), [group], []) is False


def test_group_unknown_scope_ignored() -> None:
    # scope 为自由字符串（V2-7 可扩展）：未知值忽略，不报错
    group = make_group(keywords=["外包"], scope=["company_scale"])
    assert exclusion_match(make_detail(company_scale="0-20人"), [group], []) is False


def test_clean_job_passes_all_rules() -> None:
    groups = [make_group(keywords=["外包", "猎头", "实习"], scope=["company_name", "tags"])]
    assert exclusion_match(make_detail(), groups, ["某竞争公司"]) is False


def test_default_config_groups_all_disabled() -> None:
    # 出厂默认（FR3.2 默认全关）+ 空黑名单：外包特征岗位也放行
    config = Config()
    job = make_ref(company_name="XX 人力资源服务有限公司", tags=["软件外包"])
    assert exclusion_match(job, config.match.exclude_groups, []) is False


def test_default_config_group_enabled_hits() -> None:
    # 打开预置外包组后命中（复用 config.py 预置组结构，不重复造规则）
    config = Config()
    groups = [
        g.model_copy(update={"enabled": True})
        for g in config.match.exclude_groups
        if g.id == "outsourcing"
    ]
    job = make_ref(company_name="XX 软件外包公司")
    assert exclusion_match(job, groups, []) is True


# ---- 匿名公司卡 [v13] ----


def test_is_anonymous_known_patterns() -> None:
    for name in ("某知名企业", "某互联网金融公司", "某网络科技有限公司"):
        assert is_anonymous(make_ref(company_name=name)) is True


def test_is_anonymous_normal_company() -> None:
    assert is_anonymous(make_ref(company_name="字节跳动")) is False


def test_is_anonymous_blank_name() -> None:
    assert is_anonymous(make_ref(company_name="")) is False


def test_is_anonymous_strips_whitespace() -> None:
    assert is_anonymous(make_ref(company_name="  某知名企业  ")) is True


def test_anonymous_job_skips_exclusion_entirely() -> None:
    # 匿名卡不参与黑名单/关键词判定（由流水线按 anonymous_company 跳过，§7 步 2）
    groups = [make_group(keywords=["知名"], scope=["company_name"])]
    job = make_ref(company_name="某知名企业", tags=["外包"])
    assert exclusion_match(job, groups, ["某知名企业"]) is False


# ---- dedup_check：两模式与过期 ----


def row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "company_key": "c-example",
        "name": "示例网络科技",
        "mode": "permanent",
        "blocked_until": None,
        "source": "delivery",
    }
    base.update(overrides)
    return base


def test_dedup_no_record_allowed() -> None:
    decision = dedup_check("c-new", None, "permanent_blacklist", NOW)
    assert decision.allowed is True
    assert decision.reason == "no_record"
    assert bool(decision) is True  # 任务书 bool 用法


def test_dedup_permanent_blocked() -> None:
    decision = dedup_check("c-1", row(mode="permanent"), "permanent_blacklist", NOW)
    assert decision.allowed is False
    assert decision.reason == "permanent"
    assert not decision  # __bool__ == allowed


def test_dedup_cooldown_active() -> None:
    blocked_until = (NOW + timedelta(days=5)).isoformat()
    decision = dedup_check(
        "c-1", row(mode="cooldown", blocked_until=blocked_until), "cooldown", NOW
    )
    assert decision.allowed is False
    assert decision.reason == "cooldown_active"


def test_dedup_cooldown_expired() -> None:
    blocked_until = (NOW - timedelta(seconds=1)).isoformat()
    decision = dedup_check(
        "c-1", row(mode="cooldown", blocked_until=blocked_until), "cooldown", NOW
    )
    assert decision.allowed is True
    assert decision.reason == "cooldown_expired"


def test_dedup_cooldown_boundary_now_equals_blocked_until() -> None:
    # 边界：now == blocked_until 视为已到期（闭区间起点拦截、终点放行）
    blocked_until = NOW.isoformat()
    decision = dedup_check(
        "c-1", row(mode="cooldown", blocked_until=blocked_until), "cooldown", NOW
    )
    assert decision.allowed is True
    assert decision.reason == "cooldown_expired"


def test_dedup_cooldown_until_missing_blocks_conservatively() -> None:
    # blocked_until 缺失（cooldown 行异常）→ 保守拦截（宁可少投不重投）
    decision = dedup_check("c-1", row(mode="cooldown", blocked_until=None), "cooldown", NOW)
    assert decision.allowed is False
    assert decision.reason == "cooldown_until_invalid"


def test_dedup_cooldown_until_unparseable_blocks_conservatively() -> None:
    decision = dedup_check(
        "c-1", row(mode="cooldown", blocked_until="not-a-timestamp"), "cooldown", NOW
    )
    assert decision.allowed is False
    assert decision.reason == "cooldown_until_invalid"


def test_dedup_row_mode_preferred_over_config() -> None:
    # FR3.6"以数据库记录为准"：行登记为 permanent，配置已切 cooldown 仍拦截
    decision = dedup_check("c-1", row(mode="permanent"), "cooldown", NOW)
    assert decision.allowed is False
    assert decision.reason == "permanent"


def test_dedup_row_mode_invalid_falls_back_to_config_permanent() -> None:
    decision = dedup_check("c-1", row(mode="weird"), "permanent_blacklist", NOW)
    assert decision.allowed is False
    assert decision.reason == "permanent"


def test_dedup_row_mode_invalid_falls_back_to_config_cooldown() -> None:
    blocked_until = (NOW - timedelta(days=1)).isoformat()
    decision = dedup_check("c-1", row(mode="", blocked_until=blocked_until), "cooldown", NOW)
    assert decision.allowed is True
    assert decision.reason == "cooldown_expired"


def test_dedup_empty_company_key_allowed() -> None:
    # 去重键缺失无从去重（键生成是适配器侧 [B9] 职责）→ 放行
    decision = dedup_check("  ", row(mode="permanent"), "permanent_blacklist", NOW)
    assert decision.allowed is True
    assert decision.reason == "empty_key"


def test_dedup_naive_now_treated_as_utc() -> None:
    # timeutil 契约：naive 视为 UTC（北京 17:00 = UTC 09:00）
    naive_now = datetime(2026, 11, 7, 9, 0, 0)
    past = (naive_now - timedelta(seconds=1)).isoformat()
    future = (naive_now + timedelta(seconds=1)).isoformat()
    assert dedup_check(
        "c-1", row(mode="cooldown", blocked_until=past), "cooldown", naive_now
    ).allowed
    assert not dedup_check("c-1", row(mode="cooldown", blocked_until=future), "cooldown", naive_now)


def test_dedup_z_suffix_timestamp_parsed() -> None:
    # 0001 迁移/utcnow() 的毫秒 + Z 格式同样可解析
    decision = dedup_check(
        "c-1", row(mode="cooldown", blocked_until="2026-12-07T09:00:00.123Z"), "cooldown", NOW
    )
    assert decision.allowed is False
    assert decision.reason == "cooldown_active"


def test_dedup_accepts_sqlite_row() -> None:
    # 与 S4c 实际取数路径兼容：sqlite3.Row（协议结构匹配，非 Mapping 子类）
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            CREATE TABLE companies (
                company_key   TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                mode          TEXT NOT NULL,
                blocked_until TEXT,
                source        TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO companies VALUES ('c-1', '示例网络科技', 'cooldown', ?, 'delivery')",
            ((NOW + timedelta(days=3)).isoformat(),),
        )
        sqlite_row = conn.execute("SELECT * FROM companies WHERE company_key = 'c-1'").fetchone()
        assert sqlite_row is not None
        decision = dedup_check("c-1", sqlite_row, "cooldown", NOW)
        assert decision.allowed is False
        assert decision.reason == "cooldown_active"
    finally:
        conn.close()


def test_dedup_row_missing_columns_does_not_crash() -> None:
    # SELECT 未带全列（缺 mode/blocked_until）也能安全判定：回落配置语义 + 保守拦截
    decision = dedup_check("c-1", {"company_key": "c-1"}, "cooldown", NOW)
    assert decision.allowed is False
    assert decision.reason == "cooldown_until_invalid"
