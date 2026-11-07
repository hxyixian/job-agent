"""S5d ``__main__`` 终版启动集成测试（Stage5 验收：A-BOOT-1 等价 / A-BOOT-2
__main__ 级 / A-BOOT-3 防休眠）。

- A-BOOT-1 等价：mock adapter 配置下子进程启动（临时 ``JOB_AGENT_HOME`` +
  ``LOCALAPPDATA`` 隔离 [R6-2]）→ 存活 + 引擎 ``run_state`` 佐证（控制台
  装配位 S5a 就绪时升级为 health 200 断言）→ 优雅退出码 0 + 日志无
  ERROR/CRITICAL；
- A-BOOT-2（__main__ 级，补充既有 ``test_single_instance`` 的锁级用例）：
  实例 A 运行中 → 实例 B 退出码 2 + "已在运行"提示，A 不受影响；
- A-BOOT-3 防休眠 [G11]：主证据 = 子进程引擎 start（经控制台 I6）后
  ``powercfg /requests`` 的 SYSTEM 区块出现 python [PROCESS] 请求、stop 后
  消失（powercfg 不显示查询进程自身请求，故由子进程持有；不可用时 skip，
  以进程内用例佐证：S4d 序列装配 + ``run_state.engine_state`` running +
  ``SetThreadExecutionState`` 激活/释放均返回 True）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from boot_support import WIDE_SCHEDULE, BootedAgent, free_port, write_config

from job_agent.__main__ import (
    _browser_hooks,
    _mock_fixtures_dir,
    assemble_engine,
    check_prerequisites,
)
from job_agent.config import Config
from job_agent.core import runstate
from job_agent.core.orchestrator import (
    KEY_ENGINE_STATE,
    EngineCommand,
    windows_keep_awake,
)
from job_agent.db import open_database

_BOOT_MARKER = "启动自检完成"
_EXIT_MARKER = "已退出（退出码 0）"


def _console_available() -> bool:
    """控制台装配位（S5a）是否已合入（未合入时 health 检查以引擎佐证替代）。"""
    return importlib.util.find_spec("job_agent.console.app") is not None


# ---- A-BOOT-1 等价：子进程启动 → 存活/health → 优雅退出 ----


def _wait_engine_state(agent: BootedAgent, expected: str, timeout: float = 20.0) -> dict[str, Any]:
    """轮询子进程 DB 的 run_state.engine_state 直到 state == expected。"""
    deadline = time.monotonic() + timeout
    snapshot: Any = None
    while time.monotonic() < deadline:
        snapshot = agent.run_state(KEY_ENGINE_STATE)
        if isinstance(snapshot, dict) and snapshot.get("state") == expected:
            return snapshot
        assert agent.is_alive(), f"引擎状态等待中进程退出\n{agent.text('stderr')}"
        time.sleep(0.3)
    raise AssertionError(f"等待 engine_state={expected} 超时：最后读到 {snapshot!r}")


def _assert_health(port: int) -> None:
    """控制台 health 端点 200（装配位就绪时的 A-BOOT-1 完整口径）。"""
    statuses: list[tuple[str, int]] = []
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as client:
        for path in ("/api/health", "/health"):
            try:
                statuses.append((path, client.get(path).status_code))
            except httpx.HTTPError:
                statuses.append((path, -1))
    assert any(code == 200 for _path, code in statuses), f"health 均 非 200：{statuses}"


def test_aboot1_boot_alive_and_graceful_exit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    lock_root = tmp_path / "localappdata"
    lock_root.mkdir()
    port = free_port()
    write_config(home, port=port)

    with BootedAgent(home, lock_root) as agent:
        agent.wait_marker("job-agent v", timeout=90)
        agent.wait_marker(_BOOT_MARKER, timeout=30)
        agent.wait_marker("引擎线程已启动", timeout=30)
        if _console_available():
            _assert_health(port)
        else:
            # 装配位 S5a 未合入：以引擎 run_state 佐证（state=stopped 为待命态）
            snapshot = _wait_engine_state(agent, "stopped")
            assert snapshot["dry_run"] is True
        rc = agent.graceful_stop()

    assert rc == 0, agent.text("stderr")
    log_text = (home / "logs" / "app.log").read_text(encoding="utf-8")
    assert _EXIT_MARKER in log_text
    assert " ERROR " not in log_text, log_text[-2000:]
    assert " CRITICAL " not in log_text, log_text[-2000:]


# ---- A-BOOT-2（__main__ 级）：双实例锁 ----


def test_aboot2_second_instance_exits_with_code_2(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    lock_root = tmp_path / "localappdata"
    lock_root.mkdir()
    write_config(home)

    with BootedAgent(home, lock_root) as first:
        first.wait_marker(_BOOT_MARKER, timeout=90)
        second = BootedAgent(home, lock_root)
        try:
            rc = second.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            second.proc.kill()
            pytest.fail("第二实例未在 60s 内退出（单实例锁未生效）")
        assert rc == 2
        assert "已在运行" in second.text("stderr")
        assert first.is_alive()
        assert first.graceful_stop() == 0


# ---- A-BOOT-3：防休眠 [G11]（进程内装配 + run_state 佐证 + powercfg 可选）----


def _powercfg_output() -> str | None:
    """powercfg /requests 输出文本；命令不可用（需管理员等）返回 None。

    输出按本机控制台编码（GBK）而非 UTF-8 解码——显式 ``errors="replace"``
    防止 reader 线程解码崩溃（断言只看 ASCII 标记 ``[PROCESS]``/``python``）。
    注意：powercfg **不显示查询进程自身的请求**，故须由子进程持有执行状态。
    """
    if sys.platform != "win32":
        return None
    try:
        proc = subprocess.run(
            ["powercfg", "/requests"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = proc.stdout or ""
    if proc.returncode != 0 or "SYSTEM" not in text:
        return None
    return text


def _system_section(report: str) -> str:
    return report.split("SYSTEM", 1)[1].split("DISPLAY", 1)[0]


def _awake_python_count(report: str) -> int:
    """SYSTEM 区块中 python 进程级请求的**数量**。

    powercfg ``[PROCESS]`` 行只给可执行文件路径；本仓库 venv 的 python.exe
    是 uv trampoline，实际进程映像 = uv 托管基础解释器——与本机常驻的
    其它 python（宿主进程）同路径，**无法按路径区分**，只能按计数观测
    （启动 +1 / 停止回落）。非 python 的 SYSTEM 请求（驱动/其它应用）
    不计数，避免噪声。
    """
    return sum(
        1
        for line in _system_section(report).splitlines()
        if "[process]" in line.lower() and "python" in line.lower()
    )


def test_aboot3_keep_awake_powercfg_subprocess(tmp_path: Path) -> None:
    """A-BOOT-3 主证据 [G11]：子进程引擎 running（宽时段）→ powercfg SYSTEM
    的 python [PROCESS] 请求 +1；stop 后回落（时段结束即释放的等价观测）。"""
    pytest.importorskip("httpx")
    home = tmp_path / "home"
    home.mkdir()
    lock_root = tmp_path / "localappdata"
    lock_root.mkdir()
    port = free_port()
    write_config(home, port=port)  # 宽时段：引擎 running 期间防休眠窗口恒成立

    with BootedAgent(home, lock_root) as agent:
        agent.wait_marker(_BOOT_MARKER, timeout=90)
        _assert_health(port)
        baseline = _powercfg_output()
        if baseline is None:
            pytest.skip("powercfg /requests 不可用（需管理员控制台）——以进程内用例佐证")
        base_count = _awake_python_count(baseline)
        headers = {"X-Job-Agent": "console"}  # 加固第 3 项：非 GET 必带头
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=5.0, headers=headers
        ) as client:
            resp = client.post("/api/engine/start")
            assert resp.status_code < 300, resp.text
        _wait_engine_state(agent, "running", timeout=30)  # run_state 佐证 [G11]

        deadline = time.monotonic() + 15.0  # 引擎 tick ≤5s 消费命令并激活
        peak = base_count
        while time.monotonic() < deadline:
            current = _powercfg_output()
            peak = max(peak, _awake_python_count(current) if current else 0)
            if peak >= base_count + 1:
                break
            time.sleep(1.0)
        assert peak >= base_count + 1, f"running 后 SYSTEM python 请求数未 +1：{peak}"

        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=5.0, headers=headers
        ) as client:
            assert client.post("/api/engine/stop").status_code < 300
        _wait_engine_state(agent, "stopped", timeout=30)
        deadline = time.monotonic() + 15.0  # 释放发生在消费 stop 的同一 tick
        released = False
        while time.monotonic() < deadline:
            current = _powercfg_output()
            if current is not None and _awake_python_count(current) < peak:
                released = True
                break
            time.sleep(1.0)
        assert released, f"stop 后 SYSTEM python 请求数未回落：peak={peak}"

        assert agent.graceful_stop() == 0


def test_aboot3_keep_awake_runstate_evidence(tmp_path: Path) -> None:
    """A-BOOT-3 进程内佐证 [G11]：S4d 序列装配 + 宽时段 start → run_state
    running + ``SetThreadExecutionState`` 实际可用（激活/释放均返回 True）。"""
    config = Config.model_validate(
        {
            "site": {"adapter": "mock"},
            "delivery": {"dry_run": True, "run_schedule": WIDE_SCHEDULE},
        }
    )
    db = open_database(tmp_path / "agent.db")
    runtime = assemble_engine(config, db)
    orch = runtime.orchestrator
    try:
        # 空库：§4 前置条件缺项（投递闸门关）；宽时段 → 防休眠窗口成立
        assert runtime.prerequisites_met is False
        assert set(runtime.missing) == {
            "ai_not_configured",
            "resume_missing",
            "direction_missing",
            "greeting_missing",
        }
        orch.command_queue().put(
            EngineCommand(cmd="start", req_id="aboot3-start", issued_at=datetime.now(UTC))
        )
        orch.tick()  # 消费 start；housekeeping 激活 SetThreadExecutionState [G11]

        snapshot = runstate.get_state(db, KEY_ENGINE_STATE)
        assert isinstance(snapshot, dict)
        assert snapshot["state"] == "running"  # run_state 佐证 [G11/A-BOOT-3]
        assert snapshot["dry_run"] is True

        if sys.platform == "win32":
            assert windows_keep_awake(True) is True  # 置位实际生效
            assert windows_keep_awake(False) is True  # 释放（ES_CONTINUOUS 复位）
    finally:
        orch.shutdown()
        orch.tick()  # 释放防休眠（active=False → awake_ctl(False)）
        runtime.center.close()
        db.close()


# ---- 装配细节（前置条件口径 / 浏览器 hook / mock 夹具回落）----


def test_check_prerequisites_all_met(tmp_path: Path) -> None:
    from job_agent.ai.greeting import save_greeting_template
    from job_agent.models import JobDirection, ResumeSnapshot, ResumeText
    from job_agent.resume import parser
    from job_agent.resume.service import directions_confirm

    config = Config.model_validate({"ai": {"api_key": "sk-test123456"}})
    db = open_database(tmp_path / "p.db")
    try:
        parser.save_resume_snapshot(db, ResumeText("教育经历 工作经历"), ResumeSnapshot())
        directions_confirm(db, [JobDirection(keywords=["Python"], rationale="r")])
        save_greeting_template(db, "active", "您好，我对这个职位很感兴趣")
        ok, missing = check_prerequisites(db, config)
        assert ok and missing == []
    finally:
        db.close()


def test_browser_hooks_wiring() -> None:
    """relaunch/login hook 与浏览器会话的接线（互斥语义本体在编排器/会话层）。"""

    class StubSession:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.fail = False

        def shutdown(self) -> None:
            self.calls.append("shutdown")

        def start(self) -> None:
            self.calls.append("start")
            if self.fail:
                raise RuntimeError("boom")

        def launch_login_window(self) -> None:
            self.calls.append("login_window")

    stub = StubSession()
    relaunch, login_mode = _browser_hooks(stub)
    assert relaunch() is True
    assert stub.calls == ["shutdown", "start"]
    stub.fail = True
    stub.calls.clear()
    assert relaunch() is False  # 失败上报（编排器保持 fatal 等待人工）
    assert stub.calls == ["shutdown", "start"]
    login_mode()
    assert stub.calls[-1] == "login_window"


def test_mock_fixtures_dir_falls_back_to_source_tree(tmp_path: Path, monkeypatch: Any) -> None:
    """JOB_AGENT_HOME 无夹具时回落源码树（R6-2 隔离启动仍可装配 mock）。"""
    monkeypatch.setenv("JOB_AGENT_HOME", str(tmp_path))
    assert _mock_fixtures_dir().is_dir()
    assert (tmp_path / "tests").exists() is False
