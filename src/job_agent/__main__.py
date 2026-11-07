"""python -m job_agent 入口：装配 + 启动（终版，Stage5 S5d；§2/§3）。

启动序列（顺序固定；S1c 骨架 → S5d 终版）：

1. ``_force_utf8_stdio`` [R6-8]（先于任何中文输出）；
2. 单实例锁 [B5]：冲突 → 退出码 2 + 友好提示（A-BOOT-2）；
3. 日志 [R6-5]：文件轮转 + 打码（目录锚定应用根 [R6-2]）；
4. 配置 [R6-1]：安全参数警告逐条打印；
5. DB 迁移（``open_database``，幂等）；
6. 引擎装配（S4d 完成报告序列 + S5d 补充项，见 :func:`assemble_engine`）；
7. 引擎 ``daemon`` 线程 ``run_forever``；
8. uvicorn 副线程（S5a ``create_app`` + S5c ``register_ops_routes``，
   ``config_loader`` 绑定引擎当前配置快照；§5.5 I6——控制台指令队列 =
   ``orch.command_queue()``；启动失败仅引擎继续运行）；
9. 主线程等待退出信号 → 优雅退出：停引擎 → 停控制台（``server.stop``）→
   关通知/DB → flush 日志（:func:`shutdown_logging`）。

退出信号：SIGINT / SIGTERM（Windows 加 SIGBREAK）；测试可另用
``JOB_AGENT_STOP_FILE`` 哨兵文件触发**同一条**优雅路径（无控制台会话的
CI 管道态兜底，见 :func:`_stop_sentinel`）。

启动自检输出（横幅）：版本 / Chrome 版本 [G4]（run_state 上次记录，写入方
``browser.patchright_session``）/ 配置·数据库·日志**绝对路径** [R6-2] /
dry_run / 前置条件清单 [B14]。

降级模式：适配器装配失败（如 zhipin 适配器随 S6 交付、当前未注册）时引擎
线程不启动，控制台照常服务——横幅与日志明示原因，退出码不变。
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from queue import Queue
from types import FrameType

from job_agent import __version__
from job_agent.adapters.base import SearchFilters, SiteAdapter, UnknownAdapterError, create_adapter
from job_agent.ai.greeting import current_greeting
from job_agent.browser.strategy_base import BrowserSession
from job_agent.config import (
    Config,
    app_root,
    default_config_path,
    load_config,
    resolve_path,
    safety_warnings,
)
from job_agent.console.api.ops import register_ops_routes
from job_agent.console.app import ConsoleServer, create_app
from job_agent.core import runstate
from job_agent.core.delivery import DeliveryPipeline
from job_agent.core.messenger import Messenger
from job_agent.core.orchestrator import TICK_S, EngineCommand, Orchestrator
from job_agent.core.session import BaselineEntry, ConversationState
from job_agent.core.state_machine import SendStateMachine
from job_agent.db import Database, open_database
from job_agent.logging_setup import setup_logging, shutdown_logging
from job_agent.notify.center import NotificationCenter
from job_agent.resume.service import confirmed_directions, get_current_snapshot
from job_agent.single_instance import InstanceExistsError, SingleInstanceLock

logger = logging.getLogger(__name__)

# 引擎线程优雅退出等待（tick 5s + 最长一次任务节流 20s 的余量）
_ENGINE_JOIN_S = 30.0
# 控制台 uvicorn 就绪等待 / 协作退出等待
_CONSOLE_START_S = 10.0
_CONSOLE_JOIN_S = 10.0
# 主线程退出轮询间隔（信号在两次 wait 之间送达并处理）
_EXIT_POLL_S = 0.5
# 投递翻页上限（超出回落第 1 页；搜索结果首页随时间自然刷新）
_MAX_SEARCH_PAGES = 5

#: run_state 键：Chrome 版本记录 [G4]（键名与 browser.patchright_session 对齐；
#: 此处按字面量引用，避免 mock 路径引入 patchright 依赖）
_KEY_CHROME_VERSION = "chrome_version"


def _force_utf8_stdio() -> None:
    """标准输出/错误被重定向（管道/文件）时按 UTF-8 输出 [R6-8]。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8")  # 非 TextIO 或已关闭：保持现状


