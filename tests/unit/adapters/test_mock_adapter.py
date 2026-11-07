"""S4a MockSiteAdapter 契约测试（§12 + S4a 任务书：每方法正常/异常路径 + 能力开关 + 注册切换）。

- 默认使用仓库夹具 ``tests/fixtures/sim``（S4a 交付的最小集：职位池 15 /
  会话池 3 / 5 persona 消息脚本骨架 + noreply）；
- 时间注入固定时钟（FIXED_NOW 晚于全部夹具消息时间），动态消息全部可断言。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from job_agent.adapters.base import (
    AdapterBrokenError,
    AdapterError,
    LoginRequiredError,
    PlatformLimitError,
    RiskDetectedError,
    SearchFilters,
    TransientError,
    create_adapter,
)
from job_agent.adapters.mock import (
    MockCrashError,
    MockFixtureError,
    MockScenario,
    MockSiteAdapter,
    SecurityCheckForm,
)
from job_agent.models import (
    JobDetail,
    LoginState,
    MsgType,
    RiskSignal,
    SendOutcome,
)

REPO = Path(__file__).resolve().parents[3]  # tests/unit/adapters/ → 仓库根
# 固定时钟：北京 2026-11-07 14:00（晚于全部夹具消息，动态消息时间可精确断言）
FIXED_NOW = datetime(2026, 11, 7, 6, 0, 0, tzinfo=UTC)


@pytest.fixture()
def frozen() -> MockSiteAdapter:
    return MockSiteAdapter(clock=lambda: FIXED_NOW)


# ---- 夹具最小集交付检查（S4a 验收）----


def test_shipped_fixture_minimal_set() -> None:
    jobs = json.loads((REPO / "tests" / "fixtures" / "sim" / "jobs.json").read_text("utf-8"))
    assert len(jobs["jobs"]) >= 15
    convs = json.loads(
        (REPO / "tests" / "fixtures" / "sim" / "conversations.json").read_text("utf-8")
    )
    assert len(convs["conversations"]) >= 3
    scripts = json.loads((REPO / "tests" / "fixtures" / "sim" / "messages.json").read_text("utf-8"))
    # §12 五 persona（冷淡/热情/压价/约面试/offer）骨架 + noreply（B2）
    assert {"cold", "warm", "price", "interview", "offer", "noreply"} <= set(scripts["scripts"])


def test_default_fixtures_dir_is_repo_sim() -> None:
    adapter = MockSiteAdapter()
    assert adapter.fixtures_dir == REPO / "tests" / "fixtures" / "sim"
    assert adapter.snapshot_conversation_baseline() >= 3


def test_scenario_object_shared_and_flippable_at_runtime() -> None:
    scn = MockScenario()
    adapter = MockSiteAdapter(scenario=scn)
    assert adapter.scenario is scn
    assert adapter.detect_risk() is RiskSignal.NONE
    scn.evicted = True  # 运行中翻转
    assert adapter.detect_risk() is RiskSignal.EVICTED


# ---- check_login_state / detect_risk（探测方法：不抛异常，只返回状态）----


def test_check_login_state_default_and_injection(frozen: MockSiteAdapter) -> None:
    assert frozen.check_login_state() is LoginState.LOGGED_IN
    frozen.scenario.login_state = LoginState.LOGGED_OUT
    assert frozen.check_login_state() is LoginState.LOGGED_OUT
    frozen.scenario.login_state = LoginState.UNKNOWN
    assert frozen.check_login_state() is LoginState.UNKNOWN


@pytest.mark.parametrize("state", [LoginState.LOGGED_OUT, LoginState.UNKNOWN])
def test_login_injection_blocks_operations(frozen: MockSiteAdapter, state: LoginState) -> None:
    frozen.scenario.login_state = state
    with pytest.raises(LoginRequiredError):
        frozen.search_jobs(SearchFilters(), 1)
    # 探测方法不受门控（失效时也要能返回状态）
    assert frozen.check_login_state() is state


def test_detect_risk_default_none(frozen: MockSiteAdapter) -> None:
    assert frozen.detect_risk() is RiskSignal.NONE


@pytest.mark.parametrize("form", list(SecurityCheckForm))
def test_detect_risk_security_check_three_forms(
    frozen: MockSiteAdapter, form: SecurityCheckForm
) -> None:
    frozen.scenario.security_check = form
    assert frozen.detect_risk() is RiskSignal.SECURITY_CHECK


def test_detect_risk_evicted_captcha_risk_page(frozen: MockSiteAdapter) -> None:
    frozen.scenario.evicted = True
    assert frozen.detect_risk() is RiskSignal.EVICTED
    frozen.scenario.evicted = False
    frozen.scenario.captcha = True
    assert frozen.detect_risk() is RiskSignal.CAPTCHA
    frozen.scenario.captcha = False
    frozen.scenario.risk_page = True
    assert frozen.detect_risk() is RiskSignal.RISK_PAGE


def test_detect_risk_platform_limit(frozen: MockSiteAdapter) -> None:
    frozen.scenario.platform_limit = True
    assert frozen.detect_risk() is RiskSignal.PLATFORM_LIMIT


def test_detect_risk_priority(frozen: MockSiteAdapter) -> None:
    frozen.scenario.security_check = SecurityCheckForm.URL_MARKER
    frozen.scenario.evicted = True
    assert frozen.detect_risk() is RiskSignal.SECURITY_CHECK  # SECURITY_CHECK > EVICTED
    frozen.scenario.security_check = None
    frozen.scenario.captcha = True
    assert frozen.detect_risk() is RiskSignal.EVICTED  # EVICTED > CAPTCHA


def _security(form: SecurityCheckForm) -> Any:
    """构造 security_check 开关翻转器（供参数化，避免超宽行）。"""
    return lambda s: setattr(s, "security_check", form)


@pytest.mark.parametrize(
    ("switch", "expected"),
    [
        (lambda s: setattr(s, "evicted", True), RiskSignal.EVICTED),
        (_security(SecurityCheckForm.URL_MARKER), RiskSignal.SECURITY_CHECK),
        (_security(SecurityCheckForm.PAGE_TEXT), RiskSignal.SECURITY_CHECK),
        (_security(SecurityCheckForm.NAV_BOUNCE), RiskSignal.SECURITY_CHECK),
        (lambda s: setattr(s, "captcha", True), RiskSignal.CAPTCHA),
        (lambda s: setattr(s, "risk_page", True), RiskSignal.RISK_PAGE),
    ],
)
def test_risk_injection_circuitbreaks_operations(
    frozen: MockSiteAdapter, switch: Any, expected: RiskSignal
) -> None:
    switch(frozen.scenario)
    assert frozen.detect_risk() is expected
    with pytest.raises(RiskDetectedError) as excinfo:
        frozen.list_conversations()
    assert excinfo.value.signal is expected
    assert frozen.send_count == 0


def test_transient_error_injection(frozen: MockSiteAdapter) -> None:
    frozen.scenario.transient_error = True
    with pytest.raises(TransientError):
        frozen.get_messages("C-1001", None)


# ---- search_jobs / get_job_detail ----


def test_search_jobs_pagination(frozen: MockSiteAdapter) -> None:
    page1 = frozen.search_jobs(SearchFilters(), 1)
    page2 = frozen.search_jobs(SearchFilters(), 2)
    page3 = frozen.search_jobs(SearchFilters(), 3)
    assert len(page1) == 10  # MOCK_PAGE_SIZE
    assert len(page2) == 5  # 职位池 15
    assert page3 == []
    assert len({j.job_id for j in page1 + page2}) == 15


def test_search_jobs_keyword_filter(frozen: MockSiteAdapter) -> None:
    result = frozen.search_jobs(SearchFilters(keywords=["Python"]), 1)
    assert [j.job_id for j in result] == ["J001", "J004", "J005"]  # title/tags 命中
    assert frozen.search_jobs(SearchFilters(keywords=["不存在的关键词xyz"]), 1) == []


def test_search_jobs_keyword_matches_company(frozen: MockSiteAdapter) -> None:
    result = frozen.search_jobs(SearchFilters(keywords=["星桥"]), 1)
    assert [j.job_id for j in result] == ["J001"]


def test_search_jobs_invalid_page(frozen: MockSiteAdapter) -> None:
    with pytest.raises(AdapterError, match="page"):
        frozen.search_jobs(SearchFilters(), 0)


def test_search_jobs_content_missing_health_check(frozen: MockSiteAdapter) -> None:
    frozen.scenario.content_missing = True
    assert frozen.search_jobs(SearchFilters(), 1) == []
    assert frozen.consecutive_element_failures == 1
    assert frozen.search_jobs(SearchFilters(), 1) == []
    assert frozen.consecutive_element_failures == 2
    with pytest.raises(AdapterBrokenError):  # 第 3 次连续失败熔断 [B7/H1]
        frozen.search_jobs(SearchFilters(), 1)
    frozen.scenario.content_missing = False
    frozen.search_jobs(SearchFilters(), 1)  # 成功归零
    assert frozen.consecutive_element_failures == 0


def test_get_job_detail_full_fields(frozen: MockSiteAdapter) -> None:
    detail = frozen.get_job_detail("J001")
    assert isinstance(detail, JobDetail)
    assert detail.title == "Python 后端工程师"
    assert detail.company_key == "C001"
    assert detail.experience == "3-5年"
    assert detail.company_scale == "500-999人"
    assert "J001" in detail.detail_url
    assert len(detail.jd_text) > 0


def test_get_job_detail_unknown_job(frozen: MockSiteAdapter) -> None:
    # 单职位缺卡 = 未分类错误（调用方跳过该职位），不计连续失败 [NFR2]
    with pytest.raises(AdapterError, match="J9999"):
        frozen.get_job_detail("J9999")
    assert frozen.consecutive_element_failures == 0


def test_get_job_detail_content_missing_health_check(frozen: MockSiteAdapter) -> None:
    frozen.scenario.content_missing = True
    assert frozen.get_job_detail("J001").jd_text == ""
    assert frozen.get_job_detail("J001").jd_text == ""
    assert frozen.consecutive_element_failures == 2
    with pytest.raises(AdapterBrokenError):
        frozen.get_job_detail("J001")


# ---- send_greeting / send_reply / send_resume ----


def _job(frozen: MockSiteAdapter, job_id: str):
    return next(j for j in frozen.search_jobs(SearchFilters(), 1) if j.job_id == job_id)


def test_send_greeting_dry_run_zero_side_effect(frozen: MockSiteAdapter) -> None:
    frozen.dry_run = True
    assert frozen.send_greeting(_job(frozen, "J002"), "您好") is SendOutcome.SKIPPED_DRYRUN
    assert frozen.send_count == 0
    assert frozen.snapshot_conversation_baseline() == 3  # 无新会话


def test_send_greeting_sent_creates_conversation(frozen: MockSiteAdapter) -> None:
    job = _job(frozen, "J002")  # J002 无既有会话
    assert frozen.send_greeting(job, "您好，很感兴趣") is SendOutcome.SENT
    assert frozen.send_count == 1
    assert frozen.snapshot_conversation_baseline() == 4
    messages = frozen.get_messages("mock-conv-J002", None)
    assert len(messages) == 1
    sent = messages[0]
    assert (sent.direction, sent.sender, sent.msg_type) == ("out", "me", MsgType.TEXT)
    assert sent.text == "您好，很感兴趣"
    assert sent.sent_at == FIXED_NOW
    assert sent.is_ours_auto is None  # [G6] 适配器不提供，核心层匹配回填
    linked = frozen.get_conversation_job("mock-conv-J002")
    assert linked is not None and linked.job_id == "J002"


def test_send_greeting_reuses_conversation_for_same_job(frozen: MockSiteAdapter) -> None:
    frozen.send_greeting(_job(frozen, "J002"), "第一条")
    frozen.send_greeting(_job(frozen, "J002"), "第二条")
    assert frozen.send_count == 2
    assert frozen.snapshot_conversation_baseline() == 4  # 不重复开会话
    assert [m.text for m in frozen.get_messages("mock-conv-J002", None)] == ["第一条", "第二条"]


def test_send_greeting_reuses_fixture_conversation(frozen: MockSiteAdapter) -> None:
    # J001 已有关联会话 C-1001（夹具）——招呼落入既有会话
    frozen.send_greeting(_job(frozen, "J001"), "追加沟通")
    assert frozen.snapshot_conversation_baseline() == 3
    last = frozen.get_messages("C-1001", None)[-1]
    assert last.text == "追加沟通" and last.direction == "out"


def test_send_greeting_platform_limit(frozen: MockSiteAdapter) -> None:
    frozen.scenario.platform_limit = True
    with pytest.raises(PlatformLimitError):
        frozen.send_greeting(_job(frozen, "J002"), "您好")
    assert frozen.send_count == 0


def test_send_reply_sent(frozen: MockSiteAdapter) -> None:
    assert frozen.send_reply("C-1001", "收到，谢谢反馈") is SendOutcome.SENT
    assert frozen.send_count == 1
    last = frozen.get_messages("C-1001", None)[-1]
    assert last.text == "收到，谢谢反馈" and last.sender == "me"


def test_send_reply_unknown_conversation(frozen: MockSiteAdapter) -> None:
    with pytest.raises(AdapterError, match="未知会话"):
        frozen.send_reply("C-9999", "x")


def test_send_reply_dry_run_and_platform_limit(frozen: MockSiteAdapter) -> None:
    frozen.dry_run = True
    assert frozen.send_reply("C-1001", "x") is SendOutcome.SKIPPED_DRYRUN
    assert frozen.send_count == 0
    frozen.dry_run = False
    frozen.scenario.platform_limit = True
    with pytest.raises(PlatformLimitError):
        frozen.send_reply("C-1001", "x")
    assert frozen.send_count == 0


def test_send_resume_requires_hr_reply(frozen: MockSiteAdapter) -> None:
    # C-1003（noreply 脚本）：无 HR 回复 → FAILED + 明确原因，不重试 [R12/A5]
    assert frozen.send_resume("C-1003") is SendOutcome.FAILED
    assert frozen.last_failed_reason is not None
    assert "HR 未回复" in frozen.last_failed_reason
    assert frozen.send_count == 0
    assert len(frozen.get_messages("C-1003", None)) == 1  # 仍只有脚本旧消息


def test_send_resume_sent(frozen: MockSiteAdapter) -> None:
    # C-1001（cold 脚本含 HR 回复）→ 可发送
    assert frozen.send_resume("C-1001") is SendOutcome.SENT
    assert frozen.send_count == 1
    last = frozen.get_messages("C-1001", None)[-1]
    assert last.msg_type is MsgType.FILE and last.direction == "out"


def test_send_resume_dry_run_unknown_and_limit(frozen: MockSiteAdapter) -> None:
    frozen.dry_run = True
    assert frozen.send_resume("C-1001") is SendOutcome.SKIPPED_DRYRUN
    frozen.dry_run = False
    with pytest.raises(AdapterError, match="未知会话"):
        frozen.send_resume("C-9999")
    frozen.scenario.platform_limit = True
    with pytest.raises(PlatformLimitError):
        frozen.send_resume("C-1001")


# ---- 会话三方法 + 基线 ----


def test_list_conversations_order_and_fields(frozen: MockSiteAdapter) -> None:
    convs = frozen.list_conversations()
    # 末条消息时间：C-1002=03:00Z > C-1001=02:30Z > C-1003=02:00Z
    assert [c.conv_id for c in convs] == ["C-1002", "C-1001", "C-1003"]
    top = convs[0]
    assert top.peer_name == "王总监"
    assert top.job_ref is not None and top.job_ref.job_id == "J003"
    assert top.unread == 0  # 夹具历史视为已读


def test_unread_lifecycle_after_injection(frozen: MockSiteAdapter) -> None:
    frozen.inject_incoming("C-1001", "在吗？")
    conv = next(c for c in frozen.list_conversations() if c.conv_id == "C-1001")
    assert conv.unread == 1
    frozen.get_messages("C-1001", None)
    conv = next(c for c in frozen.list_conversations() if c.conv_id == "C-1001")
    assert conv.unread == 0


def test_get_messages_all_ordered(frozen: MockSiteAdapter) -> None:
    messages = frozen.get_messages("C-1002", None)
    assert len(messages) == 4
    times = [m.sent_at for m in messages]
    assert times == sorted(times)
    types = [m.msg_type for m in messages]
    assert MsgType.RESUME_REQUEST_CARD in types  # warm 脚本含卡片消息


def test_get_messages_since_strictly_greater(frozen: MockSiteAdapter) -> None:
    all_messages = frozen.get_messages("C-1002", None)
    boundary = all_messages[1].sent_at
    later = frozen.get_messages("C-1002", boundary)
    assert [m.platform_msg_id for m in later] == [m.platform_msg_id for m in all_messages[2:]]
    assert frozen.get_messages("C-1002", FIXED_NOW) == []  # 未来时刻 → 空


def test_get_messages_unknown_conversation(frozen: MockSiteAdapter) -> None:
    with pytest.raises(AdapterError, match="未知会话"):
        frozen.get_messages("C-9999", None)


def test_get_conversation_job_linked_and_unknown(frozen: MockSiteAdapter) -> None:
    job = frozen.get_conversation_job("C-1001")
    assert job is not None and job.job_id == "J001"
    with pytest.raises(AdapterError, match="未知会话"):
        frozen.get_conversation_job("C-9999")


def test_snapshot_conversation_baseline(frozen: MockSiteAdapter) -> None:
    assert frozen.snapshot_conversation_baseline() == 3
    frozen.send_greeting(_job(frozen, "J002"), "您好")
    assert frozen.snapshot_conversation_baseline() == 4


# ---- 注入：来信 / 用户手回（接管检测）----


def test_inject_incoming_text_and_cards(frozen: MockSiteAdapter) -> None:
    msg = frozen.inject_incoming("C-1001", "方便发一下简历吗？")
    assert (msg.direction, msg.sender, msg.msg_type) == ("in", "hr", MsgType.TEXT)
    card = frozen.inject_incoming(
        "C-1001",
        "（系统卡片：对方请求交换联系方式）",
        msg_type=MsgType.CONTACT_REQUEST_CARD,
    )
    assert card.msg_type is MsgType.CONTACT_REQUEST_CARD
    system = frozen.inject_incoming("C-1001", "会话已建立", sender="system")
    assert system.sender == "system"
    assert frozen.send_count == 0  # 来信不计发送数
    texts = [m.text for m in frozen.get_messages("C-1001", None)]
    assert texts[-3:] == ["方便发一下简历吗？", "（系统卡片：对方请求交换联系方式）", "会话已建立"]


def test_inject_incoming_unknown_conversation(frozen: MockSiteAdapter) -> None:
    with pytest.raises(AdapterError, match="未知会话"):
        frozen.inject_incoming("C-9999", "x")


def test_inject_manual_outgoing_for_takeover_detection(frozen: MockSiteAdapter) -> None:
    # 非我方自动消息：未经 send_* 发出，核心层 digest 匹配不上 → 转人工 [G6/B3]
    msg = frozen.inject_manual_outgoing("C-1001", "我手动回一下")
    assert (msg.direction, msg.sender) == ("out", "me")
    assert msg.is_ours_auto is None
    assert frozen.send_count == 0
    assert "inject_manual_outgoing" in frozen.call_log


# ---- 崩溃点（"已发送未落库" [A-CORE-2]）----


def test_crash_after_send_greeting(frozen: MockSiteAdapter) -> None:
    frozen.scenario.crash_after_send = True
    with pytest.raises(MockCrashError):
        frozen.send_greeting(_job(frozen, "J002"), "您好")
    # 平台侧事实已发生：消息入会话 + 计数（重启对账依据，且不得重发）
    assert frozen.send_count == 1
    messages = frozen.get_messages("mock-conv-J002", None)
    assert messages[-1].text == "您好" and messages[-1].direction == "out"


def test_crash_after_send_reply(frozen: MockSiteAdapter) -> None:
    frozen.scenario.crash_after_send = True
    with pytest.raises(MockCrashError):
        frozen.send_reply("C-1001", "收到")
    assert frozen.send_count == 1
    assert frozen.get_messages("C-1001", None)[-1].text == "收到"


def test_mock_crash_error_is_not_adapter_error() -> None:
    # 崩溃语义：不可被 except AdapterError 兜底捕获（模拟进程死亡）
    assert not issubclass(MockCrashError, AdapterError)


# ---- 调用序列（A-CORE-4/5 取证）----


def test_call_log_records_sequence(frozen: MockSiteAdapter) -> None:
    frozen.check_login_state()
    frozen.detect_risk()
    frozen.search_jobs(SearchFilters(), 1)
    assert frozen.call_log[-3:] == ["check_login_state", "detect_risk", "search_jobs"]


def test_call_log_records_gate_failures(frozen: MockSiteAdapter) -> None:
    frozen.scenario.evicted = True
    with pytest.raises(RiskDetectedError):
        frozen.list_conversations()
    assert frozen.call_log[-1] == "list_conversations"  # 失败调用也留痕（审查依据）


# ---- 注册切换（与 ZhipinAdapter 同机制；create_adapter 装配）----


def test_create_adapter_mock_roundtrip() -> None:
    adapter = create_adapter("mock")
    assert isinstance(adapter, MockSiteAdapter)


def test_create_adapter_forwards_constructor_kwargs() -> None:
    adapter = create_adapter("mock", scenario=MockScenario(evicted=True), dry_run=True)
    assert isinstance(adapter, MockSiteAdapter)
    assert adapter.detect_risk() is RiskSignal.EVICTED
    adapter.scenario.evicted = False  # 风控解除后验证 dry_run 转发生效
    job = next(j for j in adapter.search_jobs(SearchFilters(), 1) if j.job_id == "J002")
    assert adapter.send_greeting(job, "您好") is SendOutcome.SKIPPED_DRYRUN


# ---- 夹具加载与校验（MockFixtureError）----

_TMP_JOB = {
    "job_id": "J001",
    "title": "后端工程师",
    "company_name": "测试公司",
    "company_key": "C001",
    "salary_text": "10-20K",
    "tags": ["Python"],
    "list_snippet": "摘要",
    "jd_text": "职责描述",
    "experience": "1-3年",
    "company_scale": "100-499人",
}
_TMP_MSG_OUT = {
    "offset_min": 0,
    "direction": "out",
    "sender": "me",
    "msg_type": "TEXT",
    "text": "您好",
}
_TMP_MSG_IN = {
    "offset_min": 10,
    "direction": "in",
    "sender": "hr",
    "msg_type": "TEXT",
    "text": "你好",
}


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _valid_dir(tmp_path: Path) -> Path:
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    _write_json(fixtures / "jobs.json", {"jobs": [dict(_TMP_JOB)]})
    _write_json(
        fixtures / "messages.json",
        {
            "base_time": "2026-11-07T02:00:00Z",
            "scripts": {"s1": [dict(_TMP_MSG_OUT), dict(_TMP_MSG_IN)]},
        },
    )
    _write_json(
        fixtures / "conversations.json",
        {
            "conversations": [
                {"conv_id": "C-1", "peer_name": "张三", "job_id": "J001", "script": "s1"}
            ]
        },
    )
    return fixtures


def test_missing_fixtures_dir(tmp_path: Path) -> None:
    with pytest.raises(MockFixtureError, match="夹具文件缺失"):
        MockSiteAdapter(tmp_path / "nope")


def test_minimal_fixtures_load_and_null_job_link(tmp_path: Path) -> None:
    fixtures = _valid_dir(tmp_path)
    convs = json.loads((fixtures / "conversations.json").read_text(encoding="utf-8"))
    convs["conversations"][0]["job_id"] = None  # 取不到关联岗位 [B8]
    _write_json(fixtures / "conversations.json", convs)
    adapter = MockSiteAdapter(fixtures)
    assert adapter.snapshot_conversation_baseline() == 1
    assert adapter.get_conversation_job("C-1") is None
    assert adapter.list_conversations()[0].job_ref is None
    assert len(adapter.get_messages("C-1", None)) == 2


@pytest.mark.parametrize(
    ("corrupt", "match"),
    [
        (lambda d: (d / "jobs.json").unlink(), "夹具文件缺失"),
        (lambda d: _write_json(d / "jobs.json", {"jobs": []}), "职位池为空"),
        (
            lambda d: _write_json(d / "jobs.json", {"jobs": [dict(_TMP_JOB), dict(_TMP_JOB)]}),
            "job_id 重复",
        ),
        (
            lambda d: _write_json(d / "jobs.json", {"positions": [dict(_TMP_JOB)]}),
            "必须是数组",  # 根节点缺 jobs 键 → get(None) 报非数组
        ),
        (lambda d: _write_json(d / "messages.json", {"scripts": {}}), "base_time"),
        (
            lambda d: _write_json(
                d / "messages.json",
                {"base_time": "2026-11-07T02:00:00", "scripts": {"s1": [dict(_TMP_MSG_OUT)]}},
            ),
            "必须带时区",
        ),
        (
            lambda d: _write_json(
                d / "messages.json",
                {
                    "base_time": "2026-11-07T02:00:00Z",
                    "scripts": {"s1": [dict(_TMP_MSG_OUT, msg_type="NOT_A_TYPE")]},
                },
            ),
            "未知 msg_type",
        ),
        (
            lambda d: _write_json(
                d / "messages.json",
                {
                    "base_time": "2026-11-07T02:00:00Z",
                    "scripts": {"s1": [dict(_TMP_MSG_OUT, direction="sideways")]},
                },
            ),
            "direction 必须为 in/out",
        ),
        (
            lambda d: _write_json(
                d / "messages.json",
                {
                    "base_time": "2026-11-07T02:00:00Z",
                    "scripts": {"s1": [dict(_TMP_MSG_OUT, sender="hr")]},
                },
            ),
            "sender 必须为 me",
        ),
        (
            lambda d: _write_json(
                d / "messages.json",
                {
                    "base_time": "2026-11-07T02:00:00Z",
                    "scripts": {"s1": [dict(_TMP_MSG_IN, sender="me")]},
                },
            ),
            "sender 必须为 hr/system",
        ),
        (
            lambda d: _write_json(
                d / "messages.json",
                {
                    "base_time": "2026-11-07T02:00:00Z",
                    "scripts": {"s1": [dict(_TMP_MSG_OUT, offset_min="5")]},
                },
            ),
            "offset_min 必须为整数",
        ),
        (
            lambda d: _write_json(
                d / "messages.json",
                {"base_time": "2026-11-07T02:00:00Z", "scripts": {"s1": []}},
            ),
            "消息脚本为空",
        ),
        (
            lambda d: _write_json(
                d / "conversations.json",
                {"conversations": [{"conv_id": "C-1", "peer_name": "张三", "script": "nope"}]},
            ),
            "引用未知脚本",
        ),
        (
            lambda d: _write_json(
                d / "conversations.json",
                {
                    "conversations": [
                        {"conv_id": "C-1", "peer_name": "张三", "job_id": "J404", "script": "s1"}
                    ]
                },
            ),
            "不在职位池中",
        ),
        (
            lambda d: _write_json(
                d / "conversations.json",
                {
                    "conversations": [
                        {"conv_id": "C-1", "peer_name": "张三", "job_id": "J001", "script": "s1"},
                        {"conv_id": "C-1", "peer_name": "李四", "job_id": "J001", "script": "s1"},
                    ]
                },
            ),
            "conv_id 重复",
        ),
        (lambda d: _write_json(d / "conversations.json", {"conversations": []}), "会话池为空"),
    ],
)
def test_fixture_validation_errors(tmp_path: Path, corrupt: Any, match: str) -> None:
    fixtures = _valid_dir(tmp_path)
    corrupt(fixtures)
    with pytest.raises(MockFixtureError, match=match):
        MockSiteAdapter(fixtures)
