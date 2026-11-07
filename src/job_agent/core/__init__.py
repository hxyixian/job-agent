"""core：核心域纯逻辑包（Stage 2-4，设计文档 §5.5 / §7 / §8 / §13）。

- state_machine（S2b）：发送状态机与对账 [B4/F1]
- quota（S2b）：频率与浏览配额 [G1/G3/R6-4]
- scheduler（S2c）：时段/频率/浏览组合闸门 [I7]
- session · rampup · dryrun（S2d）：会话计数/放量与试运行状态机 [I8/I9]
- delivery（S4c）：投递流水线（§7 十步）
- adapter_api / messenger / orchestrator（S4d）：站点契约结构视图、
  消息六级路由引擎（§8）、单执行者优先级编排器（§5.3 [B5]/I6）
"""

from __future__ import annotations
