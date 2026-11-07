"""pytest 共享配置。

- src 布局在包尚未安装（``uv sync`` 未执行）时也能导入：把仓库 ``src``
  目录加入 ``sys.path``。包安装后本逻辑无害（sys.path 去重）。
- ``tests/sim``（假通知端点/SMTP sink 等模拟环境组件，设计文档 §12）同样
  加入 ``sys.path``，供单元测试直接 ``import fake_webhook``。
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_SRC = str(_TESTS.parent / "src")
_SIM = str(_TESTS / "sim")
for _path in (_SRC, _SIM):
    if _path not in sys.path:
        sys.path.insert(0, _path)
