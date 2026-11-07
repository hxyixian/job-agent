"""S4a adapters/base 契约测试（§5.2：异常树/11 方法抽象/连续失败计数/注册表）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from job_agent.adapters import base
from job_agent.adapters.base import (
    DEFAULT_BROKEN_THRESHOLD,
    AdapterBrokenError,
    AdapterError,
    LoginRequiredError,
    PlatformLimitError,
    RiskDetectedError,
    SearchFilters,
    SiteAdapter,
    TransientError,
    UnknownAdapterError,
    create_adapter,
    register_adapter,
    registered_adapters,
)
from job_agent.models import (
    Conversation,
    JobDetail,
    JobRef,
    LoginState,
    Message,
    RiskSignal,
    SendOutcome,
)


class RecordingAdapter(SiteAdapter):
    """最小可实例化实现（记录 create_adapter 转发的构造参数）。"""

    site_id = "fake"

    def __init__(
        self, *args: Any, broken_threshold: int = DEFAULT_BROKEN_THRESHOLD, **kwargs: Any
    ) -> None:
        super().__init__(broken_threshold=broken_threshold)
        self.init_args: tuple[tuple[Any, ...], dict[str, Any]] = (args, kwargs)

    def check_login_state(self) -> LoginState:
        return LoginState.LOGGED_IN

    def detect_risk(self) -> RiskSignal:
        return RiskSignal.NONE

    def search_jobs(self, filters: SearchFilters, page: int) -> list[JobRef]:
        return []

    def get_job_detail(self, job_id: str) -> JobDetail:
        raise AdapterError(job_id)

    def send_greeting(self, job: JobRef, message: str) -> SendOutcome:
        return SendOutcome.FAILED

    def send_reply(self, conv_id: str, message: str) -> SendOutcome:
        return SendOutcome.FAILED

    def send_resume(self, conv_id: str) -> SendOutcome:
        return SendOutcome.FAILED

    def list_conversations(self) -> list[Conversation]:
        return []

    def get_messages(self, conv_id: str, since: datetime | None) -> list[Message]:
        return []

    def get_conversation_job(self, conv_id: str) -> JobRef | None:
        return None

    def snapshot_conversation_baseline(self) -> int:
        return 0


class MismatchedAdapter(RecordingAdapter):
    site_id = "other"


@pytest.fixture()
def registry_sandbox() -> Any:
    """注册表沙盒：测试内注册不泄漏到其它测试（mock 等既有注册保留）。"""
    saved = dict(base._REGISTRY)
    yield base._REGISTRY
    base._REGISTRY.clear()
    base._REGISTRY.update(saved)


# ---- 异常树（§5.2 六类 + 调用方行为表的存在性由类契约承载）----


def test_exception_tree_six_classes() -> None:
    for exc_cls in (
        LoginRequiredError,
        RiskDetectedError,
        AdapterBrokenError,
        PlatformLimitError,
        TransientError,
    ):
        assert issubclass(exc_cls, AdapterError)
    assert issubclass(AdapterError, Exception)
    assert issubclass(UnknownAdapterError, ValueError)


def test_risk_detected_error_carries_signal() -> None:
    err = RiskDetectedError(RiskSignal.EVICTED, "URL→about:blank")
    assert err.signal is RiskSignal.EVICTED
    assert "EVICTED" in str(err)
    err2 = RiskDetectedError(RiskSignal.SECURITY_CHECK)
    assert err2.signal is RiskSignal.SECURITY_CHECK
    assert err2.detail == ""


def test_search_filters_defaults() -> None:
    filters = SearchFilters()
    assert filters.keywords == []
    assert filters.city == ""


# ---- 抽象基类 ----


def test_cannot_instantiate_abstract_site_adapter() -> None:
    with pytest.raises(TypeError):
        SiteAdapter()  # type: ignore[abstract]


def test_abstract_method_surface_is_exactly_eleven() -> None:
    expected = {
        "check_login_state",
        "detect_risk",
        "search_jobs",
        "get_job_detail",
        "send_greeting",
        "send_reply",
        "send_resume",
        "list_conversations",
        "get_messages",
        "get_conversation_job",
        "snapshot_conversation_baseline",
    }
    assert set(SiteAdapter.__abstractmethods__) == expected
    assert len(expected) == 11


# ---- 连续定位失败计数（AdapterBrokenError 判定 [B7/H1]）----


def test_consecutive_failure_default_threshold_three() -> None:
    adapter = RecordingAdapter()
    adapter.note_element_failure("职位卡")
    adapter.note_element_failure("JD 正文")
    assert adapter.consecutive_element_failures == 2
    with pytest.raises(AdapterBrokenError):
        adapter.note_element_failure("会话列表")
    assert adapter.consecutive_element_failures >= 3


def test_consecutive_failure_stays_broken_until_success_or_reset() -> None:
    adapter = RecordingAdapter()
    adapter.note_element_failure("a")
    adapter.note_element_failure("b")
    with pytest.raises(AdapterBrokenError):
        adapter.note_element_failure("c")  # 第 3 次熔断
    # 已达阈值：后续任何失败立即再抛（熔断态保持）
    with pytest.raises(AdapterBrokenError):
        adapter.note_element_failure("d")
    adapter.reset_element_failures()
    assert adapter.consecutive_element_failures == 0
    adapter.note_element_failure("e")  # 重新从 1 计
    assert adapter.consecutive_element_failures == 1


def test_consecutive_failure_reset_by_success() -> None:
    adapter = RecordingAdapter()
    adapter.note_element_failure("a")
    adapter.note_element_failure("b")
    adapter.note_element_success()
    assert adapter.consecutive_element_failures == 0
    # 成功归零后需重新连续计满 3 次才熔断
    adapter.note_element_failure("c")
    adapter.note_element_failure("d")
    with pytest.raises(AdapterBrokenError):
        adapter.note_element_failure("e")


def test_broken_threshold_configurable_and_validated() -> None:
    adapter = RecordingAdapter(broken_threshold=1)
    with pytest.raises(AdapterBrokenError):
        adapter.note_element_failure("first")
    with pytest.raises(ValueError):
        RecordingAdapter(broken_threshold=0)


# ---- 注册表（Mock 与 ZhipinAdapter 同机制）----


def test_register_and_create_roundtrip(registry_sandbox: Any) -> None:
    register_adapter("fake", RecordingAdapter)
    adapter = create_adapter("fake", 1, flag="x")
    assert isinstance(adapter, RecordingAdapter)
    assert adapter.init_args == ((1,), {"flag": "x"})


def test_register_idempotent_for_same_class(registry_sandbox: Any) -> None:
    register_adapter("fake", RecordingAdapter)
    register_adapter("fake", RecordingAdapter)  # 同类重复注册无副作用
    assert isinstance(create_adapter("fake"), RecordingAdapter)


def test_register_conflict_rejected(registry_sandbox: Any) -> None:
    register_adapter("fake", RecordingAdapter)

    class AnotherFake(RecordingAdapter):
        pass

    with pytest.raises(ValueError, match="拒绝覆盖"):
        register_adapter("fake", AnotherFake)


def test_register_site_id_mismatch_rejected(registry_sandbox: Any) -> None:
    with pytest.raises(ValueError, match="site_id 不一致"):
        register_adapter("fake2", MismatchedAdapter)


def test_register_non_subclass_rejected(registry_sandbox: Any) -> None:
    with pytest.raises(TypeError):
        register_adapter("notanadapter", object)  # type: ignore[arg-type]


def test_register_empty_site_id_rejected(registry_sandbox: Any) -> None:
    with pytest.raises(ValueError, match="site_id 不能为空"):
        register_adapter("  ", RecordingAdapter)


def test_create_unknown_site_id(registry_sandbox: Any) -> None:
    with pytest.raises(UnknownAdapterError, match="no-such"):
        create_adapter("no-such")


def test_registered_adapters_sorted_and_contains_mock(registry_sandbox: Any) -> None:
    register_adapter("fake", RecordingAdapter)
    listed = registered_adapters()
    assert listed == sorted(listed)
    assert "mock" in listed  # adapters 包导入即注册
    assert "fake" in listed


def test_mock_and_other_adapters_dispatch_independently(registry_sandbox: Any) -> None:
    from job_agent.adapters.mock import MockSiteAdapter

    register_adapter("fake", RecordingAdapter)
    assert isinstance(create_adapter("fake"), RecordingAdapter)
    assert isinstance(create_adapter("mock"), MockSiteAdapter)