# ---- 启动前置条件（§4 清单，B14）----


def check_prerequisites(db: Database, config: Config) -> tuple[bool, list[str]]:
    """启动前置条件清单 [B14]：缺任一项 → 投递任务不启动。

    ① AI 连通性自检：取**离线口径**（端点 + 密钥已配置）——真实连通性由
    控制台 AI 配置页『连通性测试』发起，不在启动路径引入网络依赖；
    ② 简历已解析（resume_snapshots 存在 current 版本）；
    ③ ≥1 组职位方向已确认（job_directions.is_confirmed=1）；
    ④ 主动招呼语已定稿（greeting_templates 存在 current active 模板）；
    ⑤ 浏览器会话登录态有效：按 §4「缓冲为运行时检测」，由引擎登录巡检
    （FR1.3）承担，不进启动清单。

    返回 ``(全部满足, 缺项列表)``；缺项同时用于横幅与控制台总览指引。
    """
    missing: list[str] = []
    if not config.ai.api_key:
        missing.append("ai_not_configured")
    if get_current_snapshot(db) is None:
        missing.append("resume_missing")
    if not confirmed_directions(db):
        missing.append("direction_missing")
    if current_greeting(db, "active") is None:
        missing.append("greeting_missing")
    return (not missing), missing


# ---- 站点适配器与浏览器会话（mock/zhipin 按配置）----


def _mock_fixtures_dir() -> Path:
    """mock 夹具目录：应用根优先（支持 JOB_AGENT_HOME 隔离 [R6-2]），回落源码树。"""
    candidate = app_root() / "tests" / "fixtures" / "sim"
    if candidate.is_dir():
        return candidate
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sim"


def _build_site_adapter(config: Config, db: Database) -> tuple[SiteAdapter, BrowserSession | None]:
    """按 ``site.adapter`` 装配适配器（§2：L3 只经注册表 create_adapter）。

    - mock（§12）：夹具目录见 :func:`_mock_fixtures_dir`；``dry_run`` 透传为
      引擎侧短路之外的第二道保险（S4a 契约）；
    - zhipin：先建 :class:`PatchrightSession`（浏览器会话，S4b；构造不启动
      浏览器），再 ``create_adapter("zhipin", session, config)``。ZhipinAdapter
      本体随 S6 交付——当前注册表无该 site_id，:class:`UnknownAdapterError`
      由 :func:`_run_locked` 捕获进入降级模式。
    """
    if config.site.adapter == "mock":
        return (
            create_adapter("mock", str(_mock_fixtures_dir()), dry_run=config.delivery.dry_run),
            None,
        )
    from job_agent.browser.patchright_session import PatchrightSession

    session = PatchrightSession(config, db)
    return create_adapter("zhipin", session, config), session


def _browser_hooks(
    session: BrowserSession,
) -> tuple[Callable[[], bool], Callable[[], None]]:
    """浏览器 relaunch / 登录模式互斥 hook（S4b 会话 × S5d 装配，[M3/G5]）。

    - relaunch（控制台『重新拉起浏览器』，I6 ``relaunch_browser``）：幂等
      拆旧连接 → ``start()``（人工路径：清除风控挂起与等待人工标志）；
    - login_mode（I6 ``login_mode``）：``launch_login_window()`` 以零自动化
      参数启动普通浏览器进程。互斥双保险：编排器先校验"引擎已停"
      （running 拒绝并保持 ``login_mode`` 引擎互斥），会话层再校验"自动化
      浏览器未运行"——同一 profile 不允许双进程占用。
    """

    def relaunch() -> bool:
        try:
            session.shutdown()
            session.start()
        except Exception:
            logger.warning("浏览器重新拉起失败（保持等待人工状态）", exc_info=True)
            return False
        logger.info("浏览器已重新拉起（编排器随后复核登录/风控状态）")
        return True

    def login_mode() -> None:
        session.launch_login_window()

    return relaunch, login_mode


# ---- 投递绑定（S4c）----


