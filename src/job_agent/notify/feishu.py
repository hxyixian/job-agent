"""飞书群机器人 Webhook 通知渠道（§11；FR7.1 主通道）。

- **签名**（飞书自定义机器人官方算法）：以 ``f"{timestamp}\\n{secret}"`` 为
  HMAC-SHA256 的**密钥**、空串为消息计算摘要再 Base64——注意密钥与消息的
  位置（timestamp 拼进 key，不是被签名的正文）。secret 为空 = 未启用签名
  （Webhook 创建时可选），请求不带 timestamp/sign 字段。
- **级别过滤**：``feishu.levels``（§4；出厂 [high, medium]，配置层已校验
  必含 high [R6-1]），``applies(level)`` 即 ``level in levels``。
- **HTTP**：httpx 同步客户端。httpx 虽不在 [project].dependencies 中显式
  声明，但主依赖 openai（SDK）硬依赖 httpx，运行环境必然存在；客户端可
  注入（测试/替换传输层）。超时 10s，非 2xx 或飞书返回 ``code != 0``
  视为失败抛异常（由管线记 error 状态，不阻断业务）。
- **消息形态**：``msg_type=text``；正文 = ``[job-agent][级别] 事件类型`` +
  payload JSON（截断至 800 字符，防超长 webhook 拒收）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import ClassVar

import httpx

from job_agent.config import FeishuConfig
from job_agent.notify.base import EventRecord, Level, Notifier

__all__ = ["FeishuNotifier", "format_event_text", "gen_sign"]

_TIMEOUT_S = 10.0
# payload JSON 在推送正文中的最大长度（超长截断，防飞书 webhook 拒收）
_MAX_PAYLOAD_CHARS = 800


def gen_sign(timestamp: str, secret: str) -> str:
    """飞书自定义机器人签名（官方算法）：HMAC-SHA256(key="{timestamp}\\n{secret}") → Base64。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def format_event_text(record: EventRecord) -> str:
    """推送正文：级别/事件类型头 + payload JSON（截断）。"""
    body = json.dumps(record.payload, ensure_ascii=False, sort_keys=True)
    if len(body) > _MAX_PAYLOAD_CHARS:
        body = body[:_MAX_PAYLOAD_CHARS] + "…（截断）"
    return f"[job-agent][{record.level}] {record.event_type}\n{body}"


class FeishuNotifier(Notifier):
    """飞书 Webhook 渠道（levels 过滤 + 签名投递）。"""

    name: ClassVar[str] = "feishu"

    def __init__(self, config: FeishuConfig, *, client: httpx.Client | None = None) -> None:
        self._config = config
        self._client = client
        self._owns_client = False  # 自建客户端时置 True（注入方自理生命周期）

    def _get_client(self) -> httpx.Client:
        """惰性创建共享客户端（未注入时自建，close() 统一释放）。"""
        if self._client is None:
            self._client = httpx.Client(timeout=_TIMEOUT_S)
            self._owns_client = True
        return self._client

    @property
    def configured(self) -> bool:
        return bool(self._config.webhook)

    def applies(self, level: Level) -> bool:
        return level in self._config.levels

    def send(self, record: EventRecord) -> None:
        if not self._config.webhook:
            return  # 防御：configured=False 时管线不会调用（双保险）
        payload: dict[str, object] = {
            "msg_type": "text",
            "content": {"text": format_event_text(record)},
        }
        if self._config.secret:
            timestamp = str(int(datetime.now(UTC).timestamp()))
            payload["timestamp"] = timestamp
            payload["sign"] = gen_sign(timestamp, self._config.secret)
        response = self._get_client().post(self._config.webhook, json=payload)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            return  # 2xx 且非 JSON（非标准端点/测试桩）——按已送达处理
        code = data.get("code", 0)
        if code != 0:
            raise RuntimeError(f"飞书 webhook 返回错误码 {code}：{data.get('msg', '')}")

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
