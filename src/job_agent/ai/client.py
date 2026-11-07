"""OpenAI 兼容 AI 客户端：统一调用入口 ``call_json``（任务书 S3a 第 1 条）。

流水线（每次 ``call_json``）：

1. **日预算熔断** [B13]：进程内计数器（评估+分类+生成合计——全模块共用
   同一 :class:`AIClient` 实例即天然合计）按北京自然日 [R6-4] 归零；
   计数 ≥ ``ai.daily_budget_calls`` 时抛 :class:`AIBudgetExhausted`，
   不发起请求；
2. 请求（openai SDK，base_url/api_key/model/temperature/proxy 从配置；
   超时见 :data:`DEFAULT_TIMEOUT_S`）；
3. 文本提取 JSON：markdown 代码块（```` ```json ````）优先，裸 JSON
   （平衡花括号扫描，容忍前后杂文）兜底；
4. Pydantic 校验目标 schema；
5. 2-3 步失败 → 指数退避重试 ≤2 次（共至多 3 次尝试，§14 重试上限）；
6. 仍失败：解析/校验问题 → :class:`AIUnparseableError`；请求层问题 →
   :class:`AITransportError`。

测试口径（TEST1 离线）：``transport`` 参数注入假实现，全程不发真实请求；
openai/httpx 仅在未注入 transport 时惰性导入。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar, cast

from pydantic import BaseModel, ValidationError

from job_agent.ai.errors import AIBudgetExhausted, AIError, AITransportError, AIUnparseableError
from job_agent.ai.types import ChatMessage
from job_agent.config import AIConfig
from job_agent.timeutil import beijing_date_str

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "EVENT_AI_BUDGET_EXHAUSTED",
    "MAX_RETRIES",
    "AIClient",
    "Transport",
    "call_json",
    "extract_json_candidates",
    "get_default_client",
    "parse_json_object",
    "set_default_client",
]

# §5.5 I5 / core.events.EVENT_LEVELS 成员拼写一致（ai 层不得 import core，
# 契约由 tests/unit/ai/test_client_budget.py 钉住）
EVENT_AI_BUDGET_EXHAUSTED = "AI_BUDGET_EXHAUSTED"

# 单次请求超时（秒）。AIConfig（§4）未含超时字段，此处为代码级默认；
# AIClient(timeout_s=...) 可覆盖，S5 控制台「AI 连通性自检」接线时如需
# 配置化再扩展 §4（当前任务书口径：超时按配置，默认值在此集中定义）。
DEFAULT_TIMEOUT_S = 60.0

# 解析/校验失败的重试上限（§14：失败重试上限；总尝试 = 1 + 2 = 3）
MAX_RETRIES = 2

# 假传输函数：接收消息序列，返回模型输出文本（测试注入点，TEST1 离线）
Transport = Callable[[Sequence[ChatMessage]], str]

_ModelT = TypeVar("_ModelT", bound=BaseModel)

# ```json ... ``` / ``` ... ``` 围栏（首尾空白容忍，语言标记可缺省）
_FENCE_RE = re.compile(r"```[ \t]*(?:json)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)


# ---- JSON 提取（纯函数，供单测直接覆盖） ----


def _iter_balanced_objects(text: str) -> list[str]:
    """从任意杂文中提取全部顶层平衡的 ``{...}`` 子串（按出现序）。

    扫描时跟踪字符串字面量与转义，因此 JSON 值内的 ``{``/``}``/引号
    不会提前截断；未闭合的对象（截断输出）直接丢弃。
    """
    objects: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                objects.append(text[start : i + 1])
                start = -1
    return objects


def extract_json_candidates(text: str) -> list[str]:
    """按优先级产出候选 JSON 文本：全部代码块（出现序）→ 裸 JSON 平衡扫描。

    兜底扫描针对全文（不剔除代码块），可覆盖「代码块内是解释文字、
    真正 JSON 在块外」与「前后杂文 + 裸 JSON」两类形态。
    """
    candidates: list[str] = []
    for match in _FENCE_RE.finditer(text):
        block = match.group(1).strip()
        if block:
            candidates.append(block)
    candidates.extend(_iter_balanced_objects(text))
    return candidates


def parse_json_object(text: str) -> dict[str, Any]:
    """从模型输出文本中解析出首个合法的 JSON 对象（代码块/裸 JSON 兜底）。

    顶层数组退化语义：平衡扫描作用于花括号对象，``[{"a":1},{"b":2}]``
    会提取其中的首个对象（对"数组包裹对象"的常见模型输出取首个，仍需
    通过 schema 校验）；全文无任何合法对象（纯散文/标量/纯数组）时抛
    :class:`AIUnparseableError`。
    """
    for candidate in extract_json_candidates(text):
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    snippet = text.strip()[:200]
    raise AIUnparseableError(f"无法从模型输出中提取 JSON 对象，输出片段：{snippet!r}")


# ---- 默认传输（openai SDK，惰性导入） ----


def _default_transport(config: AIConfig, timeout_s: float) -> Transport:
    """按配置构建 openai SDK 传输（base_url/api_key/model/temperature/proxy）。"""
    import httpx
    from openai import OpenAI

    http_client: httpx.Client | None = None
    if config.proxy:
        http_client = httpx.Client(proxy=config.proxy, timeout=timeout_s)
    # openai SDK 的 vendored httpx 类型标注与独立 httpx 包不同源（httpx2.*），
    # 运行时同构，类型层以 Any 桥接（构造参数仅接受 Client 实例或 None）
    client = OpenAI(
        base_url=config.base_url or None,
        api_key=config.api_key or None,  # 留空回落 OPENAI_API_KEY 环境变量
        timeout=timeout_s,
        http_client=cast(Any, http_client),
    )

    def transport(messages: Sequence[ChatMessage]) -> str:
        # ChatMessage 与 SDK 消息参数同为 {role, content} 字典结构，
        # 以 Any 列表传入（值类型在 ChatMessage 上已约束）
        payload: list[Any] = [{"role": m["role"], "content": m["content"]} for m in messages]
        response = client.chat.completions.create(
            model=config.model,
            messages=payload,
            temperature=config.temperature,
        )
        content = response.choices[0].message.content
        return content if content is not None else ""

    return transport


# ---- 客户端 ----


class AIClient:
    """带日预算与重试的统一调用入口（进程内共享以合计评估+分类+生成 [B13]）。

    参数（除 config 外均为依赖注入点，测试传假实现离线运行 TEST1）：

    - ``transport``：假传输函数，缺省按配置构建 openai SDK 传输；
    - ``timeout_s`` / ``backoff_base_s`` / ``sleep`` / ``now_fn``：超时秒数、
      退避基数（第 n 次重试睡 ``base * 2**n``）、睡眠函数与时钟。
    """

    def __init__(
        self,
        config: AIConfig,
        *,
        transport: Transport | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        backoff_base_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._timeout_s = timeout_s
        self._backoff_base_s = backoff_base_s
        self._sleep = sleep
        self._now_fn = now_fn
        self._on_event = on_event
        self._calls_today = 0
        self._budget_date = beijing_date_str(now_fn())
        self._notified_exhausted = False
        if transport is not None:
            self._transport = transport
        else:
            try:
                self._transport = _default_transport(config, timeout_s)
            except Exception as exc:  # SDK 构建期错误（缺 key/代理非法等）
                raise AITransportError(f"AI 客户端初始化失败：{exc}") from exc

    # ---- 预算 ----

    @property
    def calls_today(self) -> int:
        """当日（北京自然日）已发起的 API 请求数（重试的每次尝试各计 1）。"""
        return self._calls_today

    def _reserve_budget(self) -> None:
        """占用一次调用额度：计数 ≥ 日预算则熔断（不发起请求）[B13]。"""
        today = beijing_date_str(self._now_fn())
        if today != self._budget_date:
            self._budget_date = today
            self._calls_today = 0
            self._notified_exhausted = False
        if self._calls_today >= self._config.daily_budget_calls:
            if not self._notified_exhausted and self._on_event is not None:
                self._on_event(EVENT_AI_BUDGET_EXHAUSTED)
            self._notified_exhausted = True
            raise AIBudgetExhausted(
                f"当日 AI 调用已达预算上限 {self._config.daily_budget_calls} 次（B13），"
                "评估与自动回复今日停摆，存量草拟转人工"
            )
        self._calls_today += 1

    # ---- 统一入口 ----

    def call_json(self, messages: Sequence[ChatMessage], schema: type[_ModelT]) -> _ModelT:
        """请求 → JSON 提取 → Pydantic 校验 → 重试 ≤2 → 返回 schema 实例。

        - 每次**尝试**前先过预算熔断（重试也消耗额度，防重试循环打爆预算）；
        - 重试间指数退避：``backoff_base_s * 2**attempt``（1 次 base、2 次 2×base）；
        - 解析/校验耗尽 → :class:`AIUnparseableError`；请求层耗尽 →
          :class:`AITransportError`；预算耗尽 → :class:`AIBudgetExhausted`。
        """
        last_failure: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            self._reserve_budget()
            try:
                text = self._transport(messages)
            except AIError:
                raise  # 传输层不该抛 AI 异常；万一出现，按显式信号透传
            except Exception as exc:  # 网络/超时/认证等请求层错误
                last_failure = exc
            else:
                try:
                    return self._validate(text, schema)
                except AIUnparseableError as exc:
                    last_failure = exc
            if attempt < MAX_RETRIES:
                self._sleep(self._backoff_base_s * (2**attempt))
        if isinstance(last_failure, AIUnparseableError):
            raise AIUnparseableError(
                f"模型输出经 {MAX_RETRIES} 次重试仍无法解析为 {schema.__name__}：{last_failure}"
            ) from last_failure
        assert last_failure is not None  # 循环至少执行一次
        raise AITransportError(
            f"AI 请求经 {MAX_RETRIES} 次重试仍失败：{last_failure}"
        ) from last_failure

    def _validate(self, text: str, schema: type[_ModelT]) -> _ModelT:
        """提取 + Pydantic 校验（校验失败视同解析失败，交由上层重试）。"""
        obj = parse_json_object(text)
        try:
            return schema.model_validate(obj)
        except ValidationError as exc:
            raise AIUnparseableError(
                f"模型输出未通过 {schema.__name__} 校验：{str(exc).splitlines()[0]}"
            ) from exc


# ---- 进程级默认客户端（S4c/S4d 装配与 evaluate_job 缺省使用） ----

_default_client: AIClient | None = None


def set_default_client(client: AIClient | None) -> None:
    """登记/清除进程级默认客户端（引擎装配与测试注入点）。

    清除（传 None）后再次获取将从当前 config.yaml 重建。
    """
    global _default_client
    _default_client = client


def get_default_client() -> AIClient:
    """取默认客户端：未登记时按当前配置惰性构建（预算计数随实例存活）。"""
    global _default_client
    if _default_client is None:
        from job_agent.config import load_config

        _default_client = AIClient(load_config().ai)
    return _default_client


def call_json[ModelT: BaseModel](messages: Sequence[ChatMessage], schema: type[ModelT]) -> ModelT:
    """模块级统一入口（任务书 S3a 第 1 条签名）：委托进程级默认客户端。

    供 S3b（classify/reply）/S3c（greeting/简历结构化）直接调用——共享
    同一默认客户端实例即共享日预算计数（评估+分类+生成合计 [B13]）；
    引擎装配（S4d/S5）负责 ``set_default_client`` 注入带 on_event 钩子的
    实例。需要独立预算/传输时自行实例化 :class:`AIClient`。
    """
    return get_default_client().call_json(messages, schema)
