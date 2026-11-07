"""S5d 启动集成测试共用工具（非测试模块，pytest 不收集；供
``test_main_integration`` 与 ``test_r6_hardening`` 复用）。

子进程隔离 [R6-2]：

- ``JOB_AGENT_HOME`` → 临时应用根（config/logs/data/夹具全部锚定于此）；
- ``LOCALAPPDATA`` → 临时目录（单实例锁 ``%LOCALAPPDATA%/job_agent/
  instance.lock`` 随之隔离，避免与开发者本机正在运行的实例互相冲突，
  也让 A-BOOT-2 可在测试内封闭验证）；
- stdout/stderr 重定向到各自文件（管道态中文编码由 [R6-8] 强制 UTF-8）。

优雅停止：优先向子进程组发 ``CTRL_BREAK_EVENT``（Windows，需双方共享
控制台会话）/ ``SIGINT``（POSIX）；超时未退出或发送失败时落
``JOB_AGENT_STOP_FILE`` 哨兵文件——与信号**同一条**优雅退出路径
（``__main__._stop_sentinel``），CI 无控制台会话时依然确定。
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# 宽时段（全周 00:00-23:59）：闸门恒开（防休眠窗口/轮询时段测试前提）
WIDE_SCHEDULE: dict[str, Any] = {
    "weekdays": [1, 2, 3, 4, 5, 6, 7],
    "start": "00:00",
    "end": "23:59",
}


def free_port() -> int:
    """取一个当前空闲的本地端口（bind(0) 后立即释放；存在微小竞态，测试可容忍）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_config(
    home: Path, *, adapter: str = "mock", port: int | None = None, dry_run: bool = True
) -> Path:
    """在临时应用根写入测试 config.yaml（JSON 为 YAML 子集，仅覆盖关注项）。"""
    config: dict[str, Any] = {
        "site": {"adapter": adapter},
        "delivery": {"dry_run": dry_run, "run_schedule": WIDE_SCHEDULE},
        "console": {"port": port if port is not None else free_port()},
    }
    path = home / "config.yaml"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class BootedAgent:
    """一个已启动的 ``python -m job_agent`` 子进程（上下文管理器兜底清理）。

    - 输出重定向到 ``home/<tag>-stdout.log`` / ``<tag>-stderr.log``
      （tag 随机，支持同 HOME 多实例场景互不覆盖）；
    - ``wait_marker`` 轮询输出文件等启动标记；进程提前退出时附 stderr；
    - ``graceful_stop`` 见模块 docstring 的优雅停止链。
    """

    def __init__(self, home: Path, localappdata: Path) -> None:
        self.home = home
        self.localappdata = localappdata
        tag = uuid.uuid4().hex[:8]
        self.stdout_path = home / f"{tag}-stdout.log"
        self.stderr_path = home / f"{tag}-stderr.log"
        self.stop_file = home / f"{tag}-stop.sentinel"
        env = dict(os.environ)
        env["JOB_AGENT_HOME"] = str(home)
        env["LOCALAPPDATA"] = str(localappdata)
        env["PYTHONIOENCODING"] = "utf-8"
        env["JOB_AGENT_STOP_FILE"] = str(self.stop_file)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        out = self.stdout_path.open("w", encoding="utf-8")
        err = self.stderr_path.open("w", encoding="utf-8")
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "job_agent"],
                cwd=str(home),
                env=env,
                stdout=out,
                stderr=err,
                creationflags=creationflags,
            )
        finally:
            out.close()
            err.close()

    # ---- 观测 ----

    def text(self, which: str = "stdout") -> str:
        """读取已落盘的子进程输出（stdout/stderr）。"""
        path = self.stdout_path if which == "stdout" else self.stderr_path
        return path.read_text(encoding="utf-8", errors="replace")

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def wait_marker(self, marker: str, *, timeout: float = 90.0, which: str = "stdout") -> None:
        """轮询输出文件直到出现 marker；进程提前退出或超时即断言失败。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker in self.text(which):
                return
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"进程提前退出 rc={self.proc.returncode}（等待标记 {marker!r}）\n"
                    f"--- stdout ---\n{self.text('stdout')}\n"
                    f"--- stderr ---\n{self.text('stderr')}"
                )
            time.sleep(0.2)
        raise AssertionError(
            f"等待标记 {marker!r} 超时（{timeout}s）\n"
            f"--- stdout ---\n{self.text('stdout')}\n"
            f"--- stderr ---\n{self.text('stderr')}"
        )

    def run_state(self, key: str) -> Any:
        """只读访问子进程 DB 的 run_state 键（WAL 并发读；不可用时返回 None）。"""
        db_file = self.home / "data" / "agent.db"
        if not db_file.is_file():
            return None
        try:
            conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=5.0)
        except sqlite3.Error:
            return None
        try:
            row = conn.execute("SELECT value_json FROM run_state WHERE key = ?", (key,)).fetchone()
            return json.loads(row[0]) if row is not None else None
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    # ---- 生命周期 ----

    def graceful_stop(self, *, timeout: float = 30.0) -> int:
        """优雅停止子进程（信号 → 哨兵兜底），返回退出码；超时强杀并断言失败。"""
        proc = self.proc
        if proc.poll() is None:
            if sys.platform == "win32":
                # 无共享控制台会话（管道态）时发送失败——等哨兵兜底
                with contextlib.suppress(OSError, ValueError):
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGINT)
            deadline = time.monotonic() + timeout
            touched = False
            while proc.poll() is None and time.monotonic() < deadline:
                if not touched and time.monotonic() > deadline - timeout / 2:
                    self.stop_file.touch()  # 信号半程未生效 → 哨兵走同一优雅路径
                    touched = True
                time.sleep(0.2)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10.0)
            raise AssertionError(
                f"优雅退出超时（{timeout}s），已强杀\n--- stderr ---\n{self.text('stderr')}"
            )
        return int(proc.returncode)

    def __enter__(self) -> BootedAgent:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        if self.proc.poll() is None:
            self.stop_file.touch()
            try:
                self.proc.wait(timeout=15.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10.0)
