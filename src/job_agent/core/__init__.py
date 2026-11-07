"""core：核心域纯逻辑包（Stage 2，设计文档 §5.5 / §7 / §8 / §13）。

- state_machine（S2b）：发送状态机与对账 [B4/F1]
- quota（S2b）：频率与浏览配额 [G1/G3/R6-4]
- scheduler（S2c）/ session · rampup · dryrun（S2d）由并行 SubStage 填充
"""

from __future__ import annotations