def _delivery_runner(
    db: Database, adapter: SiteAdapter, orch: Orchestrator
) -> Callable[[datetime], None]:
    """``bind_delivery`` 绑定体：跑一页搜索结果的 §7 十步流水线。

    每次调用以**当前**配置快照与闸门重建 :class:`DeliveryPipeline`——
    ``reload_config`` 后的新配置/新闸门下一轮即生效；搜索词 = 已确认
    职位方向关键词并集（§5.2），城市 = ``config.target.city``。整页走完
    （stop_reason 为空）翻页，上限 :data:`_MAX_SEARCH_PAGES`；提前停止
    （限额/时段/闸门）下轮回落第 1 页重新观察。
    """
    page = 1

    def run(now: datetime) -> None:
        nonlocal page
        config = orch.config
        keywords = [kw for d in confirmed_directions(db) for kw in d.keywords]
        # clock/sleep 取流水线默认真实时钟（内部 min_interval 等待依赖真实流逝）
        pipeline = DeliveryPipeline(db, config, adapter, gate=orch.gate)
        report = pipeline.run_page(
            page=page, filters=SearchFilters(keywords=keywords, city=config.target.city)
        )
        ran_page = page
        page = page % _MAX_SEARCH_PAGES + 1 if report.stop_reason is None else 1
        logger.info(
            "[S4c] 投递页完成 page=%d jobs=%d dry_run=%s stop=%s greeting_probe=%s",
            ran_page,
            report.jobs_seen,
            report.dry_run,
            report.stop_reason,
            report.greeting_probe,
        )

    return run


# ---- 启动恢复与首次基线 ----


def _startup_recover(intents: SendStateMachine) -> None:
    """启动恢复 [B4/§13]：残留 ``pending`` intent → ``unknown``（只对账不重发）。"""
    intents.startup_recover()


def _first_run_baseline(db: Database, adapter: SiteAdapter, conv_state: ConversationState) -> None:
    """首次运行会话基线 [B3]：既有会话标记 ``is_baseline=1``，不参与自动化。

    非首次启动（conversations 表已有行）为无操作；**dry_run 关闭时的重拍**
    [N2] 由控制台动作触发（S5/S7），不在启动路径。站点访问失败（如 zhipin
    浏览器尚未拉起）记 WARNING 跳过，不阻断启动——S6 会话装配交付后由
    引擎 start 流程补拍。
    """
    if db.query_one("SELECT 1 FROM conversations LIMIT 1") is not None:
        return
    try:
        count = adapter.snapshot_conversation_baseline()
        convs = adapter.list_conversations() if count > 0 else []
        entries = [
            BaselineEntry(
                conv.conv_id,
                conv.peer_name,
                conv.job_ref.job_id if conv.job_ref is not None else None,
            )
            for conv in convs[:count]
        ]
    except Exception:
        logger.warning("首次运行基线快照失败（跳过，不影响启动）", exc_info=True)
        return
    if not entries:
        logger.info("首次运行基线快照：站点无既有会话 [B3]")
        return
    written = conv_state.snapshot_baseline(entries)
    logger.info("首次运行基线快照：%d 个既有会话标记 is_baseline=1 [B3]", written)


# ---- 引擎装配（S4d 完成报告序列）----


@dataclass
class EngineRuntime:
    """装配完成的引擎束（main 生命周期管理与启动测试共用）。"""

    db: Database
    config: Config
    adapter: SiteAdapter
    session: BrowserSession | None  # zhipin 路线的浏览器会话（mock 为 None）
    orchestrator: Orchestrator
    messenger: Messenger
    center: NotificationCenter
    prerequisites_met: bool
    missing: list[str] = field(default_factory=list)


