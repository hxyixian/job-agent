"""MockSiteAdapter：本地数据驱动的模拟站点（设计文档 §12 / 开发文档 S4a）。

- **零网络/零浏览器**（S4a 禁止事项）：全部状态为本进程内存，数据源 =
  ``tests/fixtures/sim/*.json``（职位池 jobs.json / 会话池 conversations.json /
  消息脚本 messages.json，合成数据）；
- 实现全部 11 个 :class:`~job_agent.adapters.base.SiteAdapter` 方法，注册机制
  与 ZhipinAdapter 完全同构（模块导入即 ``register_adapter("mock", ...)``，
  ``config.site.adapter=mock`` 一键切换，TEST5 依赖）；
- **dry_run 语义**：引擎侧在调用 send_* 之前已短路（A-CORE-1 判据）；Mock
  自身的 ``dry_run=True`` 为第二道保险——send_* 恒返回 ``SKIPPED_DRYRUN``、
  不产生任何会话消息、发送计数不变；
- 观测接口（供 S4c/S4d/S7 集成测试与验收取证）：
  ``send_count``（实际发出数，崩溃点也计数）、``last_failed_reason``
  （send_resume 前置校验失败原因）、``call_log``（方法调用序列——A-CORE-5
  "adapter 调用序列审查"、A-CORE-4 "无自动重导航=熔断后不再有任何调用"的
  证据源）。

能力开关（``MockScenario``，测试持有同一实例即可运行中翻转）：

- ``login_state``：登录页/登录态未知——check_login_state 返回注入值，
  操作方法抛 LoginRequiredError；
- ``security_check`` [H1/A-CORE-20]：三种形态任选（URL 带
  ``_security_check`` / 页面"安全验证"文字 / 导航弹回首页连续 2 次）——
  detect_risk→SECURITY_CHECK，操作方法抛 RiskDetectedError(SECURITY_CHECK)；
- ``evicted`` [G2/A-CORE-4]：URL→about:blank（驱逐）——detect_risk→EVICTED，
  操作方法抛 RiskDetectedError(EVICTED)；
- ``captcha`` / ``risk_page`` [T4-3]：验证码页 / 风控提示页——
  detect_risk→CAPTCHA / RISK_PAGE，操作方法抛 RiskDetectedError；
- ``platform_limit`` [B6/A-CORE-3]：平台"今日沟通达上限"——send_* 抛
  PlatformLimitError；detect_risk→PLATFORM_LIMIT；
- ``content_missing`` [H1/A-CORE-20]：预期内容缺失（搜索页零职位卡/
  详情页无 JD 正文）——返回空结果/空正文并计入连续失败，
  第 3 次抛 AdapterBrokenError [B7]；
- ``transient_error``：网络抖动（§5.2 TransientError 路径，
  供 S4c 退避重试用）——操作方法抛 TransientError；
- ``crash_after_send`` [A-CORE-2]：崩溃点"已发送未落库"——消息已入会话、
  send_count 已 +1，随后抛 :class:`MockCrashError`。

使用示例（S6/验收注入速查）::

    from job_agent.adapters.base import SearchFilters
    from job_agent.adapters.mock import MockScenario, MockSiteAdapter

    scn = MockScenario(evicted=True)                       # A-CORE-4
    mock = MockSiteAdapter("tests/fixtures/sim", scenario=scn)
    mock.detect_risk()                                     # RiskSignal.EVICTED
    mock.search_jobs(SearchFilters(), 1)                   # RiskDetectedError(EVICTED)

    mock.inject_incoming("C-1001", "方便发一下简历吗？")     # 注入 HR 来信（路由）
    mock.inject_manual_outgoing("C-1001", "好的我看看")      # 注入用户手回（接管检测 [G6/B3]）
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

from job_agent.adapters.base import (
    DEFAULT_BROKEN_THRESHOLD,
    AdapterError,
    LoginRequiredError,
    PlatformLimitError,
    RiskDetectedError,
    SearchFilters,
    SiteAdapter,
    TransientError,
    register_adapter,
)
from job_agent.config import app_root
from job_agent.models import (
    Conversation,
    JobDetail,
    JobRef,
    LoginState,
    Message,
    MsgType,
    RiskSignal,
    SendOutcome,
)
from job_agent.timeutil import as_utc

__all__ = [
    "MOCK_PAGE_SIZE",
    "MockCrashError",
    "MockFixtureError",
    "MockScenario",
    "MockSiteAdapter",
    "SecurityCheckForm",
]

#: search_jobs 每页条数（15 条职位池 → 第 1 页 10 条、第 2 页 5 条、第 3 页空）
MOCK_PAGE_SIZE = 10

#: 合成详情 URL 模板（纯展示文本，.invalid 域名保证不可误访问；Mock 永不联网）
_MOCK_DETAIL_URL = "https://mock.zhipin.invalid/job_detail/{job_id}.html"


def _default_fixtures_dir() -> Path:
    """默认夹具目录 = <应用根>/tests/fixtures/sim [R6-2 路径锚定应用根]。"""
    return app_root() / "tests" / "fixtures" / "sim"


class MockCrashError(RuntimeError):
    """崩溃点注入异常（A-CORE-2"已发送未落库"）。

    **刻意不是 AdapterError 子类**：崩溃 = 调用方的异常处理本身也来不及执行
    （模拟进程死亡），若可被 ``except AdapterError`` 捕获就不是崩溃语义。
    调用方不应捕获本异常；测试用它模拟"kill 进程"，重启后经
    startup_recover → unknown → 对账（get_messages 可读到已发出的消息）。
    """


class MockFixtureError(ValueError):
    """夹具数据缺失/非法（加载期错误，不属于运行期异常树）。"""


class SecurityCheckForm(StrEnum):
    """SECURITY_CHECK 注入形态（[H1]——A-CORE-20 三种来源，接口表现一致）。"""

    URL_MARKER = "url_marker"  # URL 含 _security_check / 验证类路径
    PAGE_TEXT = "page_text"  # 页面出现"安全验证、环境异常、验证码"类文字
    NAV_BOUNCE = "nav_bounce"  # 导航后停留页非目标页连续 2 次（弹回首页）


@dataclass
class MockScenario:
    """Mock 能力开关集合（见模块 docstring 的开关表；测试可运行中翻转）。"""

    login_state: LoginState = LoginState.LOGGED_IN
    security_check: SecurityCheckForm | None = None  # 非 None → SECURITY_CHECK
    evicted: bool = False  # URL→about:blank（EVICTED [G2]）
    captcha: bool = False  # 验证码页
    risk_page: bool = False  # 风控提示页
    platform_limit: bool = False  # 平台沟通上限 [B6]
    content_missing: bool = False  # 预期内容缺失（内容级健康检查失败 [H1/B7]）
    transient_error: bool = False  # 瞬时错误（TransientError 路径）
    crash_after_send: bool = False  # 崩溃点"已发送未落库" [A-CORE-2]


# ---- 夹具内存形态 ----


@dataclass
class _MockJob:
    """职位池条目（JobRef 字段 + 详情页补充字段）。"""

    job_id: str
    title: str
    company_name: str
    company_key: str
    salary_text: str | None
    tags: list[str]
    list_snippet: str
    jd_text: str
    experience: str
    company_scale: str

    def to_ref(self) -> JobRef:
        return JobRef(
            job_id=self.job_id,
            title=self.title,
            company_name=self.company_name,
            company_key=self.company_key,
            salary_text=self.salary_text,
            tags=list(self.tags),
            list_snippet=self.list_snippet,
        )

    def to_detail(self, jd_text: str | None = None) -> JobDetail:
        return JobDetail(
            job_id=self.job_id,
            title=self.title,
            company_name=self.company_name,
            company_key=self.company_key,
            salary_text=self.salary_text,
            tags=list(self.tags),
            list_snippet=self.list_snippet,
            jd_text=self.jd_text if jd_text is None else jd_text,
            experience=self.experience,
            company_scale=self.company_scale,
            detail_url=_MOCK_DETAIL_URL.format(job_id=self.job_id),
        )


@dataclass
class _MockConv:
    """会话内存形态（消息追加式；``_watermark`` 为已读取水位）。"""

    conv_id: str
    peer_name: str
    job_ref: JobRef | None
    messages: list[Message] = field(default_factory=list)
    _watermark: int = 0

    def last_message_at(self) -> datetime:
        return max(m.sent_at for m in self.messages)

    def unread(self) -> int:
        return sum(1 for m in self.messages[self._watermark :] if m.direction == "in")

    def mark_read(self) -> None:
        self._watermark = len(self.messages)

    def has_hr_reply(self) -> bool:
        return any(m.direction == "in" and m.sender == "hr" for m in self.messages)


@dataclass
class _ScriptMsg:
    """消息脚本条目（offset_min 相对 base_time）。"""

    direction: Literal["in", "out"]
    sender: Literal["hr", "me", "system"]
    msg_type: MsgType
    text: str
    offset_min: int


# ---- 夹具加载与校验 ----


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise MockFixtureError(f"夹具文件缺失: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise MockFixtureError(f"夹具 JSON 解析失败: {path}: {exc}") from exc


def _as_dict(value: Any, ctx: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MockFixtureError(f"{ctx} 必须是对象，实际为 {type(value).__name__}")
    return value


def _as_list(value: Any, ctx: str) -> list[Any]:
    if not isinstance(value, list):
        raise MockFixtureError(f"{ctx} 必须是数组，实际为 {type(value).__name__}")
    return value


def _req_str(container: dict[str, Any], key: str, ctx: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MockFixtureError(f"{ctx}: 字段 {key!r} 必须为非空字符串")
    return value


def _str_or_none(container: dict[str, Any], key: str, ctx: str) -> str | None:
    value = container.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MockFixtureError(f"{ctx}: 字段 {key!r} 必须为字符串或 null")
    return value


def _str_list(container: dict[str, Any], key: str, ctx: str) -> list[str]:
    value = container.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise MockFixtureError(f"{ctx}: 字段 {key!r} 必须为字符串数组")
    return list(value)


def _load_jobs(raw: Any) -> dict[str, _MockJob]:
    root = _as_dict(raw, "jobs.json")
    jobs: dict[str, _MockJob] = {}
    for i, item in enumerate(_as_list(root.get("jobs"), "jobs.json jobs")):
        ctx = f"jobs.json jobs[{i}]"
        entry = _as_dict(item, ctx)
        job = _MockJob(
            job_id=_req_str(entry, "job_id", ctx),
            title=_req_str(entry, "title", ctx),
            company_name=_req_str(entry, "company_name", ctx),
            company_key=_req_str(entry, "company_key", ctx),
            salary_text=_str_or_none(entry, "salary_text", ctx),
            tags=_str_list(entry, "tags", ctx),
            list_snippet=_req_str(entry, "list_snippet", ctx),
            jd_text=_req_str(entry, "jd_text", ctx),
            experience=_req_str(entry, "experience", ctx),
            company_scale=_req_str(entry, "company_scale", ctx),
        )
        if job.job_id in jobs:
            raise MockFixtureError(f"{ctx}: job_id 重复 {job.job_id!r}")
        jobs[job.job_id] = job
    if not jobs:
        raise MockFixtureError("jobs.json: 职位池为空")
    return jobs


def _load_scripts(raw: Any) -> tuple[datetime, dict[str, list[_ScriptMsg]]]:
    root = _as_dict(raw, "messages.json")
    base_raw = _req_str(root, "base_time", "messages.json")
    try:
        base_time = datetime.fromisoformat(base_raw)
    except ValueError as exc:
        raise MockFixtureError(f"messages.json: base_time 非法 ISO 时间 {base_raw!r}") from exc
    if base_time.tzinfo is None:
        raise MockFixtureError("messages.json: base_time 必须带时区（如 ...Z）")
    scripts_raw = _as_dict(root.get("scripts"), "messages.json scripts")
    scripts: dict[str, list[_ScriptMsg]] = {}
    for name, msgs_raw in scripts_raw.items():
        ctx = f"messages.json scripts[{name!r}]"
        msgs: list[_ScriptMsg] = []
        for i, item in enumerate(_as_list(msgs_raw, ctx)):
            mctx = f"{ctx}[{i}]"
            entry = _as_dict(item, mctx)
            direction = _req_str(entry, "direction", mctx)
            sender = _req_str(entry, "sender", mctx)
            if direction not in ("in", "out"):
                raise MockFixtureError(f"{mctx}: direction 必须为 in/out")
            if direction == "out" and sender != "me":
                raise MockFixtureError(f"{mctx}: direction=out 时 sender 必须为 me")
            if direction == "in" and sender not in ("hr", "system"):
                raise MockFixtureError(f"{mctx}: direction=in 时 sender 必须为 hr/system")
            type_name = _req_str(entry, "msg_type", mctx)
            try:
                msg_type = MsgType[type_name]
            except KeyError as exc:
                raise MockFixtureError(f"{mctx}: 未知 msg_type {type_name!r}") from exc
            offset = entry.get("offset_min")
            if not isinstance(offset, int) or isinstance(offset, bool):
                raise MockFixtureError(f"{mctx}: offset_min 必须为整数分钟")
            msgs.append(
                _ScriptMsg(
                    direction=cast(Literal["in", "out"], direction),
                    sender=cast(Literal["hr", "me", "system"], sender),
                    msg_type=msg_type,
                    text=_req_str(entry, "text", mctx),
                    offset_min=offset,
                )
            )
        if not msgs:
            raise MockFixtureError(f"{ctx}: 消息脚本为空")
        scripts[str(name)] = msgs
    if not scripts:
        raise MockFixtureError("messages.json: scripts 为空")
    return base_time, scripts


def _load_conversations(
    raw: Any,
    jobs: dict[str, _MockJob],
    base_time: datetime,
    scripts: dict[str, list[_ScriptMsg]],
) -> dict[str, _MockConv]:
    root = _as_dict(raw, "conversations.json")
    conversations: dict[str, _MockConv] = {}
    entries = _as_list(root.get("conversations"), "conversations.json conversations")
    for i, item in enumerate(entries):
        ctx = f"conversations.json conversations[{i}]"
        entry = _as_dict(item, ctx)
        conv_id = _req_str(entry, "conv_id", ctx)
        if conv_id in conversations:
            raise MockFixtureError(f"{ctx}: conv_id 重复 {conv_id!r}")
        job_id = _str_or_none(entry, "job_id", ctx)
        job_ref: JobRef | None = None
        if job_id is not None:
            job = jobs.get(job_id)
            if job is None:
                raise MockFixtureError(f"{ctx}: job_id {job_id!r} 不在职位池中")
            job_ref = job.to_ref()
        script_name = _req_str(entry, "script", ctx)
        script = scripts.get(script_name)
        if script is None:
            raise MockFixtureError(f"{ctx}: 引用未知脚本 {script_name!r}")
        conv = _MockConv(
            conv_id=conv_id,
            peer_name=_req_str(entry, "peer_name", ctx),
            job_ref=job_ref,
        )
        for j, sm in enumerate(script):
            conv.messages.append(
                Message(
                    platform_msg_id=f"{conv_id}-{j + 1:03d}",
                    conv_id=conv_id,
                    direction=sm.direction,
                    sender=sm.sender,
                    msg_type=sm.msg_type,
                    text=sm.text,
                    sent_at=as_utc(base_time) + timedelta(minutes=sm.offset_min),
                )
            )
        conv.mark_read()  # 夹具历史视为已读：unread 只反映注入/新发送
        conversations[conv_id] = conv
    if not conversations:
        raise MockFixtureError("conversations.json: 会话池为空")
    return conversations


def _load_fixtures(fixtures_dir: Path) -> tuple[dict[str, _MockJob], dict[str, _MockConv]]:
    jobs = _load_jobs(_read_json(fixtures_dir / "jobs.json"))
    base_time, scripts = _load_scripts(_read_json(fixtures_dir / "messages.json"))
    conversations = _load_conversations(
        _read_json(fixtures_dir / "conversations.json"), jobs, base_time, scripts
    )
    return jobs, conversations


# ---- 适配器实现 ----


class MockSiteAdapter(SiteAdapter):
    """全部 11 方法的数据驱动 Mock 实现（§12；能力开关见模块 docstring）。"""

    site_id = "mock"

    def __init__(
        self,
        fixtures_dir: str | Path | None = None,
        *,
        scenario: MockScenario | None = None,
        dry_run: bool = False,
        clock: Callable[[], datetime] | None = None,
        broken_threshold: int = DEFAULT_BROKEN_THRESHOLD,
    ) -> None:
        super().__init__(broken_threshold=broken_threshold)
        self.fixtures_dir = (
            Path(fixtures_dir) if fixtures_dir is not None else _default_fixtures_dir()
        )
        self.scenario = scenario if scenario is not None else MockScenario()
        self.dry_run = dry_run
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._jobs, self._convs = _load_fixtures(self.fixtures_dir)
        # 观测接口（见模块 docstring）
        self.send_count = 0
        self.last_failed_reason: str | None = None
        self.call_log: list[str] = []

    # ---- 内部门控 ----

    def _now(self) -> datetime:
        return as_utc(self._clock())

    def _log(self, op: str) -> None:
        self.call_log.append(op)

    def _active_risk(self) -> RiskSignal:
        """开关 → RiskSignal（优先级 SECURITY_CHECK > EVICTED > CAPTCHA > RISK_PAGE）。"""
        scn = self.scenario
        if scn.security_check is not None:
            return RiskSignal.SECURITY_CHECK
        if scn.evicted:
            return RiskSignal.EVICTED
        if scn.captcha:
            return RiskSignal.CAPTCHA
        if scn.risk_page:
            return RiskSignal.RISK_PAGE
        return RiskSignal.NONE

    def _gate(self, op: str) -> None:
        """操作方法统一门控：记录调用序列 + 登录/风控/瞬时错误注入。

        探测方法（check_login_state/detect_risk）不走本门——它们正是失效时
        也要能返回状态的路径；注入类辅助方法（inject_*）模拟平台侧事实，
        也不受门控。
        """
        self._log(op)
        if self.scenario.login_state is not LoginState.LOGGED_IN:
            raise LoginRequiredError(
                f"mock 登录态注入: {self.scenario.login_state.name}（UNKNOWN 保守按登录失效处理）"
            )
        risk = self._active_risk()
        if risk is not RiskSignal.NONE:
            raise RiskDetectedError(risk, "mock 风控注入（见 MockScenario 开关表）")
        if self.scenario.transient_error:
            raise TransientError("mock 瞬时错误注入（TransientError 路径）")

    def _send_precheck(self, op: str) -> bool:
        """send_* 共同前置：门控 → dry_run 短路 → 平台限额。

        返回 True = dry_run 短路（调用方直接返回 SKIPPED_DRYRUN，零副作用）。
        """
        self._gate(op)
        if self.dry_run:
            return True
        if self.scenario.platform_limit:
            raise PlatformLimitError("mock 平台限额注入：今日沟通达上限 [B6]")
        return False

    def _conv(self, conv_id: str) -> _MockConv:
        conv = self._convs.get(conv_id)
        if conv is None:
            raise AdapterError(f"未知会话: {conv_id!r}（不在 mock 会话池中）")
        return conv

    def _append(
        self,
        conv: _MockConv,
        *,
        direction: Literal["in", "out"],
        sender: Literal["hr", "me", "system"],
        msg_type: MsgType,
        text: str,
    ) -> Message:
        message = Message(
            platform_msg_id=f"{conv.conv_id}-{len(conv.messages) + 1:03d}",
            conv_id=conv.conv_id,
            direction=direction,
            sender=sender,
            msg_type=msg_type,
            text=text,
            sent_at=self._now(),
        )
        conv.messages.append(message)
        return message

    def _deliver(self, conv: _MockConv, msg_type: MsgType, text: str) -> None:
        """落一条我方发出消息并计数；崩溃点开启时先发出再抛 MockCrashError。"""
        self._append(conv, direction="out", sender="me", msg_type=msg_type, text=text)
        self.send_count += 1
        if self.scenario.crash_after_send:
            raise MockCrashError(
                "mock 崩溃点：消息已发出（平台侧送达，get_messages 可对账），"
                "但结果未返回调用方——模拟'已发送未落库'[A-CORE-2]"
            )

    # ---- 11 方法实现 ----

    def check_login_state(self) -> LoginState:
        self._log("check_login_state")
        return self.scenario.login_state

    def detect_risk(self) -> RiskSignal:
        self._log("detect_risk")
        risk = self._active_risk()
        if risk is not RiskSignal.NONE:
            return risk
        if self.scenario.platform_limit:
            return RiskSignal.PLATFORM_LIMIT
        return RiskSignal.NONE

    def search_jobs(self, filters: SearchFilters, page: int) -> list[JobRef]:
        self._gate("search_jobs")
        if page < 1:
            raise AdapterError(f"page 必须 >= 1，收到 {page}")
        if self.scenario.content_missing:
            # 搜索页零职位卡 [H1]——计入连续失败，第 3 次熔断 AdapterBrokenError [B7]
            self.note_element_failure("搜索页零职位卡（内容级健康检查失败）")
            return []
        self.note_element_success()
        matched = [
            job.to_ref()
            for job in self._jobs.values()
            if not filters.keywords
            or any(
                keyword.lower() in job.title.lower()
                or keyword.lower() in job.company_name.lower()
                or any(keyword.lower() in tag.lower() for tag in job.tags)
                for keyword in filters.keywords
            )
        ]
        start = (page - 1) * MOCK_PAGE_SIZE
        return matched[start : start + MOCK_PAGE_SIZE]

    def get_job_detail(self, job_id: str) -> JobDetail:
        self._gate("get_job_detail")
        job = self._jobs.get(job_id)
        if job is None:
            # 单职位缺卡/已下线：未分类错误（调用方跳过该职位），不计连续失败 [NFR2]
            raise AdapterError(f"job 不存在或已下线: {job_id!r}")
        if self.scenario.content_missing:
            # 详情页无 JD 正文 [H1]——计入连续失败，第 3 次熔断 AdapterBrokenError [B7]
            self.note_element_failure("详情页无 JD 正文（内容级健康检查失败）")
            return job.to_detail(jd_text="")
        self.note_element_success()
        return job.to_detail()

    def send_greeting(self, job: JobRef, message: str) -> SendOutcome:
        if self._send_precheck("send_greeting"):
            return SendOutcome.SKIPPED_DRYRUN
        conv = self._ensure_job_conversation(job)
        self._deliver(conv, MsgType.TEXT, message)
        return SendOutcome.SENT

    def send_reply(self, conv_id: str, message: str) -> SendOutcome:
        if self._send_precheck("send_reply"):
            return SendOutcome.SKIPPED_DRYRUN
        conv = self._conv(conv_id)
        self._deliver(conv, MsgType.TEXT, message)
        return SendOutcome.SENT

    def send_resume(self, conv_id: str) -> SendOutcome:
        if self._send_precheck("send_resume"):
            return SendOutcome.SKIPPED_DRYRUN
        conv = self._conv(conv_id)
        if not conv.has_hr_reply():
            # R12 实测前置校验：FAILED + 明确原因，不抛异常、不重试 [A5/A-CORE-27]
            self.last_failed_reason = f"HR 未回复，附件简历入口为禁用态，无法发送: {conv_id}"
            return SendOutcome.FAILED
        self._deliver(conv, MsgType.FILE, "（附件简历已发送）")
        return SendOutcome.SENT

    def list_conversations(self) -> list[Conversation]:
        self._gate("list_conversations")
        ordered = sorted(
            self._convs.values(),
            key=lambda c: (c.last_message_at(), c.conv_id),
            reverse=True,
        )
        return [
            Conversation(
                conv_id=c.conv_id,
                peer_name=c.peer_name,
                job_ref=c.job_ref,
                last_message_at=c.last_message_at(),
                unread=c.unread(),
            )
            for c in ordered
        ]

    def get_messages(self, conv_id: str, since: datetime | None) -> list[Message]:
        self._gate("get_messages")
        conv = self._conv(conv_id)
        if since is None:
            result = list(conv.messages)
        else:
            boundary = as_utc(since)
            result = [m for m in conv.messages if as_utc(m.sent_at) > boundary]
        conv.mark_read()
        return result

    def get_conversation_job(self, conv_id: str) -> JobRef | None:
        self._gate("get_conversation_job")
        return self._conv(conv_id).job_ref

    def snapshot_conversation_baseline(self) -> int:
        self._gate("snapshot_conversation_baseline")
        return len(self._convs)

    # ---- Mock 专用：会话创建与注入（绕过门控——模拟平台侧事实）----

    def _ensure_job_conversation(self, job: JobRef) -> _MockConv:
        """取职位关联会话；无则创建（模拟点击"立即沟通"后平台开新会话）。"""
        for conv in self._convs.values():
            if conv.job_ref is not None and conv.job_ref.job_id == job.job_id:
                return conv
        conv_id = f"mock-conv-{job.job_id}"
        conv = _MockConv(conv_id=conv_id, peer_name=f"{job.company_name}·HR", job_ref=job)
        self._convs[conv_id] = conv
        return conv

    def inject_incoming(
        self,
        conv_id: str,
        text: str,
        *,
        msg_type: MsgType = MsgType.TEXT,
        sender: Literal["hr", "system"] = "hr",
    ) -> Message:
        """注入平台侧来信（HR 消息/系统卡片），驱动路由/待发箱/计数场景。

        时间取 ``clock()``；不计数、不受能力开关门控（外部事实）。
        """
        self._log("inject_incoming")
        conv = self._conv(conv_id)
        return self._append(conv, direction="in", sender=sender, msg_type=msg_type, text=text)

    def inject_manual_outgoing(self, conv_id: str, text: str) -> Message:
        """注入**非我方自动**的我方消息：模拟用户在平台 App 手动回复。

        该消息未经 send_* 发出——核心层 digest 匹配不上 send_intents →
        ``is_ours_auto=None`` → 按人工消息处理（手动接管检测 [G6/B3]）。
        """
        self._log("inject_manual_outgoing")
        conv = self._conv(conv_id)
        return self._append(conv, direction="out", sender="me", msg_type=MsgType.TEXT, text=text)


# 导入即注册（与 ZhipinAdapter 同机制；adapters/__init__ 导入本模块完成注册）
register_adapter("mock", MockSiteAdapter)
