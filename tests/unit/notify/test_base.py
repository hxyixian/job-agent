"""S3d notify/base：EventType 全枚举 + 级别权威表（§5.5 I5）+ Notifier 接口。"""

from __future__ import annotations

import pytest

from job_agent.notify import EVENT_LEVELS, EventType, Level, Notifier, event_name, level_of

# §5.5 I5 权威表（RAMPUP_DONE=low，裁决 2026-11-07）
_I5_EXPECTED: dict[str, Level] = {
    # high
    "LOGIN_EXPIRED": "high",
    "RISK_PAUSE": "high",
    "ADAPTER_BROKEN": "high",
    "OUTBOX_TIMEOUT": "high",
    "BROWSER_LOST": "high",
    "PAGE_EVICTED": "high",
    "SECURITY_CHECK": "high",
    # medium
    "INTERVIEW_INVITE": "medium",
    "KEY_QUESTION": "medium",
    "OFFER_SIGNAL": "medium",
    "CONV_LIMIT_REACHED": "medium",
    "MULTIMODAL_PENDING": "medium",
    "CONTACT_REQUEST": "medium",
    "PLATFORM_LIMIT": "medium",
    "AI_BUDGET_EXHAUSTED": "medium",
    "CHROME_UPGRADED": "medium",
    "GREETING_MISMATCH": "medium",
    "BROWSE_LIMIT_REACHED": "medium",
    # low
    "DAILY_DIGEST": "low",
    "DRYRUN_DATA_READY": "low",
    "RAMPUP_DONE": "low",
}


def test_event_type_full_enumeration_matches_i5() -> None:
    """枚举成员 = §5.5 I5 全表（21 个），值 = 成员名（落库字符串）。"""
    members = {member.value for member in EventType}
    assert members == set(_I5_EXPECTED)
    assert len(EventType) == 21
    for member in EventType:
        assert member.value == member.name  # 值与成员名一致（DB 字符串契约）


def test_event_levels_authoritative_table() -> None:
    """EVENT_LEVELS 与 I5 权威表逐条一致（含 RAMPUP_DONE=low 裁决）。"""
    assert EVENT_LEVELS == _I5_EXPECTED
    assert EVENT_LEVELS["RAMPUP_DONE"] == "low"


@pytest.mark.parametrize(
    ("event", "expected"),
    sorted(_I5_EXPECTED.items()),
)
def test_level_of_enum_and_string(event: str, expected: Level) -> None:
    assert level_of(EventType(event)) == expected
    assert level_of(event) == expected
    assert event_name(event) == event
    assert event_name(EventType(event)) == event


def test_level_of_unknown_rejected() -> None:
    with pytest.raises(ValueError, match="未知事件类型"):
        level_of("NOT_AN_EVENT")
    with pytest.raises(ValueError, match="未知事件类型"):
        event_name("rampup_done")  # 大小写敏感：小写不是合法成员


def test_notifier_interface_is_abstract() -> None:
    with pytest.raises(TypeError):
        Notifier()  # type: ignore[abstract]

    class _Minimal(Notifier):
        name = "minimal"

        def applies(self, level: Level) -> bool:
            return True

        def send(self, record: object) -> None:  # type: ignore[override]
            return None

    plugin = _Minimal()
    assert plugin.name == "minimal"
    assert plugin.configured is True  # 默认已配置
    assert plugin.applies("low") is True
