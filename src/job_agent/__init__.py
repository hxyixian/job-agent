"""job-agent：本地运行的 Boss直聘 AI 求职助手（v1.0.0）。

工程结构总览见设计文档 §3；各模块由对应 SubStage 逐步填充：
- config（S1a，本包已交付）/ __main__（S1a 骨架，S5d 完成装配）
- db + migrations（S1b）/ logging_setup + 单实例锁（S1c）
- models / core（S2）/ ai / notify（S3）/ adapters / browser（S4）
- console（S5）
"""

__version__ = "1.0.0"
