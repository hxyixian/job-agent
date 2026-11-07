"""单实例锁：``%LOCALAPPDATA%/job_agent/instance.lock`` 独占句柄（[B5]）。

设计依据：设计文档 §2（进程模型：单实例约束）与 §3（工程结构），
开发文档 Stage1 SubStage S1c。

- Windows 实现使用 ``msvcrt.locking`` 对文件首字节加非阻塞排他锁；锁随进程
  退出（含崩溃）由操作系统自动释放，残留的 lock 文件不会阻塞下一次启动；
- POSIX 平台退化使用 ``fcntl.flock``（本项目目标平台为 Windows，此分支仅保证可运行）；
- 重复启动抛出 :class:`InstanceExistsError`，其 ``exit_code``（2）与
  ``friendly_message`` 供 ``__main__`` 直接使用：打印友好提示并以退出码 2 退出。

典型用法（``__main__`` / A-BOOT-2）::

    from job_agent.single_instance import InstanceExistsError, SingleInstanceLock

    try:
        lock = SingleInstanceLock()
        lock.acquire()
    except InstanceExistsError as exc:
        print(exc.friendly_message, file=sys.stderr)
        sys.exit(exc.exit_code)
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Any

if sys.platform == "win32":  # pragma: no cover - 平台分支
    import msvcrt
else:  # pragma: no cover - 非 Windows 兜底（本项目目标平台为 Windows）
    import fcntl

__all__ = [
    "EXIT_CODE",
    "FRIENDLY_MESSAGE",
    "InstanceExistsError",
    "SingleInstanceLock",
    "default_lock_path",
]

APP_DIR_NAME = "job_agent"
LOCK_FILE_NAME = "instance.lock"

# A-BOOT-2 / [B5]：双实例冲突的统一退出码与提示文案
EXIT_CODE = 2
FRIENDLY_MESSAGE = (
    "job-agent 已在运行（单实例限制）：为避免重复投递与浏览器会话冲突，"
    "本程序同一时间仅允许一个实例。请先关闭正在运行的实例后重新启动；"
    "若确认没有实例在运行，请重启电脑后再试。"
)


def default_lock_path() -> Path:
    """返回默认锁文件路径 ``%LOCALAPPDATA%/job_agent/instance.lock``。

    本函数不创建任何目录；仅在 :meth:`SingleInstanceLock.acquire` 时才落盘。
    """
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    base = Path(local_app_data) if local_app_data else Path.home() / ".local" / "state"
    return base / APP_DIR_NAME / LOCK_FILE_NAME


class InstanceExistsError(RuntimeError):
    """已有实例持有单实例锁时由 :meth:`SingleInstanceLock.acquire` 抛出。"""

    exit_code = EXIT_CODE

    def __init__(self, lock_path: Path, message: str | None = None) -> None:
        self.lock_path = lock_path
        self.friendly_message = f"{FRIENDLY_MESSAGE}（锁定文件：{lock_path}）"
        super().__init__(message if message is not None else self.friendly_message)


def _lock_region(fd: int) -> None:
    """对文件首字节加非阻塞排他锁；失败（已被占用）抛 OSError。"""
    if sys.platform == "win32":  # pragma: no cover - 平台分支
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - 非 Windows 兜底
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_region(fd: int) -> None:
    """释放首字节锁；未持锁等异常由调用方吞掉。"""
    if sys.platform == "win32":  # pragma: no cover - 平台分支
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - 非 Windows 兜底
        fcntl.flock(fd, fcntl.LOCK_UN)


class SingleInstanceLock:
    """跨进程独占锁（可重复 acquire/release；支持 with 语句）。

    同一进程内对同一锁文件的第二个实例同样会被拒绝（字节锁按文件句柄隔离），
    与"子进程模拟第二实例"的行为一致。
    """

    def __init__(self, lock_path: str | Path | None = None) -> None:
        self.lock_path = Path(lock_path) if lock_path is not None else default_lock_path()
        self._fd: int | None = None

    @property
    def is_held(self) -> bool:
        """当前对象是否持有锁。"""
        return self._fd is not None

    def acquire(self) -> None:
        """获取单实例锁；已有实例（本进程或其他进程）持有时抛 InstanceExistsError。"""
        if self._fd is not None:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT)
        try:
            _lock_region(fd)
        except OSError as exc:
            os.close(fd)
            raise InstanceExistsError(self.lock_path) from exc
        # 持锁后写入持有者 PID，便于人工诊断（写失败不影响锁语义）
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        self._fd = fd

    def release(self) -> None:
        """释放锁并关闭句柄；未持锁时为空操作。"""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            _unlock_region(fd)
        os.close(fd)

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()
