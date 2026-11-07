"""浏览器会话层（设计文档 §5.3，S4b 交付）。

- :mod:`strategy_base`：``BrowserSession`` 策略接口 + 浏览器层异常树；
- :mod:`patchright_session`：S1b 实现（Patchright 1.64.4，2026-11-07
  第二轮 X4 定型）。

S1a（原 playwright 驱动）已被实验排除，代码不进产品（实验脚本仅在
``private/survey/`` 留档）[K2]。
"""

from job_agent.browser.patchright_session import PatchrightSession
from job_agent.browser.strategy_base import (
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
    "BrowserClosedError",
    "BrowserNotRunningError",
    "BrowserRiskError",
    "BrowserSession",
    "BrowserSessionError",
    "BrowserSetupError",
    "BrowserTimeoutError",
    "LoginWindowOpenError",
    "PatchrightSession",
    "TabName",
]
