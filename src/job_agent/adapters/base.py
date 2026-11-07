"""SiteAdapter 抽象基类 + 异常树 + 注册表（设计文档 §5.2，本模块为唯一权威定义方）。

- 全部方法**同步**：引擎单线程串行调用（单执行者队列 [B5]），与 Playwright
  同步 API 匹配；适配器实现不得自行开线程/异步任务；
- ``LoginState`` / ``RiskSignal`` / 各领域模型（JobRef/JobDetail/Conversation/
  Message/SendOutcome）复用 :mod:`job_agent.models`（S2a 交付，§5.1 唯一权威）；
- ``SearchFilters`` 为 ``search_jobs`` 入参，随本契约定义于本模块（§5.2 签名
  引用、§5.1 未列——搜索词来自已确认职位方向 JobDirection.keywords、城市来自
  config.target.city；薪资维度已废止 [R13]）。

错误分类与调用方行为契约（§5.2 表，含 EVICTED/SECURITY_CHECK [H1 修订]）：

- ``LoginRequiredError``（登录失效）→ 熔断：停全部任务 + 高告警 [FR1.3]；
  恢复走登录模式（M3/G5），零登录动作。
- ``RiskDetectedError(signal)``（验证码/风控页/驱逐/安全拦截）→ 熔断：
  暂停全部任务 + 高告警 [FR1.4]。signal ∈ {CAPTCHA, RISK_PAGE, EVICTED,
  SECURITY_CHECK}，其中——
  - EVICTED [G2]：URL 变 about:blank 或非我方发起的异常跳转；
  - SECURITY_CHECK [H1]：URL 含 ``_security_check``/验证类路径，或页面出现
    "安全验证、环境异常、验证码"类文字，或导航后停留页非目标页连续 2 次；
  - 两者均**禁止自动重导航**、拦截前置不自动重建浏览器 [M5]，等人工处理。
- ``AdapterBrokenError``（关键元素连续 N 次定位失败，默认 3，可配
  ``broken_threshold`` [B7]；内容级健康检查失败同样计入 [H1]）→
  熔断：暂停 + 高告警。
- ``PlatformLimitError``（平台"今日沟通达上限"类提示 [B6]）→
  当日停止投递；消息处理照常；中级通知。
- ``TransientError``（网络抖动等临时错误）→ 指数退避重试 ≤2 次，
  仍失败按 FAILED 落库。
- 其他 ``AdapterError``（未分类）→ 当前职位/会话标记失败跳过，
  循环继续 [NFR2]。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from job_agent.models import (
    Conversation,
    JobDetail,
    JobRef,
    LoginState,
    Message,
    RiskSignal,
    SendOutcome,
)

__all__ = [
    "AdapterBrokenError",
    "AdapterError",
    "LoginRequiredError",
    "PlatformLimitError",
    "RiskDetectedError",
    "SearchFilters",
    "SiteAdapter",
    "TransientError",
    "UnknownAdapterError",
    "create_adapter",
    "register_adapter",
    "registered_adapters",
]


# ---- 异常树（六类，§5.2 表）----


class AdapterError(Exception):
    """适配器错误基类（表中"未分类"行：调用方按 当前职位/会话失败跳过 处理 [NFR2]）。"""


class LoginRequiredError(AdapterError):
    """登录失效：熔断——停全部任务 + 高告警 [FR1.3]，恢复走登录模式（零登录动作）。"""


class RiskDetectedError(AdapterError):
    """验证码/风控页/驱逐/安全拦截：熔断——暂停全部任务 + 高告警 [FR1.4]。

    ``signal`` 携带具体信号（§5.2 RiskSignal）。EVICTED / SECURITY_CHECK
    额外约束：禁止自动重导航、不自动重建浏览器，等人工处理 [G2/H1/M5]。
    """

    def __init__(self, signal: RiskSignal, detail: str = "") -> None:
        self.signal = signal
        self.detail = detail
        super().__init__(f"risk signal {signal.name}" + (f": {detail}" if detail else ""))


class AdapterBrokenError(AdapterError):
    """关键元素连续 N 次（默认 3，可配）定位失败 [B7]：熔断——暂停 + 高告警。

    内容级健康检查失败（页面加载成功但预期内容缺失，如搜索页零职位卡、
    详情页无 JD 正文）同样计入连续失败计数 [H1]。
    """


class PlatformLimitError(AdapterError):
    """平台"今日沟通达上限"类提示 [B6]：当日停止投递；消息处理照常；中级通知。"""


class TransientError(AdapterError):
    """网络抖动等临时错误：调用方指数退避重试 ≤2 次，仍失败按 FAILED 落库。"""


class UnknownAdapterError(ValueError):
    """``create_adapter`` 收到未注册的 site_id（装配期错误，不属于运行期异常树）。"""


# ---- search_jobs 入参 ----


@dataclass
class SearchFilters:
    """搜索过滤条件（§5.2 ``search_jobs`` 入参）。

    - ``keywords``：搜索关键词（来自已确认职位方向 ``JobDirection.keywords``，
      多组并集传入；空表 = 不过滤，由适配器按平台默认流返回）；
    - ``city``：求职目标城市（``config.target.city``）；
    - 薪资维度已废止 [R13]，不设薪资字段。
    """

    keywords: list[str] = field(default_factory=list)
    city: str = ""


# ---- 抽象基类 ----

#: AdapterBrokenError 的默认连续失败阈值（可被构造参数覆盖）[B7]
DEFAULT_BROKEN_THRESHOLD = 3


class SiteAdapter(ABC):
    """招聘站点适配器抽象（11 方法，全部同步，§5.2）。

    实现约束：
    - ``site_id`` 类属性必须与非空注册名一致（``register_adapter`` 校验）；
    - 全同步、无自发线程；一个实例代表一个站点会话，由引擎独占串行调用；
    - 关键元素定位失败一律经 :meth:`note_element_failure` 计数（达到
      ``broken_threshold`` 抛 :class:`AdapterBrokenError`），成功路径调用
      :meth:`note_element_success` 归零——Mock 与 Zhipin 共用该机制；
    - ``Message.is_ours_auto`` 由核心层与 send_intents 匹配回填 [G6]，
      适配器恒置 None。
    """

    site_id: str = ""

    def __init__(self, broken_threshold: int = DEFAULT_BROKEN_THRESHOLD) -> None:
        if broken_threshold < 1:
            raise ValueError(f"broken_threshold 必须 >= 1，收到 {broken_threshold}")
        self.broken_threshold = broken_threshold
        self._element_failures = 0

    # ---- 连续定位失败计数（AdapterBrokenError 判定，[B7/H1]）----

    @property
    def consecutive_element_failures(self) -> int:
        """当前连续关键元素定位失败次数（观测用；熔断抛错后保持 >= 阈值）。"""
        return self._element_failures

    def note_element_failure(self, what: str) -> None:
        """记录一次关键元素定位失败（含内容级健康检查失败 [H1]）。

        连续达到 ``broken_threshold``（默认 3）抛 :class:`AdapterBrokenError`
        （熔断由调用方执行）。抛错后计数**不归零**：后续任何一次失败立即
        再次抛出，直到某次成功（:meth:`note_element_success`）或显式
        :meth:`reset_element_failures`（人工恢复/重建会话后）。
        """
        self._element_failures += 1
        if self._element_failures >= self.broken_threshold:
            raise AdapterBrokenError(
                f"关键元素连续 {self._element_failures} 次定位失败（阈值 "
                f"{self.broken_threshold}，最近一次: {what}）[B7]"
            )

    def note_element_success(self) -> None:
        """记录一次关键元素定位成功：连续失败计数归零 [B7]。"""
        self._element_failures = 0

    def reset_element_failures(self) -> None:
        """显式清零连续失败计数（人工恢复/浏览器重建后由调用方调用）。"""
        self._element_failures = 0

    # ---- 11 方法契约 ----

    @abstractmethod
    def check_login_state(self) -> LoginState:
        """检测登录态（更名自 ensure_login_state [B8]）。

        返回三态；LOGGED_OUT/UNKNOWN 时调用方按登录失效熔断处理——
        本方法只探测，不抛 LoginRequiredError（那是操作方法的事）。
        """

    @abstractmethod
    def detect_risk(self) -> RiskSignal:
        """探测当前页面风控信号（NONE=无）。

        EVICTED / SECURITY_CHECK 的判定来源见 §5.2/§5.3 [G2/H1]；本方法
        只探测，不抛 RiskDetectedError（那是操作方法的事）。
        """

    @abstractmethod
    def search_jobs(self, filters: SearchFilters, page: int) -> list[JobRef]:
        """按关键词搜索职位列表（page 从 1 起；无更多结果返回空表）。"""

    @abstractmethod
    def get_job_detail(self, job_id: str) -> JobDetail:
        """取职位详情（JD 正文缺失属内容级健康检查失败，计入连续失败计数 [H1]）。"""

    @abstractmethod
    def send_greeting(self, job: JobRef, message: str) -> SendOutcome:
        """向职位发送招呼语（点击"立即沟通" + 定稿招呼语，A1 机制）。

        平台限额触发时抛 :class:`PlatformLimitError` [B6]。
        """

    @abstractmethod
    def send_reply(self, conv_id: str, message: str) -> SendOutcome:
        """向会话发送文本回复。"""

    @abstractmethod
    def send_resume(self, conv_id: str) -> SendOutcome:
        """发送附件简历。

        前置校验（R12 实测）：HR 未回复的会话无法发送——不满足时返回
        ``FAILED``（附明确原因，不抛异常、不重试）[A5]。
        """

    @abstractmethod
    def list_conversations(self) -> list[Conversation]:
        """列出会话（按最近消息时间降序）。"""

    @abstractmethod
    def get_messages(self, conv_id: str, since: datetime | None) -> list[Message]:
        """取会话消息（``since`` 为 None 取全部；否则取 ``sent_at > since``）。"""

    @abstractmethod
    def get_conversation_job(self, conv_id: str) -> JobRef | None:
        """取会话关联岗位 [B8]；取不到返回 None。"""

    @abstractmethod
    def snapshot_conversation_baseline(self) -> int:
        """返回当前会话总数作为基线 [B3]（调用方据此把既有会话标记为基线外不自动化）。"""


# ---- 注册表（§2：create_adapter(site_id, ...) 装配；Mock 与 Zhipin 同机制）----

_REGISTRY: dict[str, type[SiteAdapter]] = {}


def register_adapter(site_id: str, cls: type[SiteAdapter]) -> None:
    """注册适配器类（具体适配器模块在导入时调用；幂等：同类重复注册无副作用）。

    - ``site_id`` 必须与 ``cls.site_id`` 一致（防注册名与实现名漂移）；
    - 已被**其它类**占用的 site_id 拒绝覆盖（防导入顺序意外顶替）。
    """
    if not site_id or not site_id.strip():
        raise ValueError("site_id 不能为空")
    if not (isinstance(cls, type) and issubclass(cls, SiteAdapter)):
        raise TypeError(f"cls 必须是 SiteAdapter 的子类，收到 {cls!r}")
    if cls.site_id != site_id:
        raise ValueError(
            f"site_id 不一致：注册名 {site_id!r} != {cls.__name__}.site_id={cls.site_id!r}"
        )
    existing = _REGISTRY.get(site_id)
    if existing is not None and existing is not cls:
        raise ValueError(
            f"site_id {site_id!r} 已注册为 {existing.__name__}，拒绝覆盖为 {cls.__name__}"
        )
    _REGISTRY[site_id] = cls


def create_adapter(site_id: str, *args: Any, **kwargs: Any) -> SiteAdapter:
    """按 site_id 装配适配器实例（位置/关键字参数原样转发给实现类构造器）。

    典型调用（§2 依赖规则：L3 只经本函数装配，不 import 具体实现）::

        create_adapter("zhipin", session, config)          # S6 交付后
        create_adapter("mock", fixtures_dir, scenario=scn) # §12 模拟环境

    未注册的 site_id 抛 :class:`UnknownAdapterError`。
    """
    cls = _REGISTRY.get(site_id)
    if cls is None:
        raise UnknownAdapterError(
            f"未注册的 site_id: {site_id!r}（已注册: {registered_adapters()}）"
        )
    return cls(*args, **kwargs)


def registered_adapters() -> list[str]:
    """已注册 site_id 列表（诊断/控制台展示用）。"""
    return sorted(_REGISTRY)
