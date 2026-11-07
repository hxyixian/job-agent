"""single_instance 单元测试：加锁/释放/互斥 + 子进程模拟第二实例（A-BOOT-2）。

全部用例使用 tmp_path 作为锁文件路径，绝不触碰真实 %LOCALAPPDATA%。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from job_agent.single_instance import (
    EXIT_CODE,
    InstanceExistsError,
    SingleInstanceLock,
    default_lock_path,
)

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

# 子进程脚本：尝试加锁，失败时按 A-BOOT-2 契约打印友好提示并以退出码 2 退出
_TRY_ACQUIRE_CHILD = """
import sys

from job_agent.single_instance import InstanceExistsError, SingleInstanceLock

lock = SingleInstanceLock(sys.argv[1])
try:
    lock.acquire()
except InstanceExistsError as exc:
    sys.stderr.write(exc.friendly_message)
    sys.stderr.flush()
    sys.exit(exc.exit_code)
sys.stderr.write("acquired-ok")
sys.exit(0)
"""

# 子进程脚本：持锁并在就绪文件写入自身 PID，随后长眠等待被父进程终止
_HOLD_LOCK_CHILD = """
import os
import sys
import time

from job_agent.single_instance import SingleInstanceLock

lock = SingleInstanceLock(sys.argv[1])
lock.acquire()
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))
time.sleep(120)
"""


def _child_env() -> dict[str, str]:
    """子进程环境：确保 src 布局可导入（与 conftest 同一兜底策略）。"""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    parts = [str(_SRC_ROOT)] + ([existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _wait_for_ready(marker: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists() and marker.read_text(encoding="utf-8").strip():
            return
        time.sleep(0.05)
    pytest.fail(f"子实例在 {timeout}s 内未完成加锁：{marker}")


def test_default_lock_path_uses_localappdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_lock_path() == tmp_path / "job_agent" / "instance.lock"


def test_construction_has_no_file_side_effects(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path / "nested" / "instance.lock")
    assert lock.is_held is False
    assert not (tmp_path / "nested").exists()  # 目录与文件都只在 acquire 时创建


def test_acquire_release_and_reacquire(tmp_path: Path) -> None:
    lock_file = tmp_path / "instance.lock"
    lock = SingleInstanceLock(lock_file)

    lock.acquire()
    assert lock.is_held is True
    assert lock_file.exists()

    lock.release()
    assert lock.is_held is False

    # 释放后可再次获取（残留锁文件不阻塞）
    lock.acquire()
    assert lock.is_held is True
    lock.release()


def test_pid_recorded_in_lock_file(tmp_path: Path) -> None:
    lock_file = tmp_path / "instance.lock"
    lock = SingleInstanceLock(lock_file)
    lock.acquire()
    assert lock.is_held
    lock.release()
    # 首字节持锁期间其他句柄无法读取（字节锁语义），释放后 PID 仍保留在文件中
    assert str(os.getpid()) in lock_file.read_text(encoding="utf-8")


def test_second_lock_in_same_process_raises(tmp_path: Path) -> None:
    lock_file = tmp_path / "instance.lock"
    first = SingleInstanceLock(lock_file)
    first.acquire()
    try:
        second = SingleInstanceLock(lock_file)
        with pytest.raises(InstanceExistsError):
            second.acquire()
    finally:
        first.release()
    # 释放后第二个对象可获取
    second = SingleInstanceLock(lock_file)
    second.acquire()
    second.release()


def test_exception_contract(tmp_path: Path) -> None:
    lock_file = tmp_path / "instance.lock"
    holder = SingleInstanceLock(lock_file)
    holder.acquire()
    try:
        with pytest.raises(InstanceExistsError) as excinfo:
            SingleInstanceLock(lock_file).acquire()
    finally:
        holder.release()

    exc = excinfo.value
    assert isinstance(exc, RuntimeError)
    assert exc.exit_code == EXIT_CODE == 2
    assert str(exc) == exc.friendly_message
    assert "job-agent" in exc.friendly_message
    assert "已在运行" in exc.friendly_message
    assert exc.lock_path == lock_file


def test_context_manager_releases_lock(tmp_path: Path) -> None:
    lock_file = tmp_path / "instance.lock"
    with SingleInstanceLock(lock_file) as lock:
        assert lock.is_held is True
    assert lock.is_held is False
    # with 退出后可立即重新获取
    again = SingleInstanceLock(lock_file)
    again.acquire()
    again.release()


def test_subprocess_second_instance_exits_with_code_2(tmp_path: Path) -> None:
    """A-BOOT-2：父实例持锁，子进程模拟第二实例 → 退出码 2 + 友好提示。"""
    lock_file = tmp_path / "instance.lock"
    holder = SingleInstanceLock(lock_file)
    holder.acquire()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _TRY_ACQUIRE_CHILD, str(lock_file)],
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 2
        assert "acquired-ok" not in proc.stderr
        assert "job-agent 已在运行" in proc.stderr
        assert "同一时间仅允许一个实例" in proc.stderr
        assert str(lock_file) in proc.stderr
    finally:
        holder.release()


def test_lock_released_when_holder_process_dies(tmp_path: Path) -> None:
    """持锁进程被强杀（模拟崩溃）后，锁由 OS 释放，新实例可启动。"""
    lock_file = tmp_path / "instance.lock"
    ready_marker = tmp_path / "child_ready.txt"

    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK_CHILD, str(lock_file), str(ready_marker)],
        env=_child_env(),
    )
    try:
        _wait_for_ready(ready_marker)

        # 子进程持锁期间，本进程加锁必须被拒绝
        with pytest.raises(InstanceExistsError):
            SingleInstanceLock(lock_file).acquire()
    finally:
        proc.kill()  # TerminateProcess：不留任何释放锁的机会
        proc.wait(timeout=30)

    # 进程死亡后即使 lock 文件残留，也能立即重新获取
    assert lock_file.exists()
    survivor = SingleInstanceLock(lock_file)
    survivor.acquire()
    assert survivor.is_held is True
    survivor.release()