def assemble_engine(config: Config, db: Database) -> EngineRuntime:
    """引擎装配：S4d 完成报告序列 + S5d 启动补充项（顺序即依赖序）。

    ::

        NotificationCenter(db, config)            # I5 接线：events.emit → 完整管线
        startup_recover()                          # B4：pending → unknown
        create_adapter(...)                        # mock / zhipin（含浏览器会话）
        首次运行基线 [B3]
        orch = Orchestrator(db, adapter, config_loader=load_config,
                            relaunch_hook=..., login_mode_hook=...)
        msgr = Messenger(db, adapter, config_provider, gate_provider,
                         session, intents)
        orch.bind_messenger(msgr)
        orch.bind_delivery(...)                    # S4c 流水线
        orch.set_prerequisites_met(...)            # §4 清单 [B14]
        # daemon 线程 run_forever 由调用方（main/测试）启动

    配置热重载：``reload_config``（I6）经 ``config_loader=load_config`` 重读
    应用根 config.yaml；Messenger 的 session 计数上限取启动时快照（S4d 口径），
    投递流水线每轮以 ``orch.config``/``orch.gate`` 现取（reload 即生效）。
    """
    center = NotificationCenter(db, config)
    intents = SendStateMachine(db)
    _startup_recover(intents)
    adapter, session = _build_site_adapter(config, db)
    conv_state = ConversationState(db, config)
    _first_run_baseline(db, adapter, conv_state)
    relaunch_hook, login_hook = _browser_hooks(session) if session is not None else (None, None)
    prereq_met, missing = check_prerequisites(db, config)
    for item in missing:
        logger.warning("启动前置条件未满足：%s（投递任务暂不启动；控制台总览逐项指引 [B14]）", item)
    orch = Orchestrator(
        db,
        adapter,
        config_loader=load_config,
        relaunch_hook=relaunch_hook,
        login_mode_hook=login_hook,
        prerequisites_met=prereq_met,
    )
    msgr = Messenger(
        db,
        adapter,
        config_provider=lambda: orch.config,
        gate_provider=lambda: orch.gate,
        session=conv_state,
        intents=intents,
    )
    orch.bind_messenger(msgr)
    orch.bind_delivery(_delivery_runner(db, adapter, orch))
    orch.set_prerequisites_met(prereq_met)
    logger.info(
        "引擎装配完成（adapter=%s dry_run=%s 前置条件=%s）",
        config.site.adapter,
        config.delivery.dry_run,
        "满足" if prereq_met else f"缺{len(missing)}项",
    )
    return EngineRuntime(
        db=db,
        config=config,
        adapter=adapter,
        session=session,
        orchestrator=orch,
        messenger=msgr,
        center=center,
        prerequisites_met=prereq_met,
        missing=missing,
    )


# ---- 控制台装配（S5a create_app/ConsoleServer + S5c register_ops_routes）----


def _start_console(
    config: Config,
    db: Database,
    command_queue: Queue[EngineCommand] | None,
    config_provider: Callable[[], Config],
) -> ConsoleServer | None:
    """启动控制台副线程（§2 进程模型；§5.5 I6 指令队列贯通）。

    装配协议（S5a ``console/app.py`` 与 S5c ``console/api/ops.py`` 的 S5d
    注记）：

    - ``create_app(db=..., command_queue=..., config_loader=...)``——S5a 骨架
      （health/status）+ S5b「配置与内容类 5 页」自动挂载；
    - ``register_ops_routes(app, db=..., command_queue=orch.command_queue(),
      config_loader=...)``——S5c「运行与操作类」路由（``POST /api/engine/*``
      等）需 OpsContext 注入，由 S5d 装配时 include；
    - ``ConsoleServer(app, host, port).start()``——uvicorn 副线程，仅监听
      127.0.0.1；``wait_until_ready`` 为启动自检。

    ``config_provider`` 绑定引擎当前配置快照（``reload_config`` 后控制台与
    引擎读同一份）。引擎未装配（降级）时 ``command_queue=None``，跳过 ops
    路由（无队列可投递）。启动失败（端口占用等）记 ERROR 返回 None——
    引擎不受影响。
    """
    try:
        app = create_app(db=db, command_queue=command_queue, config_loader=config_provider)
        if command_queue is not None:
            register_ops_routes(
                app,
                db=db,
                command_queue=command_queue,
                config_loader=config_provider,
            )
        server = ConsoleServer(app, host=config.console.host, port=config.console.port)
        server.start()
    except Exception:
        logger.exception("控制台启动失败（引擎不受影响）")
        return None
    if not server.wait_until_ready(timeout=_CONSOLE_START_S):
        logger.error(
            "控制台 %.0fs 内未就绪（http://%s:%d 可能被占用）——仅引擎继续运行",
            _CONSOLE_START_S,
            config.console.host,
            config.console.port,
        )
        return None
    logger.info("控制台线程已启动：http://%s:%d", config.console.host, config.console.port)
    return server


