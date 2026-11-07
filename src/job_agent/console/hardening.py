"""控制台六项安全加固的请求/响应管线实现（设计文档 §10.1 [S1/G8]）。

六项中四项在本模块落地；其余两项由别处保证：

1. 仅监听 ``127.0.0.1``：``config.ConsoleConfig.host`` 已用 Literal 锁定，
   ``app.serve_console`` 装配 uvicorn 时取该值（不开放配置）；
4. GET 无副作用：路由层只经 ``snapshot.ReadOnlyDatabase`` 读库（写语句与
   写属性一律阻断），本中间件自身不改动任何应用状态；

本模块实现：

2. **Host 校验中间件**：``Host ∈ {localhost, 127.0.0.1}`` 否则 403
   （允许带端口、大小写不敏感；其余主机名——含 ``127.0.0.1.nip.io``、
   ``sub.localhost``、``[::1]`` 等变体——一律拒绝，防 DNS rebinding）；
3. **非 GET/HEAD 要求** ``X-Job-Agent: console`` 头（自定义头，跨站"简单
   请求"无法携带 → 防 CSRF；缺失/取值不符均 403）；
5. **JSON 响应敏感字段打码**：api_key / webhook / secret / password 族键 →
   ``****`` + 尾 4 位（复用 :func:`job_agent.logging_setup.mask_secret_value`
   的口径，短于 8 位的值完全打码；空字符串视为"未配置"原样保留）；对任意
   ``application/json`` 响应生效，S5b/S5c 路由即使遗漏打码也被兜底；
6. **不注册 CORSMiddleware + OPTIONS 预检一律 405 + Origin 校验**：Origin
   头存在时必须为空串或与请求同源（协议、host、port 与 Host 头逐项相等；
   ``null`` 与跨源一律 403）。

中间件以纯 ASGI 形态实现（add_middleware 后位于 Starlette 异常处理层
外侧），拒绝响应直接构造 JSON，不依赖路由层。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from job_agent.logging_setup import MASK_PREFIX, mask_secret_value

__all__ = [
    "ALLOWED_HOSTS",
    "CONSOLE_HEADER",
    "CONSOLE_HEADER_VALUE",
    "SAFE_METHODS",
    "SENSITIVE_KEYS",
    "ConsoleHardeningMiddleware",
    "is_same_origin",
    "mask_sensitive_fields",
    "split_host_port",
]

#: §10.1 第 2 项：Host 主机名白名单（大小写不敏感）
ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1"})

#: §10.1 第 3 项：非 GET 请求要求的自定义头与取值
CONSOLE_HEADER = "x-job-agent"
CONSOLE_HEADER_VALUE = "console"

#: 无副作用方法（HEAD 由 Starlette 自动映射到 GET 路由，同视为安全方法）
SAFE_METHODS = frozenset({"GET", "HEAD"})

#: §10.1 第 5 项：响应中需要打码的键（归一化后精确匹配）。对应配置字段
#: ai.api_key / notify.feishu.webhook / notify.feishu.secret /
#: notify.email.password——飞书 webhook URL 自带长令牌，与签名 secret
#: 一并按"webhook secret"口径从保守打码（与 S5b ``api._helpers.SENSITIVE_PATHS``
#: 同口径；打码幂等，两层叠加不冲突）
SENSITIVE_KEYS = frozenset(
    {"api_key", "webhook", "webhook_url", "secret", "password", "passwd", "pwd"}
)


# ---- Host / Origin 解析 ----


def split_host_port(host_header: str) -> tuple[str, int | None]:
    """Host 头值 → (小写主机名, 端口或 None)；支持 ``[IPv6]:port`` 形态。"""
    value = host_header.strip()
    if not value:
        return "", None
    if value.startswith("["):  # IPv6 字面量：端口在 ] 之后
        end = value.find("]")
        if end == -1:
            return value.lower(), None
        rest = value[end + 1 :]
        port_text = rest[1:] if rest.startswith(":") else ""
        return value[1:end].lower(), int(port_text) if port_text.isdigit() else None
    host, sep, port_text = value.rpartition(":")
    if sep and port_text.isdigit():
        return host.lower(), int(port_text)
    return value.lower(), None


def is_same_origin(origin: str, *, host_header: str, scheme: str) -> bool:
    """Origin 头是否与请求自身同源（§10.1 第 6 项）。

    - 非 http/https 协议、``null``（沙箱 iframe）、无法解析的形态 → False；
    - host:port 与 Host 头逐项相等（端口缺省按协议默认端口 80/443 归一），
      且 Origin 协议与请求协议一致——``http://localhost`` 对 ``127.0.0.1``
      属跨源（浏览器同源口径），同样拒绝。
    """
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https"):
        return False
    try:
        origin_port = parsed.port
    except ValueError:  # 畸形端口
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    if origin_port is None:
        origin_port = 443 if parsed.scheme == "https" else 80
    request_host, request_port = split_host_port(host_header)
    if request_port is None:
        request_port = 443 if scheme == "https" else 80
    return (
        parsed.scheme == scheme and hostname.lower() == request_host and origin_port == request_port
    )


# ---- 敏感字段打码（§10.1 第 5 项）----


def _normalize_key(key: str) -> str:
    """键名归一化：小写、``-``/空格 → ``_``（兼容 API-Key / apiKey 形态）。"""
    return key.strip().lower().replace("-", "_").replace(" ", "_")


def mask_sensitive_fields(value: Any) -> Any:
    """递归打码 dict/list 结构中的敏感字符串字段（``****`` + 尾 4 位）。

    - 只处理字符串值：None/空串视为"未配置"原样保留（打码 ``""`` 会误导
      用户以为已配置密钥）；数值/布尔等不可能是密钥，原样返回；
    - **已是打码形态（``****`` 开头）的值原样放行**：S5b/S5c 路由可在
      源头打码（口径允许略宽，如短值也保留尾 4 位），本层作为兜底不二次
      改写，保证两层叠加结果稳定（精确幂等）；
    - 键名归一化后精确命中 :data:`SENSITIVE_KEYS` 才打码（与
      :func:`job_agent.logging_setup.mask_secret_value` 同口径：长度 < 8
      完全打码，避免尾 4 位泄露过多）。
    """
    if isinstance(value, dict):
        masked: dict[Any, Any] = {}
        for key, item in value.items():
            if (
                isinstance(key, str)
                and isinstance(item, str)
                and item != ""
                and not item.startswith(MASK_PREFIX)
                and _normalize_key(key) in SENSITIVE_KEYS
            ):
                masked[key] = mask_secret_value(item)
            else:
                masked[key] = mask_sensitive_fields(item)
        return masked
    if isinstance(value, list):
        return [mask_sensitive_fields(item) for item in value]
    return value


# ---- 错误响应（统一 JSON 信封，与 app.py 异常处理器同构）----


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """中间件层错误响应（调用方 ``await resp(scope, receive, send)`` 发送）。"""
    """统一 JSON 错误信封：``{"error": {"code", "message"}}``。"""
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


# ---- ASGI send 包装：JSON 响应体打码 ----


class _MaskingSend:
    """ASGI ``send`` 包装：对 ``application/json`` 响应体执行敏感字段打码。

    非 JSON 响应（静态页等）原样透传；JSON 响应缓冲完整 body 后整体重写
    （同步更新 Content-Length），序列化参数与 Starlette ``JSONResponse``
    一致（``ensure_ascii=False, separators=(",", ":")``），打码无命中时
    字节级不变。
    """

    def __init__(self, send: Send) -> None:
        self._send = send
        self._start: Message | None = None
        self._chunks: list[bytes] = []
        self._json = False

    async def __call__(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            content_type = Headers(raw=list(message.get("headers", []))).get("content-type", "")
            lowered = content_type.lower()
            self._json = "application/json" in lowered or lowered.endswith("+json")
            if self._json:
                self._start = message  # 持有到 body 齐再发（需重写长度）
            else:
                await self._send(message)
            return
        if message["type"] == "http.response.body" and self._json:
            chunk = message.get("body", b"")
            if chunk:
                self._chunks.append(chunk)
            if message.get("more_body", False):
                return
            await self._flush()
            return
        await self._send(message)

    async def _flush(self) -> None:
        start = self._start
        assert start is not None  # http.response.body 必然先于 start 之后到达
        raw = b"".join(self._chunks)
        payload = self._masked_payload(raw)
        headers = [
            (key, val)
            for key, val in list(start.get("headers", []))
            if key.lower() != b"content-length"
        ]
        headers.append((b"content-length", str(len(payload)).encode("latin-1")))
        await self._send({**start, "headers": headers})
        await self._send({"type": "http.response.body", "body": payload})

    @staticmethod
    def _masked_payload(raw: bytes) -> bytes:
        """解析 → 打码 → 重序列化；任何解析/序列化异常时原样透传。"""
        try:
            data = json.loads(raw)
            masked = mask_sensitive_fields(data)
            return json.dumps(
                masked, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (ValueError, TypeError):
            return raw


# ---- 六项加固中间件（§10.1 第 2/3/6 项 + 第 5 项响应打码）----


class ConsoleHardeningMiddleware:
    """控制台请求/响应加固中间件（纯 ASGI；装配见 ``console.app.create_app``）。

    检查顺序（先外后内，命中即短路返回 JSON）：

    1. Host 校验（403 ``forbidden_host``）；
    2. OPTIONS 一律 405（``method_not_allowed``，不注册 CORSMiddleware 的
       配套：跨域预检在此终结，不会到达路由层）；
    3. Origin 校验（存在且非空时必须同源，否则 403 ``cross_origin``）；
    4. 非 GET/HEAD 要求 ``X-Job-Agent: console``（缺失 403
       ``console_header_required`` / 取值不符 403 ``console_header_invalid``）；
    5. 通过后交由内层应用处理，响应经 ``_MaskingSend`` 打码。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "")).upper()
        headers = Headers(scope=scope)
        host_header = headers.get("host", "")
        host_name, _port = split_host_port(host_header)
        if host_name not in ALLOWED_HOSTS:
            await error_response(403, "forbidden_host", f"非法 Host 头：{host_header or '(缺失)'}")(
                scope, receive, send
            )
            return
        if method == "OPTIONS":
            await error_response(405, "method_not_allowed", "预检请求一律拒绝（控制台不开放跨域）")(
                scope, receive, send
            )
            return
        origin = headers.get("origin")
        if origin is not None and origin != "":
            scheme = str(scope.get("scheme", "http"))
            if not is_same_origin(origin, host_header=host_header, scheme=scheme):
                await error_response(403, "cross_origin", f"跨域 Origin 拒绝：{origin}")(
                    scope, receive, send
                )
                return
        if method not in SAFE_METHODS:
            value = headers.get(CONSOLE_HEADER)
            if value is None:
                await error_response(
                    403,
                    "console_header_required",
                    "非 GET 请求必须携带 X-Job-Agent: console 头",
                )(scope, receive, send)
                return
            if value.strip() != CONSOLE_HEADER_VALUE:
                await error_response(
                    403, "console_header_invalid", "X-Job-Agent 头取值必须为 console"
                )(scope, receive, send)
                return
        await self.app(scope, receive, _MaskingSend(send))
