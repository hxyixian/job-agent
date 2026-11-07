"""pytest 共享配置。

src 布局在包尚未安装（``uv sync`` 未执行）时也能导入：把仓库 ``src`` 目录
加入 ``sys.path``。包安装后本逻辑无害（sys.path 去重）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
