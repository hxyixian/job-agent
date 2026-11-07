"""本地 SMTP sink（设计文档 §12 / T3；aiosmtpd）。

绑定 ``127.0.0.1`` 的明文 SMTP 接收器，记录收到的每封邮件（解析为
``EmailMessage``），供断言"投递内容与级别"——**不真实外发**。用法::

    with FakeSmtpSink() as sink:
        port = sink.port       # 注入 EmailConfig.smtp_port（smtp_factory 用明文 SMTP）
        ...
        msg = sink.messages[0]  # EmailMessage（Subject/From/To/get_content()）

注意：产品路径是 SMTP_SSL（§11），sink 是明文端点——测试经
``EmailNotifier(smtp_factory=...)`` 注入 ``smtplib.SMTP`` 明文客户端对接；
aiosmtpd Controller 不支持 port=0 自动分配（Windows 就绪探测连 0 端口
报错），故先探测空闲端口再启动（测试场景竞态可忽略，失败重试一次）。
"""

from __future__ import annotations

import socket
import threading
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as email_policy_default

from aiosmtpd.controller import Controller

__all__ = ["FakeSmtpSink"]


def _accept_any_auth(
    server: object, session: object, envelope: object, mechanism: str, auth_data: object
) -> str:
    """宽松鉴权：接受任意账号/密码（sink 只验证投递内容，不做身份校验）。"""
    return "235 2.7.0 Authentication successful"


class _RecordingHandler:
    """aiosmtpd 处理器：handle_DATA 解析并记录邮件。"""

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []
        self.lock = threading.Lock()

    async def handle_DATA(self, server: object, session: object, envelope: object) -> str:
        content = getattr(envelope, "content", b"")
        message = BytesParser(policy=email_policy_default).parsebytes(content)
        with self.lock:
            self.messages.append(message)
        return "250 Message accepted for delivery"


def _free_port(host: str = "127.0.0.1") -> int:
    """探测一个空闲 TCP 端口（bind 0 → 取号 → 关闭）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


class FakeSmtpSink:
    """上下文管理器形式的本地 SMTP 接收器。"""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self._host = host
        self._handler = _RecordingHandler()
        self._controller: Controller | None = None
        self.port = 0

    @property
    def messages(self) -> list[EmailMessage]:
        """已接收邮件快照（列表副本）。"""
        with self._handler.lock:
            return list(self._handler.messages)

    def reset(self) -> None:
        with self._handler.lock:
            self._handler.messages.clear()

    def start(self) -> FakeSmtpSink:
        last_error: Exception | None = None
        for _ in range(3):  # 探测端口被抢占的窄竞态：重试
            port = _free_port()
            try:
                controller = Controller(
                    self._handler,
                    hostname=self._host,
                    port=port,
                    authenticator=_accept_any_auth,  # 让 smtplib.login 可用
                    auth_require_tls=False,  # 明文 sink（产品路径仍是 SMTP_SSL）
                )
                controller.start()
            except OSError as exc:  # 端口被抢占/占用
                last_error = exc
                continue
            self._controller = controller
            self.port = port
            return self
        raise RuntimeError(f"FakeSmtpSink 启动失败（本地端口探测 3 次）: {last_error}")

    def stop(self) -> None:
        if self._controller is not None:
            self._controller.stop()
            self._controller = None
        self.port = 0

    def __enter__(self) -> FakeSmtpSink:
        return self.start()

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()