# ---- 退出信号与哨兵 ----


def _install_exit_handlers(exit_event: threading.Event) -> None:
    """SIGINT/SIGTERM（Windows 加 SIGBREAK）→ 置位同一优雅退出事件。"""

    def _handle(signum: int, _frame: FrameType | None) -> None:
        logger.info("收到退出信号 %s：开始优雅退出", signal.Signals(signum).name)
        exit_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if isinstance(sig, int):
            with contextlib.suppress(OSError, ValueError):
                signal.signal(sig, _handle)


def _stop_sentinel() -> Path | None:
    """测试哨兵 [测试通道]：``JOB_AGENT_STOP_FILE`` 文件出现时走优雅退出。

    与信号完全同一条退出路径（CI 管道态无控制台会话、CTRL_BREAK 送不进
    时的确定性兜底）；未设置环境变量时无任何行为。
    """
    raw = os.environ.get("JOB_AGENT_STOP_FILE", "").strip()
    return Path(raw) if raw else None


def _wait_for_exit(
    exit_event: threading.Event,
    sentinel: Path | None,
    engine_thread: threading.Thread | None,
) -> int:
    """主线程等待循环：退出信号 / 哨兵文件 / 引擎线程意外死亡（→1）。"""
    while not exit_event.is_set():
        if engine_thread is not None and not engine_thread.is_alive():
            logger.error("引擎线程意外退出——进程终止（异常详情见日志）")
            return 1
        if sentinel is not None and sentinel.exists():
            logger.info("检测到停止哨兵文件 %s：走优雅退出路径", sentinel)
            exit_event.set()
            break
        exit_event.wait(_EXIT_POLL_S)
    return 0


def _engine_main(runtime: EngineRuntime) -> None:
    """引擎线程体：``run_forever``（tick 异常自愈，仅 shutdown 退出）。"""
    try:
        runtime.orchestrator.run_forever()
    except Exception:
        logger.exception("引擎线程致命异常（run_forever 退出）")


# ---- 启动自检横幅 [R6-2/G4/B14] ----


def build_banner(
    config: Config,
    config_loaded: bool,
    *,
    logs_dir: str,
    db_path: str,
    adapter_line: str,
    chrome_line: str,
    prerequisites: Sequence[str] = (),
) -> str:
    """构造启动横幅（配置/数据库/日志一律**绝对路径**锚定应用根 [R6-2]）。"""
    source = (
        f"{default_config_path()}（已加载）"
        if config_loaded
        else "内置默认值（未找到 config.yaml，可在控制台首次引导中生成）"
    )
    dry_run = "开启（send_* 仅记录为拟发送，不实际发送）" if config.delivery.dry_run else "关闭"
    if prerequisites:
        prereq = f"缺 {len(prerequisites)} 项——{', '.join(prerequisites)}（总览页逐项指引）"
    else:
        prereq = "全部满足"
    return "\n".join(
        [
            "=" * 68,
            f"  job-agent v{__version__} — Boss直聘 AI 求职助手",
            "=" * 68,
            f"  配置来源 : {source}",
            f"  数据库   : {db_path}",
            f"  日志目录 : {logs_dir}",
            f"  运行模式 : 试运行 dry_run={dry_run}",
            f"  站点适配 : {adapter_line}",
            f"  Chrome   : {chrome_line}",
            f"  前置条件 : {prereq}",
            f"  控制台   : http://{config.console.host}:{config.console.port}",
            "=" * 68,
        ]
    )


def _chrome_line(db: Database, adapter_id: str) -> str:
    """Chrome 版本横幅行 [G4]：run_state 上次记录（写入方浏览器会话层）。"""
    if adapter_id == "mock":
        return "—（mock 适配器无浏览器会话）"
    recorded = runstate.get_state(db, _KEY_CHROME_VERSION)
    if isinstance(recorded, dict) and recorded.get("full"):
        return f"{recorded['full']}（run_state 上次记录；本次拉起浏览器时刷新 [G4]）"
    return "尚未记录（浏览器首次拉起时写入 [G4]）"


