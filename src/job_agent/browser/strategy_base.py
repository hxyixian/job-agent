"""浏览器会话策略接口（设计文档 §5.3 [G4 修订]，S4b 交付）。

``BrowserSession`` 暴露 **Patchright 同步 Page 级原语**（goto/wait_for/
click/query/scroll/screenshot）+ persistent profile 生命周期 + 双标签页模型。
仅 S1b（Patchright，:mod:`job_agent.browser.patchright_session`）一个实现；
扩展路线（M2 第 3 顺位，默认不开发）将作为独立 SiteAdapter 注册、不经
BrowserSession [G4/M5，F11 清理]。

分层约定（S6a/S6b 必读）：
- 本层只做"驱动 + 健康检测 + 生命周期"，**不含任何站点语义**（选择器/URL
  由 adapter 传入）；
- 风控信号（EVICTED/SECURITY_CHECK [G2/H1]）由本层检测并经
  :class:`BrowserRiskError` 上抛，adapter 映射为
  ``RiskDetectedError(signal)``（adapters/base.py §5.2 异常树），编排器再走
  M5 处置（core/rampup.handle_risk_signal）；本层**自发事件仅限浏览器生命
  周期**（BROWSER_LOST/CHROME_UPGRADED），不重复发 PAGE_EVICTED/
  SECURITY_CHECK——单一来源，防双报；
- 本层没有任何"自动重新导航"代码路径（§5.3 驱逐/拦截后禁止自动重导航，
  可 grep 复核）；风控挂起后所有自动化原语立即拒绝执行。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from job_agent.models import RiskSignal

__all__ = [
    "CLICK_TIMEOUT_MS",
    "DEFAULT_TIMEOUT_MS",
    "BrowserClosedError",
    "BrowserNotRunningError",
    "BrowserRiskError",
    "BrowserSession",
    "BrowserSessionError",
    "BrowserSetupError",
    "BrowserTimeoutError",
    "LoginWindowOpenError",
    "TabName",
]

#: 默认操作超时（毫秒）——两轮 X4 实测采用的 30s 基线
DEFAULT_TIMEOUT_MS = 30000

#: 点击类操作超时（毫秒）——元素不在即返回 False，不抛异常
CLICK_TIMEOUT_MS = 5000

#: 双标签页模型 [Q22]：会话页（常驻，轮询只读 DOM 不导航）与工作页（搜索/详情）
TabName = Literal["session", "work"]


# ---- 异常树（浏览器层独立于 §5.2 适配器异常树；映射关系见模块 docstring）----


class BrowserSessionError(Exception):
    """浏览器会话层错误基类。"""


class BrowserSetupError(BrowserSessionError):
    """启动/装配失败（驱动缺失、profile 被占用、可执行文件未找到等）。"""


class BrowserNotRunningError(BrowserSessionError):
    """会话未启动（或已关闭）时调用自动化原语。"""


class BrowserClosedError(BrowserSessionError):
    """底层目标（page/context/browser）已关闭（TargetClosedError 族）。

    抛出前会话已按 §5.3 尝试自动重建（限 1 次/小时；关闭前出现过
    EVICTED/SECURITY_CHECK 则不重建）——调用方以 ``session.running``
    区分"已重建可重试"与"等待人工重新拉起"。
    """


class BrowserTimeoutError(BrowserSessionError):
    """驱动操作超时（goto 之外的超时类错误；click/wait 类有各自的返回值语义）。"""


class BrowserRiskError(BrowserSessionError):
    """检测到风控信号 [G2/H1]——适配器应映射为 ``RiskDetectedError(signal)``。

    携带 :class:`~job_agent.models.RiskSignal`（EVICTED/SECURITY_CHECK）。
    抛出后会话进入风控挂起：所有自动化原语拒绝执行，直到人工经
    :meth:`BrowserSession.start` 重新拉起（M5：无自动恢复）。
    """

    def __init__(self, signal: RiskSignal, detail: str = "") -> None:
        self.signal = signal
        self.detail = detail
        super().__init__(f"browser risk signal {signal.name}" + (f": {detail}" if detail else ""))


class LoginWindowOpenError(BrowserSessionError):
    """登录窗口存活期间引擎启动被拒绝（同一 profile 不能双进程占用）[M3/G5]。"""


class BrowserSession(ABC):
    """浏览器会话策略接口（§5.3）。

    实现约束：
    - 全同步（引擎单线程串行调用，Patchright 同步 API 匹配 [B5]）；
    - persistent profile 管理：一个实例独占一个 ``user_data_dir``；
    - 双标签页模型 [Q22]：``session`` 页常驻（**不允许 close_tab**，轮询只读
      DOM 不导航），``work`` 页承载搜索/详情导航、可随时关闭重开；
    - 健康检测三形态（§5.3 [G2/H1]）：URL 变 ``about:blank`` 或非我方发起的
      站外跳转 → EVICTED；URL 含 ``_security_check``/验证类路径、或页面出现
      "安全验证/环境异常/验证码"类文字 → SECURITY_CHECK；导航后停留页非目标
      页**连续 2 次** → SECURITY_CHECK；
    - 检测粒度 2s：由 :meth:`watch`（观察窗轮询）与 :meth:`wait_for_selector`
      （分片等待）承担；
    - ``TargetClosedError`` 后自动重建**最多 1 次/小时**；关闭前出现过
      EVICTED/SECURITY_CHECK 则**不重建**、直接暂停等人工处理（控制台
      "重新拉起浏览器"按钮 = 再次 :meth:`start`）。
    """

    # ---- profile 生命周期 ----

    @abstractmethod
    def start(self) -> None:
        """启动浏览器（persistent context）。幂等：已运行时无操作。

        - 登录窗口存活期间拒绝启动（:class:`LoginWindowOpenError`，
          提示"请先关闭登录窗口"）[M3/G5]；
        - 人工重新拉起（含风控/重建拒绝后的恢复）同走本方法：成功后
          清除风控挂起与"等待人工"标志。
        """

    @abstractmethod
    def shutdown(self) -> None:
        """关闭浏览器与驱动连接（幂等）。"""

    @property
    @abstractmethod
    def running(self) -> bool:
        """浏览器是否处于运行态（启动成功且未关闭/未拒绝重建）。"""

    @property
    @abstractmethod
    def awaiting_manual_relaunch(self) -> bool:
        """自动重建被拒（限流/拦截前置/重建失败）后等待人工重新拉起。"""

    # ---- 双标签页模型 [Q22] ----

    @abstractmethod
    def ensure_session_tab(self, url: str) -> None:
        """确保会话页存在并定位到 url（首次创建允许一次导航），切换为当前页。

        会话页常驻：创建后不再关闭、轮询只读 DOM 不导航 [Q22]。
        """

    @abstractmethod
    def switch_tab(self, name: TabName) -> None:
        """切换当前标签页（后续原语作用于此页）。

        ``work`` 缺失时惰性创建空页；``session`` 未初始化时先调
        :meth:`ensure_session_tab`。
        """

    @abstractmethod
    def close_tab(self, name: TabName) -> None:
        """关闭标签页。仅 ``work`` 可关（``session`` 常驻 [Q22]，传入即拒绝）。"""

    # ---- Page 级原语（作用于当前标签页）----

    @abstractmethod
    def goto(
        self,
        url: str,
        *,
        expect_url_prefix: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> str:
        """导航到 url，返回实际停留页 URL。

        - ``expect_url_prefix``：导航落点校验前缀（缺省 = 目标 URL 去
          query/fragment）；落点不匹配记一次"非目标页"，**连续 2 次**
          → SECURITY_CHECK [H1]，匹配则计数归零；
        - 导航后立即做一次健康检查（EVICTED/URL/文字三形态）；
        - goto 超时按 X4 协议容忍（不抛），继续以当前 URL 落点校验；
        - 风控挂起期间调用立即抛 :class:`BrowserRiskError`（禁止自动重导航）。
        """

    @abstractmethod
    def wait_for_selector(self, selector: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
        """等待选择器可见。返回是否在超时内等到（超时返回 False，不抛错）。

        等待按 2s 粒度分片，每片前做一次健康检查（发现风控信号立即抛
        :class:`BrowserRiskError`）。
        """

    @abstractmethod
    def click(self, selector: str, *, timeout_ms: int = CLICK_TIMEOUT_MS) -> bool:
        """点击首个匹配元素。超时未命中返回 False（不抛错）；点击成功后追加
        一次健康检查（点击触发安全拦截跳转时抛 :class:`BrowserRiskError`）。"""

    @abstractmethod
    def query_text(self, selector: str) -> str | None:
        """取首个匹配元素的可见文本（``inner_text``）；无匹配返回 None。"""

    @abstractmethod
    def query_attrs(self, selector: str, attrs: Sequence[str]) -> list[dict[str, str | None]]:
        """取全部匹配元素的目标属性（元素顺序保持 DOM 序）。

        供列表页批量提取（如职位卡 href/ka 属性）——纯 locator 循环，
        不向页面注入任何脚本。
        """

    @abstractmethod
    def scroll(self, *, dy: int = 600) -> None:
        """鼠标滚轮向下滚动 dy 像素（拟人滚动；负值向上）。"""

    @abstractmethod
    def screenshot(self, path: str | Path) -> None:
        """当前页截图存至 path（风控证据/故障留档）。"""

    @abstractmethod
    def get_url(self) -> str:
        """当前标签页 URL（诊断友好：风控挂起期间仍可读）。"""

    # ---- 健康检测 [G2/H1] ----

    @abstractmethod
    def check_health(self) -> RiskSignal:
        """对当前页做一次即时健康检查（URL 三形态 + 验证文字探测）。

        只探测不抛错（信号经返回值给出）；EVICTED/SECURITY_CHECK 会
        写入会话风控挂起标志。无风控时返回 ``RiskSignal.NONE``。
        """

    @abstractmethod
    def watch(self, seconds: float, *, interval_s: float | None = None) -> RiskSignal:
        """观察窗：默认 2s 粒度轮询健康检查（§5.3 检测粒度）。

        拟人延时（8-20s 节流）期间由本方法承担驱逐/拦截监测：发现风控
        信号立即返回该信号，否则观察满 ``seconds`` 返回 NONE。
        """

    @property
    @abstractmethod
    def last_signal(self) -> RiskSignal:
        """最近一次健康检查的信号（观测用；挂起标志以异常语义为准）。"""

    # ---- 登录模式 [M3/G5] ----

    @abstractmethod
    def launch_login_window(self) -> None:
        """以普通浏览器进程打开登录窗口（零自动化参数：无调试端口/管道）。

        用户扫码登录并**关闭窗口后**引擎方可启动（:meth:`start` 在窗口
        存活期间拒绝）；引擎运行中调用本方法同样拒绝（同一 profile 不能
        双进程占用）。幂等：窗口已存活时无操作。
        """

    @abstractmethod
    def login_window_alive(self) -> bool:
        """登录窗口是否存活（进程退出即视为已关闭，并回收句柄）。"""

    # ---- 便捷协议 ----

    def __enter__(self) -> BrowserSession:
        """``with`` 装配：进入即 start（登录窗口存活时抛错）。"""
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.shutdown()
