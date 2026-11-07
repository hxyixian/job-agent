"""Mock 站点适配器（§12 模拟环境；导入即注册 ``site_id="mock"``）。"""

from job_agent.adapters.mock.adapter import (
    MOCK_PAGE_SIZE,
    MockCrashError,
    MockFixtureError,
    MockScenario,
    MockSiteAdapter,
    SecurityCheckForm,
)

__all__ = [
    "MOCK_PAGE_SIZE",
    "MockCrashError",
    "MockFixtureError",
    "MockScenario",
    "MockSiteAdapter",
    "SecurityCheckForm",
]
