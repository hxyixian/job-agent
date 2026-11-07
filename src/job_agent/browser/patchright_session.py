"""S1b 实现：Patchright 浏览器会话（设计文档 §5.3 正式定型路线，S4b 交付）。

2026-11-07 第二轮 X4 按产品形态通过（60 分钟零驱逐零拦截）后的定型实现；
契约逐条对应 §5.3：

- **启动形态 [V4/M4]**：``patchright==1.64.4``（Playwright drop-in），
  ``launch_persistent_context(channel=…, user_data_dir=.browser_profile,
  headless=False, no_viewport=True)``——**Patchright 默认启动参数**，不显式
  传 ``args``/``ignore_default_args``，不加自定义 headers/UA、无注入
  （本模块零 ``add_init_script``/``evaluate`` 调用，可 grep 复核）；
- **Chrome 版本记录 [G4]**：启动时把 ``ctx.browser.version`` 写日志；
- **主版本联动降档 [V9]**：与上次记录的主版本比较，变化 → 写
  ``run_state.chrome_downgrade={"active": true, "since", "chrome_major"}``
  （键常量复用 :data:`job_agent.core.rampup.KEY_CHROME_DOWNGRADE`，由
  ``rampup.browse_tier`` 折算第 1 档 15/时·60/天、运行 30 分钟无风控信号
  自动恢复配置值）+ CHROME_UPGRADED 中级事件；
- **EVICTED/SECURITY_CHECK 检测 [G2/H1]**：URL 轮询 2s 粒度
  （:meth:`watch` / :meth:`wait_for_selector` 分片）+ 安全验证文字探测 +
  导航落点校验（连续 2 次非目标页）；检出即挂起会话并抛
  :class:`BrowserRiskError`，**无任何自动重导航路径**；
- **自动重建 [A-CORE-25]**：TargetClosedError 后重建**最多 1 次/小时**；
  关闭前出现过 EVICTED/SECURITY_CHECK 则**不重建**；被拒 → BROWSER_LOST
  高级事件 + 等待人工重新拉起（:meth:`start` 即控制台"重新拉起浏览器"）；
- **登录模式 [M3/G5]**：:meth:`launch_login_window` 以
  ``chrome.exe --user-data-dir=<profile>`` 普通进程启动（**零自动化参数**：
  无调试端口/管道），窗口存活期间 :meth:`start` 拒绝（提示"请先关闭登录
  窗口"）；FR1.3 登录失效恢复同走此模式。

事件边界（防双报）：本层只发 BROWSER_LOST / CHROME_UPGRADED 两个生命
周期事件；PAGE_EVICTED/SECURITY_CHECK 由编排器经
``core.rampup.handle_risk_signal`` 发（adapter 把 :class:`BrowserRiskError`
映射为 ``RiskDetectedError``）。

依赖注入（测试口径，S4b 单测全 mock page、不启真实浏览器）：
``pw_provider``（驱动入口）、``process_launcher``/``chrome_executable_resolver``
（登录窗口进程）、``clock``（重建限流/降档时间）均可替换。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

# S1b 定型依赖：patchright 1.64.4（pyproject 锁定）。异常类在
# patchright._impl._errors 定义；sync_api 只再导出了 Error/TimeoutError，
# TargetClosedError 须自 _impl 导入（版本已锁，路径稳定）。
from patchright._impl._errors import (
    Error as PatchrightError,
)
from patchright._impl._errors import (
    TargetClosedError,
)
from patchright._impl._errors import (
    TimeoutError as PatchrightTimeout,
)
from patchright.sync_api import sync_playwright

from job_agent.config import Config, resolve_path
from job_agent.core import events, runstate
from job_agent.core.rampup import KEY_CHROME_DOWNGRADE
from job_agent.db import Database
from job_agent.models import RiskSignal
from job_agent.timeutil import utc_str

from .strategy_base import (
    CLICK_TIMEOUT_MS,
    DEFAULT_TIMEOUT_MS,
    BrowserClosedError,
    BrowserNotRunningError,
    BrowserRiskError,
    BrowserSession,
    BrowserSessionError,
    BrowserSetupError,
    BrowserTimeoutError,
    LoginWindowOpenError,
    TabName,
)

__all__ = [
    "AUTO_REBUILD_MIN_INTERVAL",
    "BAD_LANDING_LIMIT",
    "DEFAULT_PROFILE_DIR",
    "DEFAULT_RISK_URL_PATTERN",
    "DEFAULT_RISK_WORDS_PATTERN",
    "POLL_INTERVAL_S",
    "PatchrightSession",
]

logger = logging.getLogger("job_agent.browser")

#: EVICTED/SECURITY_CHECK 轮询检测粒度（秒，§5.3）
POLL_INTERVAL_S = 2.0

#: 自动重建最小间隔（§5.3：最多 1 次/小时）
AUTO_REBUILD_MIN_INTERVAL = timedelta(hours=1)

#: 导航落点校验阈值：连续 2 次非目标页 → SECURITY_CHECK [H1]
BAD_LANDING_LIMIT = 2

#: persistent profile 默认目录（§5.3 字面值，基于应用根解析 [R6-2]）
DEFAULT_PROFILE_DIR = ".browser_profile"

#: 拦截 URL 形态（第二轮 X4 验证口径：_security_check / verify 类路径）
DEFAULT_RISK_URL_PATTERN = r"_security_check|verify"

#: 拦截文字形态（X4 验证口径：安全验证/环境异常/验证码/访问异常）
DEFAULT_RISK_WORDS_PATTERN = r"安全验证|环境异常|验证码|访问异常"

#: 文字探测只扫描正文前 N 字符（X4 口径 body[:4000]）
_TEXT_PROBE_CHARS = 4000
#: 文字/单元素探测超时（毫秒）
_PROBE_TIMEOUT_MS = 3000

#: run_state 键：Chrome 版本记录（{"major", "full", "recorded_at"}）
KEY_CHROME_VERSION = "chrome_version"

T = TypeVar("T")

_PopenLike = Any  # subprocess.Popen 或测试替身（poll() → int | None）
_PwContextManager = Any  # sync_playwright() 返回的上下文管理器


def _default_chrome_executable(channel: str) -> str:
    """按 channel 定位本机浏览器可执行文件（登录窗口用，M3/G5）。

    先 PATH 后标准安装目录；找不到返回空串（调用方转
    :class:`BrowserSetupError`）。
    """
    if channel == "msedge":
        exe_names = ("msedge", "msedge.exe")
        install_subdir = os.path.join("Microsoft", "Edge", "Application")
    else:
        exe_names = ("chrome", "chrome.exe")
        install_subdir = os.path.join("Google", "Chrome", "Application")
    for name in exe_names:
        found = shutil.which(name)
        if found:
            return found
    for base in (
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ):
        if not base:
            continue
        candidate = os.path.join(base, install_subdir, f"{channel}.exe")
        if Path(candidate).is_file():
            return candidate
    return ""


class PatchrightSession(BrowserSession):
    """S1b 浏览器会话（§5.3 逐条实现，规格对应关系见模块 docstring）。"""

    def __init__(
        self,
        config: Config,
        db: Database | None = None,
        *,
        profile_dir: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
        pw_provider: Callable[[], _PwContextManager] | None = None,
        process_launcher: Callable[[list[str]], _PopenLike] | None = None,
        chrome_executable_resolver: Callable[[str], str] | None = None,
        site_host: str = "zhipin.com",
        risk_url_pattern: str = DEFAULT_RISK_URL_PATTERN,
        risk_words_pattern: str = DEFAULT_RISK_WORDS_PATTERN,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        """``db`` 为空时降级为"仅日志"（V9 降档/事件均需要 db 落库）。

        ``profile_dir`` 缺省 = 应用根下 ``.browser_profile`` [R6-2]；
        ``clock`` 缺省取当前 UTC（注入以测重建限流/降档时间窗）。
        """
        self._config = config
        self._db = db
        self._channel = config.site.channel
        self._headless = config.site.headless
        self._profile_dir = Path(resolve_path(profile_dir or DEFAULT_PROFILE_DIR))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pw_provider = pw_provider or sync_playwright
        self._process_launcher = process_launcher or (lambda args: subprocess.Popen(args))
        self._chrome_executable_resolver = chrome_executable_resolver or _default_chrome_executable
        self._site_host = site_host
        self._risk_url_re = re.compile(risk_url_pattern, re.IGNORECASE)
        self._risk_words_re = re.compile(risk_words_pattern)
        self._poll_interval_s = poll_interval_s

        # 运行态
        self._pw_cm: _PwContextManager | None = None
        self._ctx: Any = None
        self._tabs: dict[str, Any] = {}
        self._current: str = "work"
        self._running = False
        self._awaiting_manual = False

        # 风控/落点/重建状态
        self._risk_pending: RiskSignal | None = None
        self._last_signal = RiskSignal.NONE
        self._bad_landings = 0
        self._last_auto_rebuild_at: datetime | None = None

        # 登录窗口进程句柄
        self._login_proc: _PopenLike | None = None

    # ---- profile 生命周期 ----

    def start(self) -> None:
        """启动浏览器（§5.3 Best Practice 参数；幂等；登录窗口存活即拒绝）。"""
        if self._running:
            return
        if self.login_window_alive():
            raise LoginWindowOpenError(
                "登录窗口打开中：同一浏览器 profile 不能被双进程占用，请先关闭登录窗口 [M3/G5]"
            )
        self._launch(mark_manual=True)

    def shutdown(self) -> None:
        """关闭浏览器与驱动连接（幂等；不清除风控挂起——那是 start 的事）。"""
        ctx, cm = self._ctx, self._pw_cm
        self._ctx = None
        self._pw_cm = None
        self._tabs = {}
        self._running = False
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                logger.warning("浏览器 context 关闭异常（忽略）", exc_info=True)
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                logger.warning("patchright 驱动连接退出异常（忽略）", exc_info=True)

    @property
    def running(self) -> bool:
        return self._running

    @property
    def awaiting_manual_relaunch(self) -> bool:
        return self._awaiting_manual

    # ---- 双标签页模型 [Q22] ----

    def ensure_session_tab(self, url: str) -> None:
        """会话页常驻：缺失则创建并首次导航到 url，随后切换为当前页。

        创建后本会话不再对其发起导航（轮询只读 DOM [Q22]）；后续
        ``close_tab("session")`` 被拒绝、重建后需调用方重新 ensure。
        """
        self._ensure_running()
        self._raise_if_risk_pending()
        page = self._tabs.get("session")
        if page is None:
            page = self._acquire_page()
            self._tabs["session"] = page
            self._goto_on(page, url, expect_url_prefix=None)
        self._current = "session"

    def switch_tab(self, name: TabName) -> None:
        """切换当前标签页；work 缺失时惰性创建，session 未初始化时拒绝。"""
        self._ensure_running()
        if name == "work":
            self._tabs.setdefault("work", self._acquire_page())
        elif name == "session":
            if self._tabs.get("session") is None:
                raise BrowserSessionError("会话页尚未初始化：先 ensure_session_tab(url) [Q22]")
        else:
            raise BrowserSessionError(f"未知标签页 {name!r}（session | work）")
        self._current = name

    def close_tab(self, name: TabName) -> None:
        """关闭标签页——仅 work 可关；session 常驻 [Q22]。"""
        self._ensure_running()
        if name == "session":
            raise BrowserSessionError("会话页常驻 [Q22]，不允许关闭（轮询只读 DOM 不导航）")
        page = self._tabs.pop(name, None)
        if page is not None:
            try:
                page.close()
            except Exception:
                logger.warning("work 标签页关闭异常（忽略）", exc_info=True)
            if self._current == name:
                self._current = "session" if self._tabs.get("session") is not None else "work"

    # ---- Page 级原语 ----

    def goto(
        self,
        url: str,
        *,
        expect_url_prefix: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> str:
        """导航当前标签页（落点校验 + 三形态健康检查，见接口 docstring）。"""
        self._raise_if_risk_pending()
        return self._page_call(lambda page: self._goto_on(page, url, expect_url_prefix, timeout_ms))

    def wait_for_selector(self, selector: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
        """2s 分片等待选择器可见；每片前健康检查（风控即抛）。超时返回 False。"""
        self._ensure_running()
        self._raise_if_risk_pending()
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            signal = self.check_health()
            if signal != RiskSignal.NONE:
                raise BrowserRiskError(signal, f"wait_for_selector({selector!r}) 等待期间检出")
            page = self._current_page()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            slice_ms = int(min(self._poll_interval_s * 1000.0, remaining * 1000.0))
            try:
                page.wait_for_selector(selector, timeout=slice_ms)
                return True
            except PatchrightTimeout:
                continue
            except TargetClosedError:
                self._handle_target_closed()
                raise BrowserClosedError(self._closed_message()) from None
            except PatchrightError as exc:
                raise BrowserSessionError(f"wait_for_selector 失败: {exc}") from exc

    def click(self, selector: str, *, timeout_ms: int = CLICK_TIMEOUT_MS) -> bool:
        """点击首个匹配元素（超时 False；点击后追加一次健康检查）。"""
        try:
            self._page_call(lambda page: page.locator(selector).click(timeout=timeout_ms))
        except BrowserTimeoutError:
            return False
        signal = self.check_health()
        if signal != RiskSignal.NONE:
            raise BrowserRiskError(signal, f"click({selector!r}) 后检出")
        return True

    def query_text(self, selector: str) -> str | None:
        """取首个匹配元素 inner_text；无匹配（超时）返回 None。"""
        try:
            return self._page_call(
                lambda page: str(page.locator(selector).first.inner_text(timeout=_PROBE_TIMEOUT_MS))
            )
        except BrowserTimeoutError:
            return None

    def query_attrs(self, selector: str, attrs: Sequence[str]) -> list[dict[str, str | None]]:
        """批量取匹配元素属性（locator 循环，零脚本注入）。"""
        attr_list = list(attrs)

        def _collect(page: Any) -> list[dict[str, str | None]]:
            locator = page.locator(selector)
            count = locator.count()
            return [{a: locator.nth(i).get_attribute(a) for a in attr_list} for i in range(count)]

        return self._page_call(_collect)

    def scroll(self, *, dy: int = 600) -> None:
        """鼠标滚轮滚动（拟人）。"""
        self._page_call(lambda page: page.mouse.wheel(0, dy))

    def screenshot(self, path: str | Path) -> None:
        """当前页截图（证据留档）。"""
        self._page_call(lambda page: page.screenshot(path=str(path)))

    def get_url(self) -> str:
        """当前标签页 URL（诊断友好：风控挂起期间仍可读）。"""
        self._ensure_running()
        page = self._current_page()
        try:
            return str(page.url)
        except TargetClosedError:
            self._handle_target_closed()
            raise BrowserClosedError(self._closed_message()) from None
        except Exception as exc:
            raise BrowserSessionError(f"读取页面 URL 失败: {exc}") from exc

    # ---- 健康检测 [G2/H1] ----

    def check_health(self) -> RiskSignal:
        """对当前页做一次即时健康检查（URL 三形态 + 验证文字探测）。

        只探测不抛错；EVICTED/SECURITY_CHECK 写入挂起标志（此后所有自动化
        原语拒绝执行，直到人工 start()）。判定逻辑见 :meth:`_health_of`。
        """
        return self._health_of(self._current_page())

    def watch(self, seconds: float, *, interval_s: float | None = None) -> RiskSignal:
        """观察窗：默认 2s 粒度轮询健康检查（§5.3 检测粒度承担方）。"""
        interval = self._poll_interval_s if interval_s is None else interval_s
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            signal = self.check_health()
            if signal != RiskSignal.NONE:
                return signal
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return RiskSignal.NONE
            time.sleep(min(interval, remaining))

    @property
    def last_signal(self) -> RiskSignal:
        return self._last_signal

    # ---- 登录模式 [M3/G5] ----

    def launch_login_window(self) -> None:
        """普通浏览器进程打开登录窗口（零自动化参数）；引擎运行中拒绝。"""
        if self._running:
            raise BrowserSessionError(
                "引擎浏览器运行中：请先停止引擎再进入登录模式（同一 profile 不能双进程占用）"
            )
        if self.login_window_alive():
            return  # 幂等：窗口已打开
        executable = self._chrome_executable_resolver(self._channel)
        if not executable:
            raise BrowserSetupError(f"未找到 {self._channel} 可执行文件（登录模式需要本机浏览器）")
        # 零自动化参数 [M3/G5]：仅 user-data-dir；无 --remote-debugging-port/管道/自动化开关
        args = [executable, f"--user-data-dir={self._profile_dir}"]
        self._login_proc = self._process_launcher(args)
        logger.info(
            "登录窗口已启动（普通浏览器进程，零自动化参数）：%s --user-data-dir=%s；"
            "用户扫码登录并关闭窗口后引擎方可启动",
            executable,
            self._profile_dir,
        )

    def login_window_alive(self) -> bool:
        """登录窗口是否存活（进程退出即视为已关闭，并回收句柄）。"""
        proc = self._login_proc
        if proc is None:
            return False
        if proc.poll() is not None:
            self._login_proc = None  # 窗口已关闭：回收句柄
            return False
        return True

    # ---- 内部：启动/重建 ----

    def _launch(self, *, mark_manual: bool) -> None:
        """按 §5.3 Best Practice 启动 persistent context 并初始化会话状态。

        ``mark_manual=True``（人工 start）：清除风控挂起与等待人工标志；
        自动重建路径不会带着挂起标志进来（挂起即拒绝重建）。
        """
        cm = self._pw_provider()
        try:
            pw = cm.__enter__()
            # [V4] Patchright 默认启动参数：仅以下 4 项，无 args/ignore_default_args/
            # extra_http_headers/user_agent（第二轮 X4 定型形态，逐字对应 §5.3）
            ctx = pw.chromium.launch_persistent_context(
                channel=self._channel,
                user_data_dir=str(self._profile_dir),
                headless=self._headless,
                no_viewport=True,
            )
        except Exception as exc:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                logger.warning("驱动连接清理异常（忽略）", exc_info=True)
            raise BrowserSetupError(f"浏览器启动失败（channel={self._channel}）: {exc}") from exc

        if mark_manual:
            self._risk_pending = None
            self._awaiting_manual = False
        self._ctx = ctx
        self._pw_cm = cm
        self._tabs = {}
        self._current = "work"
        self._running = True
        self._bad_landings = 0
        self._last_signal = RiskSignal.NONE
        self._record_chrome_version()

    def _handle_target_closed(self) -> None:
        """TargetClosedError 统一处置：拆连接 → 按限流/拦截规则尝试自动重建。"""
        logger.warning("浏览器目标已关闭（TargetClosedError），尝试自动重建（限 1 次/小时）")
        self.shutdown()  # 幂等拆连接（保留风控挂起/重建时间戳）
        self._try_auto_rebuild()

    def _try_auto_rebuild(self) -> bool:
        """自动重建（§5.3/A-CORE-25）：拦截前置不重建 + 1 次/小时限流。"""
        now = self._clock()
        if self._risk_pending is not None:
            self._refuse_rebuild(
                now,
                reason="risk_preceded",
                extra={"signal": self._risk_pending.name},
            )
            return False
        last = self._last_auto_rebuild_at
        if last is not None and (now - last) < AUTO_REBUILD_MIN_INTERVAL:
            self._refuse_rebuild(
                now,
                reason="throttled",
                extra={"last_auto_rebuild_at": utc_str(last)},
            )
            return False
        self._last_auto_rebuild_at = now
        try:
            self._launch(mark_manual=False)
        except BrowserSetupError as exc:
            self._refuse_rebuild(now, reason="launch_failed", extra={"error": str(exc)})
            return False
        logger.info("浏览器自动重建成功（标签页已重置，由调用方重新初始化会话页/导航）")
        return True

    def _refuse_rebuild(self, now: datetime, *, reason: str, extra: dict[str, Any]) -> None:
        """拒绝自动重建：BROWSER_LOST 高级事件 + 等待人工重新拉起。"""
        self._awaiting_manual = True
        logger.warning(
            "自动重建被拒（%s）：暂停并等待人工重新拉起浏览器（控制台『重新拉起浏览器』）",
            reason,
        )
        if self._db is not None:
            events.emit(
                self._db,
                "BROWSER_LOST",
                {
                    "reason": reason,
                    "at": utc_str(now),
                    "action": "pause_awaiting_manual_relaunch",
                    **extra,
                },
            )

    # ---- 内部：Chrome 版本记录与 V9 降档联动 ----

    def _record_chrome_version(self) -> None:
        """记录 Chrome 版本 [G4]；主版本变化 → 降档协议 + CHROME_UPGRADED [V9]。"""
        browser = getattr(self._ctx, "browser", None)
        version: str | None = None
        if browser is not None:
            try:
                version = str(browser.version)
            except Exception:
                version = None
        if not version:
            logger.warning("未能读取 Chrome 版本（G4 版本记录缺失）")
            return
        try:
            major = int(version.split(".")[0])
        except ValueError:
            logger.warning("Chrome 版本号无法解析主版本：%r", version)
            return
        logger.info("Chrome 版本: %s [G4 版本记录]", version)
        if self._db is None:
            return
        prev = runstate.get_state(self._db, KEY_CHROME_VERSION)
        prev_major = prev.get("major") if isinstance(prev, dict) else None
        now = self._clock()
        runstate.set_state(
            self._db,
            KEY_CHROME_VERSION,
            {"major": major, "full": version, "recorded_at": utc_str(now)},
        )
        if isinstance(prev_major, int) and prev_major != major:
            runstate.set_state(
                self._db,
                KEY_CHROME_DOWNGRADE,
                {"active": True, "since": utc_str(now), "chrome_major": major},
            )
            events.emit(
                self._db,
                "CHROME_UPGRADED",
                {
                    "from_major": prev_major,
                    "to_major": major,
                    "full": version,
                    "downgrade": "tier1_browse_15h_60d_recover_30min",
                },
            )
            logger.warning(
                "Chrome 主版本变化 %s → %s：浏览上限联动降至试运行第 1 档"
                "（15/时·60/天），运行 30 分钟无风控信号自动恢复配置值 [V9]",
                prev_major,
                major,
            )

    # ---- 内部：页面访问与导航 ----

    def _ensure_running(self) -> None:
        if self._awaiting_manual:
            raise BrowserClosedError(self._closed_message())
        if not self._running:
            raise BrowserNotRunningError("浏览器会话未启动（先 start()）")

    def _raise_if_risk_pending(self) -> None:
        """风控挂起即拒绝自动化原语（含导航——禁止自动重导航的会话侧防线）。"""
        if self._risk_pending is not None:
            raise BrowserRiskError(
                self._risk_pending,
                "风控信号挂起中，会话已冻结等待人工处理（M5：无自动恢复）；"
                "处理完成后经 start() 重新拉起",
            )

    def _closed_message(self) -> str:
        if self._awaiting_manual:
            return "浏览器已关闭且自动重建被拒，等待人工重新拉起（控制台『重新拉起浏览器』）"
        return "浏览器目标已关闭（TargetClosedError）"

    def _current_page(self) -> Any:
        """取当前标签页（work 缺失时惰性创建；风控挂起由各原语自行先检）。"""
        self._ensure_running()
        page = self._tabs.get(self._current)
        if page is None:
            if self._current != "work":
                raise BrowserSessionError("会话页尚未初始化：先 ensure_session_tab(url) [Q22]")
            page = self._tabs["work"] = self._acquire_page()
        return page

    def _acquire_page(self) -> Any:
        """复用 context 已有页面（如启动时的初始页），否则新建。"""
        assert self._ctx is not None  # 调用前均已 _ensure_running
        existing = [p for p in self._ctx.pages if p not in self._tabs.values()]
        if existing:
            return existing[0]
        return self._ctx.new_page()

    def _page_call(self, fn: Callable[[Any], T]) -> T:
        """单次页面操作统一包装：运行态/风控前置检查 + 异常归一化。"""
        self._ensure_running()
        self._raise_if_risk_pending()
        page = self._current_page()
        try:
            return fn(page)
        except TargetClosedError:
            self._handle_target_closed()
            raise BrowserClosedError(self._closed_message()) from None
        except PatchrightTimeout as exc:
            raise BrowserTimeoutError(str(exc)) from exc
        except PatchrightError as exc:
            raise BrowserSessionError(f"页面操作失败: {exc}") from exc

    def _goto_on(
        self,
        page: Any,
        url: str,
        expect_url_prefix: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> str:
        """导航 + 落点校验 + 健康检查（goto/ensure_session_tab 共用）。

        goto 超时容忍（X4 协议：超时不抛，以实际落点继续判定）；落点不匹配
        记一次"非目标页"，连续 2 次 → SECURITY_CHECK [H1]。
        """
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PatchrightTimeout:
            logger.warning("goto 超时（容忍，以实际落点继续判定）: %s", url)
        except TargetClosedError:
            self._handle_target_closed()
            raise BrowserClosedError(self._closed_message()) from None
        except PatchrightError as exc:
            raise BrowserSessionError(f"goto 失败: {exc}") from exc

        signal = self._health_of(page)
        if signal != RiskSignal.NONE:
            raise BrowserRiskError(signal, f"goto {url} 后检出风控信号")
        landing = str(page.url)
        prefix = expect_url_prefix or _default_prefix(url)
        if not landing.startswith(prefix):
            self._bad_landings += 1
            logger.warning(
                "导航落点非目标页（第 %d/%d 次）: 目标 %s → 停留 %s",
                self._bad_landings,
                BAD_LANDING_LIMIT,
                url,
                landing,
            )
            if self._bad_landings >= BAD_LANDING_LIMIT:
                self._mark_risk(
                    RiskSignal.SECURITY_CHECK, f"导航后停留页非目标页连续 {self._bad_landings} 次"
                )
                raise BrowserRiskError(
                    RiskSignal.SECURITY_CHECK,
                    f"导航落点连续 {BAD_LANDING_LIMIT} 次非目标页（目标 {url}，停留 {landing}）",
                )
        else:
            self._bad_landings = 0
        return landing

    def _health_of(self, page: Any) -> RiskSignal:
        """健康检查核心（check_health/goto 共用；返回信号，EVICTED/SECURITY_CHECK 置挂起）。

        形态顺序 [G2/H1]：about:* 与站外跳转 → EVICTED；URL 拦截特征 →
        SECURITY_CHECK；验证类文字 → SECURITY_CHECK。注意健康检查假定当前页
        已导航——新开页的初始 ``about:blank`` 同样判 EVICTED（about:blank 即
        驱逐签名），调用方应先 goto 再探测。
        """
        try:
            url = str(page.url)
        except TargetClosedError:
            self._handle_target_closed()
            raise BrowserClosedError(self._closed_message()) from None

        if url.startswith("about:"):
            self._mark_risk(RiskSignal.EVICTED, f"url={url}")
            return RiskSignal.EVICTED
        if self._site_host not in url:
            self._mark_risk(RiskSignal.EVICTED, f"非我方发起的站外跳转 url={url}")
            return RiskSignal.EVICTED
        if self._risk_url_re.search(url):
            self._mark_risk(RiskSignal.SECURITY_CHECK, f"url={url}")
            return RiskSignal.SECURITY_CHECK
        body = self._probe_body_text(page)
        if body and self._risk_words_re.search(body[:_TEXT_PROBE_CHARS]):
            self._mark_risk(RiskSignal.SECURITY_CHECK, "页面出现安全验证类文字")
            return RiskSignal.SECURITY_CHECK
        self._last_signal = RiskSignal.NONE
        return RiskSignal.NONE

    def _probe_body_text(self, page: Any) -> str:
        """body 可见文字探测（失败按"无风控文字"处理——X4 协议口径）。

        注意 TargetClosedError 是 PatchrightError 子类，必须先行匹配。
        """
        try:
            return str(page.locator("body").first.inner_text(timeout=_PROBE_TIMEOUT_MS))
        except TargetClosedError:
            self._handle_target_closed()
            raise BrowserClosedError(self._closed_message()) from None
        except PatchrightError:
            return ""

    def _mark_risk(self, signal: RiskSignal, detail: str) -> None:
        """记录风控信号并挂起会话（M5：无自动恢复——仅人工 start() 解除）。"""
        self._last_signal = signal
        self._risk_pending = signal
        logger.warning(
            "风控信号 %s：%s（会话挂起，禁止自动重导航，等待人工处理）",
            signal.name,
            detail,
        )


def _default_prefix(url: str) -> str:
    """导航落点校验的默认前缀：目标 URL 去 query/fragment。"""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"
