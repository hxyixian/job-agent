"""Patchright 驱动替身（S4b 单测口径：全 mock page，不启真实浏览器）。

模拟面与 Patchright 同步 API 的使用面对齐（会话层实际用到的方法）：
``launch_persistent_context`` / ``BrowserContext.pages|new_page|close|browser.version``
/ ``Page.goto|wait_for_selector|locator|mouse.wheel|screenshot|close|url`` /
``Locator.count|first|nth|inner_text|click|get_attribute``。异常一律抛
patchright 真实异常类（TargetClosedError/TimeoutError），保证会话层的
except 匹配与产品路径一致。
"""

from __future__ import annotations

from typing import Any

from patchright._impl._errors import Error as PatchrightError
from patchright._impl._errors import TargetClosedError
from patchright._impl._errors import TimeoutError as PatchrightTimeout

__all__ = [
    "FakeChromium",
    "FakeContext",
    "FakeElement",
    "FakeLocator",
    "FakeMouse",
    "FakePage",
    "FakePlaywright",
    "FakeProcess",
    "FakeProcessRecorder",
    "make_pw",
]


class FakeElement:
    """一个 DOM 元素的最小替身（文本 + 属性）。"""

    def __init__(self, text: str = "", attrs: dict[str, str | None] | None = None) -> None:
        self.text = text
        self.attrs: dict[str, str | None] = dict(attrs or {})


class FakeLocator:
    """Locator 替身：元素来源 = FakePage.elements[selector]。"""

    def __init__(self, page: FakePage, selector: str, index: int | None = None) -> None:
        self._page = page
        self._selector = selector
        self._index = index

    def count(self) -> int:
        self._page._check_open()
        return len(self._page.elements.get(self._selector, ()))

    @property
    def first(self) -> FakeLocator:
        return FakeLocator(self._page, self._selector, 0)

    def nth(self, i: int) -> FakeLocator:
        return FakeLocator(self._page, self._selector, i)

    def _element(self) -> FakeElement:
        self._page._check_open()
        elements = self._page.elements.get(self._selector, ())
        index = 0 if self._index is None else self._index
        if index >= len(elements):
            raise PatchrightTimeout(f"timeout 3000ms exceeded waiting for {self._selector}")
        return elements[index]

    def inner_text(self, timeout: float | None = None) -> str:
        if self._selector == "body":
            self._page.text_probe_count += 1
        return self._element().text

    def click(self, timeout: float | None = None) -> None:
        element = self._element()  # 无匹配 → PatchrightTimeout
        self._page.clicks.append((self._selector, id(element)))

    def get_attribute(self, name: str) -> str | None:
        return self._element().attrs.get(name)


class FakeMouse:
    def __init__(self, page: FakePage) -> None:
        self._page = page
        self.wheels: list[tuple[int, int]] = []

    def wheel(self, delta_x: int, delta_y: int) -> None:
        self._page._check_open()
        self.wheels.append((delta_x, delta_y))


class FakePage:
    """同步 Page 替身。url 为属性：页面/上下文关闭后读取即抛 TargetClosedError。"""

    def __init__(self, ctx: FakeContext, url: str = "about:blank") -> None:
        self.ctx = ctx
        self._url = url
        self.elements: dict[str, list[FakeElement]] = {}
        self.nav_log: list[str] = []
        self.clicks: list[tuple[str, int]] = []
        self.screenshots: list[str] = []
        self.closed = False
        self.text_probe_count = 0
        self.mouse = FakeMouse(self)

    @property
    def url(self) -> str:
        self._check_open()
        return self._url

    @url.setter
    def url(self, value: str) -> None:
        self._url = value

    def _check_open(self) -> None:
        if self.closed or self.ctx.closed:
            raise TargetClosedError()

    def goto(
        self, url: str, *, wait_until: str | None = None, timeout: float | None = None
    ) -> None:
        self._check_open()
        self.nav_log.append(url)
        # 可脚本化落点（模拟导航弹回首页/拦截页；缺省 = 目标页）
        self._url = self.ctx.landings.get(url, url)

    def wait_for_selector(
        self, selector: str, *, timeout: float | None = None, state: str | None = None
    ) -> object:
        self._check_open()
        if self.elements.get(selector):
            return object()
        raise PatchrightTimeout(f"timeout {timeout}ms exceeded waiting for {selector}")

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def screenshot(self, path: str | None = None, **kwargs: Any) -> None:
        self._check_open()
        self.screenshots.append(str(path))

    def close(self) -> None:
        self.closed = True
        if self in self.ctx.pages:
            self.ctx.pages.remove(self)

    def kill(self) -> None:
        """测试辅助：模拟浏览器被外部关闭（后续操作抛 TargetClosedError）。"""
        self.closed = True


class FakeBrowser:
    def __init__(self, version: str) -> None:
        self.version = version


class FakeContext:
    """BrowserContext 替身：``landings`` 可脚本化导航落点。"""

    def __init__(self, version: str) -> None:
        self.browser = FakeBrowser(version)
        self.pages: list[FakePage] = []
        self.closed = False
        self.new_page_count = 0
        self.landings: dict[str, str] = {}

    def new_page(self) -> FakePage:
        if self.closed:
            raise TargetClosedError()
        page = FakePage(self)
        self.pages.append(page)
        self.new_page_count += 1
        return page

    def close(self) -> None:
        self.closed = True
        for page in list(self.pages):
            page.closed = True


class FakeChromium:
    """逐次 launch 消费 versions[i]（模拟 Chrome 升级/重建后的新实例）。

    ``fail_next_launch``：注入下一次 launch 失败（自动重建失败路径）。
    """

    def __init__(self, versions: tuple[str, ...] | list[str]) -> None:
        self.versions = list(versions)
        self.launch_calls: list[dict[str, Any]] = []
        self.contexts: list[FakeContext] = []
        self.fail_next_launch = False

    def launch_persistent_context(self, **kwargs: Any) -> FakeContext:
        self.launch_calls.append(kwargs)
        if self.fail_next_launch:
            self.fail_next_launch = False
            raise PatchrightError("injected launch failure")
        version = self.versions[min(len(self.contexts), len(self.versions) - 1)]
        ctx = FakeContext(version)
        self.contexts.append(ctx)
        return ctx


class FakePlaywright:
    """sync_playwright() 替身：__enter__ 返回带 .chromium 的根对象。"""

    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium
        self.exited = False

    def __enter__(self) -> FakePlaywright:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.exited = True


def make_pw(versions: tuple[str, ...] | list[str]) -> tuple[FakePlaywright, FakeChromium]:
    chromium = FakeChromium(versions)
    return FakePlaywright(chromium), chromium


class FakeProcess:
    """登录窗口进程替身（Popen 最小面：poll）。"""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.args: list[str] | None = None

    def poll(self) -> int | None:
        return self.returncode

    def exit(self, code: int = 0) -> None:
        """测试辅助：用户关闭登录窗口。"""
        self.returncode = code


class FakeProcessRecorder:
    """process_launcher 替身：记录每次启动参数并返回 FakeProcess。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.procs: list[FakeProcess] = []

    def launch(self, args: list[str]) -> FakeProcess:
        self.calls.append(list(args))
        proc = FakeProcess()
        proc.args = list(args)
        self.procs.append(proc)
        return proc
