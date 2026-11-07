"""本地 HTTP 假通知端点（设计文档 §12 / T3；TEST2 模拟测试环境组件）。

一个绑定 ``127.0.0.1`` 随机端口的极简 HTTP 接收器，记录所有 POST 请求
（头 + 原始 body），供断言"投递内容与级别"——**绝不触达真实外网**。
用法::

    with FakeWebhook() as fake:
        url = fake.url          # 注入 FeishuConfig.webhook
        ...（触发通知）...
        req = fake.requests[0]  # RecordedRequest（.json / .headers / .body）

- ``set_response_status(n)``：切换响应状态码（默认 200 + ``{"code": 0}``，
  可设 500 测试失败路径）；
- ``reset()``：清空已记录请求（同进程内多用例复用）。

仅标准库实现（http.server），测试与模拟环境零第三方依赖。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

__all__ = ["FakeWebhook", "RecordedRequest"]


class RecordedRequest:
    """一次被接收的请求（不可变快照）。"""

    __slots__ = ("body", "headers", "method", "path")

    def __init__(self, method: str, path: str, headers: dict[str, str], body: bytes) -> None:
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def json(self) -> Any:
        """解析后的 JSON body（非 JSON 返回 None）。"""
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeDecodeError):
            return None

    def __repr__(self) -> str:  # pragma: no cover（调试辅助）
        return f"RecordedRequest({self.method} {self.path}, body={self.body[:120]!r})"


class _SinkHTTPServer(ThreadingHTTPServer):
    """携带记录存储与可配置响应状态的 HTTP 服务器。"""

    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _Handler)
        self.records: list[RecordedRequest] = []
        self.lock = threading.Lock()
        self.response_status = 200


class _Handler(BaseHTTPRequestHandler):
    server: _SinkHTTPServer

    def _record(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        with self.server.lock:
            self.server.records.append(
                RecordedRequest(self.command, self.path, dict(self.headers), body)
            )

    def _respond(self) -> None:
        status = self.server.response_status
        payload = (
            b'{"code": 0, "msg": "ok"}'
            if status == 200
            else b'{"code": 99991663, "msg": "bad request"}'
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802（http.server 约定方法名）
        self._record()
        self._respond()

    def do_GET(self) -> None:  # noqa: N802（同上；调试用：返回已记录请求）
        with self.server.lock:
            snapshot = [
                {"method": r.method, "path": r.path, "body": r.text} for r in self.server.records
            ]
        payload = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002（基类签名）
        """静默访问日志（测试输出不被逐请求日志淹没）。"""


class FakeWebhook:
    """上下文管理器形式的假 Webhook 端点。"""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self._host = host
        self._server: _SinkHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """接收端点 URL（注入 FeishuConfig.webhook 用）。"""
        if self._server is None:
            raise RuntimeError("FakeWebhook 尚未 start()")
        return f"http://{self._host}:{self._server.server_address[1]}/webhook"

    @property
    def requests(self) -> list[RecordedRequest]:
        """已记录请求快照（列表副本）。"""
        if self._server is None:
            raise RuntimeError("FakeWebhook 尚未 start()")
        with self._server.lock:
            return list(self._server.records)

    def set_response_status(self, status: int) -> None:
        """切换响应状态码（200 → 成功 JSON；其他 → 错误 JSON）。"""
        if self._server is None:
            raise RuntimeError("FakeWebhook 尚未 start()")
        self._server.response_status = status

    def reset(self) -> None:
        if self._server is not None:
            with self._server.lock:
                self._server.records.clear()
            self._server.response_status = 200

    def start(self) -> FakeWebhook:
        self._server = _SinkHTTPServer((self._host, 0))
        # serve_forever 必须在独立线程跑：无它则连接悬置、shutdown() 永久阻塞
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-webhook", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._server = None
            self._thread = None

    def __enter__(self) -> FakeWebhook:
        return self.start()

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()