# ---- 主流程 ----


def main() -> int:
    """入口主流程（启动序列见模块 docstring）。"""
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
    logger.info("job-agent v%s 启动", __version__)

    # 3) 加载配置 + 安全参数警告 [R6-1/R6-2]
    config = load_config()
    config_loaded = default_config_path().exists()
    for warn in safety_warnings(config):
        logger.warning("安全参数警告：%s", warn)

    # 4) DB 迁移（幂等）→ 5) 引擎装配（失败 → 降级：仅控制台）
    db_path = resolve_path(config.data.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = open_database(db_path)
    runtime: EngineRuntime | None = None
    try:
        runtime = assemble_engine(config, db)
    except UnknownAdapterError as exc:
        logger.warning(
            "站点适配器装配失败：%s（zhipin 适配器随 S6 交付，当前可用 site.adapter=mock）"
            "——引擎线程未启动，控制台以降级模式运行",
            exc,
        )
    # 装配失败统一降级（含未注册 site_id），原因入日志与横幅——控制台照常服务
    except Exception as exc:
        logger.warning(
            "引擎装配失败（%s: %s）——引擎线程未启动，控制台以降级模式运行",
            type(exc).__name__,
            exc,
        )
        logger.debug("引擎装配异常栈：", exc_info=True)

    # 6) 启动自检横幅（绝对路径 [R6-2]；Chrome 版本 [G4]；前置条件 [B14]）
    print(
        build_banner(
            config,
            config_loaded,
            logs_dir=str(logs_dir),
            db_path=str(db_path),
            adapter_line=(
                config.site.adapter if runtime is not None else f"{config.site.adapter}（未装配）"
            ),
            chrome_line=_chrome_line(db, config.site.adapter),
            prerequisites=runtime.missing if runtime is not None else ["engine_not_assembled"],
        )
    )

    # 7) 引擎 daemon 线程（S4d 序列收尾：run_forever）
    engine_thread: threading.Thread | None = None
    if runtime is not None:
        engine_thread = threading.Thread(
            target=_engine_main, args=(runtime,), name="engine", daemon=True
        )
        engine_thread.start()
        logger.info("引擎线程已启动（tick=%.0fs，daemon；I6 指令经控制台投递）", TICK_S)

    # 8) uvicorn 副线程（S5a create_app + S5c ops 路由；§5.5 I6 队列贯通）
    exit_event = threading.Event()
    _install_exit_handlers(exit_event)
    sentinel = _stop_sentinel()
    runtime_ref = runtime  # 闭包窄化（mypy）：降级分支回落启动时快照

    def config_provider() -> Config:
        if runtime_ref is not None:
            return runtime_ref.orchestrator.config
        return config

    command_queue = runtime.orchestrator.command_queue() if runtime is not None else None
    console = _start_console(config, db, command_queue, config_provider)
    logger.info("启动自检完成：等待退出信号（Ctrl+C / SIGTERM）")
    rc = _wait_for_exit(exit_event, sentinel, engine_thread)

    # 9) 优雅退出：停引擎 → 关控制台 → 关通知/DB → flush 日志
    logger.info("优雅退出开始：停引擎 → 关控制台 → flush 日志")
    if runtime is not None:
        runtime.orchestrator.shutdown()
    if engine_thread is not None:
        engine_thread.join(timeout=_ENGINE_JOIN_S)
        if engine_thread.is_alive():
            logger.warning("引擎线程 %.0fs 内未退出（daemon 兜底随进程结束）", _ENGINE_JOIN_S)
    if console is not None:
        console.stop(timeout=_CONSOLE_JOIN_S)
    if runtime is not None:
        runtime.center.close()
    db.close()
    logger.info("job-agent v%s 已退出（退出码 %d）", __version__, rc)
    shutdown_logging()  # flush + 关闭全部自装配 handler
    return rc


if __name__ == "__main__":
    sys.exit(main())
