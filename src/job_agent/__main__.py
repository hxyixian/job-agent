"""python -m job_agent 入口：装配 + 启动（设计文档 §3）。

R6-5：接入 S1c 的真实日志（文件轮转+打码）与单实例锁；
R6-8：标准输出重定向时强制 UTF-8（Windows 管道默认 GBK 会中文乱码）；
R6-2：横幅打印配置/数据库/日志的**绝对路径**（全部锚定应用根，与工作目录无关）。

后续 SubStage 在标注的装配位接入，不实现任何业务逻辑。
"""

from __future__ import annotations

import contextlib
import sys

from job_agent import __version__
from job_agent.config import Config, default_config_path, load_config, resolve_path, safety_warnings
from job_agent.logging_setup import setup_logging, shutdown_logging
from job_agent.single_instance import InstanceExistsError, SingleInstanceLock

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"  # 保留供早期引导日志


def _force_utf8_stdio() -> None:
    """标准输出/错误被重定向（管道/文件）时按 UTF-8 输出 [R6-8]。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8")  # 非 TextIO 或已关闭：保持现状


def build_banner(config: Config, config_loaded: bool, *, logs_dir: str, db_path: str) -> str:
    """构造启动横幅（绝对路径锚定应用根 [R6-2]）。"""
    source = (
        f"{default_config_path()}（已加载）"
        if config_loaded
        else "内置默认值（未找到 config.yaml，可在首次引导中生成）"
    )
    dry_run = "开启（send_* 仅记录为拟发送，不实际发送）" if config.delivery.dry_run else "关闭"
    return "\n".join(
        [
            "=" * 68,
            f"  job-agent v{__version__} — Boss直聘 AI 求职助手",
            "=" * 68,
            f"  配置来源 : {source}",
            f"  数据库   : {db_path}",
            f"  日志目录 : {logs_dir}",
            f"  运行模式 : 试运行 dry_run={dry_run}",
            f"  控制台   : http://{config.console.host}:{config.console.port}（后续 Stage 启用）",
            "  当前为工程骨架阶段（S1）：横幅打印后即退出。",
            "=" * 68,
        ]
    )


def main() -> int:
    """入口主流程（加载配置 → 日志 → 单实例锁 → 横幅 → 退出）。"""
    _force_utf8_stdio()  # [R6-8] 必须先于任何中文输出

    # 1) 单实例锁 [B5/R6-5]：重复启动退出码 2 + 友好提示
    try:
        with SingleInstanceLock():
            return _run_locked()
    except InstanceExistsError as exc:
        print(exc.friendly_message, file=sys.stderr)
        return exc.exit_code


def _run_locked() -> int:
    # 2) 日志 [R6-5]：真实文件轮转 + 打码过滤器（目录锚定应用根 [R6-2]）
    logs_dir = resolve_path("logs")
    setup_logging(logs_dir=str(logs_dir))
    import logging

    logger = logging.getLogger(__name__)
    logger.info("job-agent v%s 启动", __version__)

    # 3) 加载配置 + 安全参数警告 [R6-1/R6-2]
    config = load_config()
    config_loaded = default_config_path().exists()
    for warn in safety_warnings(config):
        logger.warning("安全参数警告：%s", warn)

    # 4) 横幅（绝对路径）
    db_path = resolve_path(config.data.db_path)
    print(build_banner(config, config_loaded, logs_dir=str(logs_dir), db_path=str(db_path)))

    # ---- 后续 SubStage 装配位（本阶段不实现任何业务逻辑）----
    # [装配位·S4d] 引擎线程：orchestrator 单执行者队列 + 调度/投递/消息循环
    # [装配位·S5d] 控制台线程：FastAPI + uvicorn，仅监听 127.0.0.1:8000
    logger.info("工程骨架阶段完成，正常退出（引擎与控制台由后续 SubStage 接入）")
    shutdown_logging()
    return 0


if __name__ == "__main__":
    sys.exit(main())
