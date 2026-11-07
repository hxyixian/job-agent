"""L2 站点适配层（设计文档 §2/§5.2）。

- L3（core/console）只 import :mod:`job_agent.adapters.base`（抽象与注册表），
  不 import 任何具体站点实现 [FR10.2]；
- 具体适配器在自身模块底部调用 ``register_adapter`` 自注册；导入本包即完成
  内置适配器注册（``mock``——开发/测试，§12；``zhipin`` 由 S6 交付后在此
  追加其模块导入，两者注册机制完全同构）。
"""

from job_agent.adapters import mock  # noqa: F401  # 导入即注册 mock
from job_agent.adapters.base import (
    AdapterBrokenError,
    AdapterError,
    LoginRequiredError,
    PlatformLimitError,
    RiskDetectedError,
    SearchFilters,
    SiteAdapter,
    TransientError,
    UnknownAdapterError,
    create_adapter,
    register_adapter,
    registered_adapters,
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
