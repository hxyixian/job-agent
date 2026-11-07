"""logging_setup 单元测试：轮转配置 / 分级路由 / 打码过滤器 / 简历拦截。

全部用例使用 ``tmp_path`` 作为日志目录，绝不污染真实 ``logs/``。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from job_agent.logging_setup import (
    BACKUP_COUNT,
    MAX_BYTES,
    RESUME_KEYWORD_MIN_HITS,
    RESUME_TEXT_LENGTH_THRESHOLD,
    SanitizingFilter,
    looks_like_resume_text,
    mask_secret_value,
    mask_secrets,
    setup_logging,
    shutdown_logging,
)

_LOGGER_NAME = "job_agent.selftest"


@pytest.fixture()
def logs_dir(tmp_path: Path) -> Path:
    setup_logging(tmp_path, console=False)  # 静音控制台，避免测试输出噪声
    yield tmp_path
    shutdown_logging()


def _flush_handlers() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def _read(path: Path) -> str:
    _flush_handlers()
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 装配与轮转配置
# ---------------------------------------------------------------------------


def test_setup_creates_log_files_and_rotation_handlers(logs_dir: Path) -> None:
    assert (logs_dir / "app.log").exists()
    assert (logs_dir / "error.log").exists()

    rotating = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
    assert len(rotating) == 2

    by_name = {Path(h.baseFilename).name: h for h in rotating}
    app_handler = by_name["app.log"]
    error_handler = by_name["error.log"]

    assert app_handler.maxBytes == 10 * 1024 * 1024 == MAX_BYTES
    assert app_handler.backupCount == 7 == BACKUP_COUNT
    assert app_handler.level == logging.INFO
    assert error_handler.maxBytes == MAX_BYTES
    assert error_handler.backupCount == BACKUP_COUNT
    assert error_handler.level == logging.ERROR


def test_console_handler_attached_and_can_be_disabled(tmp_path: Path) -> None:
    try:
        setup_logging(tmp_path)
        streams = [
            h
            for h in logging.getLogger().handlers
            if type(h) is logging.StreamHandler  # 精确类型，排除 RotatingFileHandler 子类
        ]
        assert len(streams) == 1
        assert streams[0].level == logging.INFO

        setup_logging(tmp_path, console=False)
        streams = [h for h in logging.getLogger().handlers if type(h) is logging.StreamHandler]
        assert streams == []
    finally:
        shutdown_logging()


def test_setup_is_idempotent(tmp_path: Path) -> None:
    try:
        setup_logging(tmp_path)
        # 只统计本模块装配的 handler（pytest 等宿主会向 root 追加自己的 handler）
        first = _managed_handlers()
        setup_logging(tmp_path)
        second = _managed_handlers()
        assert len(first) == len(second) == 3  # app.log + error.log + console

        logging.getLogger(_LOGGER_NAME).info("idempotent-probe")
        content = _read(tmp_path / "app.log")
        assert content.count("idempotent-probe") == 1
    finally:
        shutdown_logging()


def _managed_handlers() -> list[logging.Handler]:
    return [
        h for h in logging.getLogger().handlers if getattr(h, "_job_agent_managed_handler", False)
    ]


def test_shutdown_removes_managed_handlers(logs_dir: Path) -> None:
    shutdown_logging()
    managed = [
        h for h in logging.getLogger().handlers if getattr(h, "_job_agent_managed_handler", False)
    ]
    assert managed == []


# ---------------------------------------------------------------------------
# 分级路由
# ---------------------------------------------------------------------------


def test_level_routing_between_app_and_error_logs(logs_dir: Path) -> None:
    logger = logging.getLogger(_LOGGER_NAME)
    logger.info("routing-info-message")
    logger.warning("routing-warning-message")
    logger.error("routing-error-message")

    app = _read(logs_dir / "app.log")
    error = _read(logs_dir / "error.log")

    assert "routing-info-message" in app
    assert "routing-warning-message" in app
    assert "routing-error-message" in app
    assert "routing-error-message" in error
    assert "routing-info-message" not in error
    assert "routing-warning-message" not in error


# ---------------------------------------------------------------------------
# 打码过滤器：各敏感形态（纯函数层）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "forbidden", "expected"),
    [
        # 1. YAML/环境变量形态：api_key=sk-xxx（尾 4 位保留）
        ("api_key=sk-abcdefgh1234", "sk-abcdefgh1234", "api_key=****1234"),
        # 2. JSON 形态：带引号的键与值
        (
            'config {"api_key": "sk-1234567890abcdef"}',
            "sk-1234567890abcdef",
            '{"api_key": "****cdef"}',
        ),
        # 3. 冒号 + 空格（日志式键值）
        ("api_key: sk-abcdefgh1234", "sk-abcdefgh1234", "api_key: ****1234"),
        # 4. Authorization: Bearer <token>
        (
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJpYXQiOjF9.SIGNATURE",
            "eyJhbGciOiJIUzI1NiJ9",
            "Bearer ****TURE",
        ),
        # 5. Basic 凭据（尾 4 位含 base64 填充符）
        (
            "Proxy-Authorization: Basic dXNlcjpwYXNzd29yZA==",
            "dXNlcjpwYXNzd29yZA==",
            "Basic ****ZA==",
        ),
        # 6. 飞书 webhook URL 中的 hook 令牌
        (
            "webhook=https://open.feishu.cn/open-apis/bot/v2/hook/4f3e2d1c0b9a8f7e6d5c4b3",
            "4f3e2d1c0b9a8f7e6d5c4b3",
            "webhook=****c4b3",
        ),
        # 7. webhook secret（冒号形态）
        ("feishu secret: v3f8Cq2mNpQrStUvWx", "v3f8Cq2mNpQrStUvWx", "secret: ****UvWx"),
        # 8. SMTP password（下划线键）
        ("smtp_password=Hunter2Secret9", "Hunter2Secret9", "smtp_password=****ret9"),
        # 9. 中文键 + 中文冒号：密码：xxx
        ("SMTP 密码：P@ssw0rd!12345", "P@ssw0rd!12345", "密码：****2345"),
        # 10. 裸 sk- 密钥出现在自由文本中
        (
            "AI 调用失败，key 为 sk-1234567890abcdef 已过期",
            "sk-1234567890abcdef",
            "key 为 ****cdef",
        ),
        # 11. 短敏感值完全打码（不保留尾 4 位，避免泄露）
        ("password=abc123", "abc123", "password=****"),
        # 12. 引号内含空格的密码（"P@ss word99" 尾 4 位 = "rd99"）
        ("password: 'P@ss word99'", "P@ss word99", "password: '****rd99'"),
    ],
)
def test_mask_secrets_covers_sensitive_forms(message: str, forbidden: str, expected: str) -> None:
    masked = mask_secrets(message)
    assert forbidden not in masked
    assert expected in masked


@pytest.mark.parametrize(
    "message",
    [
        "job_id=1024 company_key=aliyun-01 hourly=25",
        "token bucket 限流算法自检通过",
        "今日投递 25 单，浏览详情 40 次，限额 100",
        "state=pending kind=greeting dry_run=true",
    ],
)
def test_mask_secrets_keeps_normal_messages(message: str) -> None:
    assert mask_secrets(message) == message


def test_mask_secrets_is_idempotent() -> None:
    message = (
        "api_key=sk-abcdefgh1234 与 webhook=hook/4f3e2d1c0b9a "
        "和 Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.sig"
    )
    once = mask_secrets(message)
    assert mask_secrets(once) == once


def test_mask_secret_value_tail_policy() -> None:
    # 长值：**** + 尾 4 位
    assert mask_secret_value("sk-abcdefgh1234") == "****1234"
    # 短值（<8）：完全打码
    assert mask_secret_value("abc123") == "****"
    assert mask_secret_value("") == "****"


# ---------------------------------------------------------------------------
# 打码过滤器：端到端（handler 层）
# ---------------------------------------------------------------------------


def test_end_to_end_masking_in_log_files(logs_dir: Path) -> None:
    logging.getLogger(_LOGGER_NAME).warning("AI 请求失败 api_key=sk-abcdefgh1234 返回 401")
    app = _read(logs_dir / "app.log")
    assert "sk-abcdefgh1234" not in app
    assert "api_key=****1234" in app
    # WARNING 不进 error.log
    assert "401" not in _read(logs_dir / "error.log")


def test_end_to_end_masking_with_lazy_args(logs_dir: Path) -> None:
    webhook_url = (
        "notify.feishu.webhook=https://open.feishu.cn/open-apis/bot/v2/hook/9999aaaabbbbcccc"
    )
    logging.getLogger(_LOGGER_NAME).info("加载配置 %s", webhook_url)
    app = _read(logs_dir / "app.log")
    assert "9999aaaabbbbcccc" not in app
    assert "****" in app


def test_sanitizing_filter_masks_record_in_place() -> None:
    record = logging.LogRecord(
        _LOGGER_NAME,
        logging.ERROR,
        __file__,
        1,
        "password=SuperSecret9876",
        None,
        None,
    )
    assert SanitizingFilter().filter(record) is True
    assert record.getMessage() == "password=****9876"


# ---------------------------------------------------------------------------
# 简历全文拦截（长度阈值 + 关键词）
# ---------------------------------------------------------------------------


def _resume_like_text(fill: int = RESUME_TEXT_LENGTH_THRESHOLD + 200) -> str:
    sections = [
        "个人信息：张三，男，本科，138-0000-0000",
        "教育经历：某大学 计算机科学与技术 本科（2016-2020）",
        "工作经历：某科技公司 后端工程师（2020-2024）",
        "项目经历：电商中台重构、支付网关",
        "专业技能：Python / FastAPI / SQLite",
    ]
    body = "\n".join(sections)
    return body + "。" * max(fill - len(body), 1)


def test_looks_like_resume_text_rules() -> None:
    long_resume = _resume_like_text()
    assert len(long_resume) >= RESUME_TEXT_LENGTH_THRESHOLD
    assert looks_like_resume_text(long_resume) is True

    # 低于长度阈值：不拦
    short = "教育经历 工作经历 项目经历 专业技能" + "简" * 100
    assert len(short) < RESUME_TEXT_LENGTH_THRESHOLD
    assert looks_like_resume_text(short) is False

    # 长文本但无简历关键词：不拦（如 JD/普通日志）
    no_keyword = "今天天气不错。" * 200
    assert looks_like_resume_text(no_keyword) is False

    # 长文本但只命中 1 个关键词：不拦（避免误拦长 JD）
    single_keyword = "工作经历要求五年以上。" * 60
    assert len(single_keyword) >= RESUME_TEXT_LENGTH_THRESHOLD
    assert single_keyword.count("工作经历") > 0
    hits = sum(1 for kw in ("教育经历", "项目经历", "专业技能") if kw in single_keyword)
    assert hits < RESUME_KEYWORD_MIN_HITS
    assert looks_like_resume_text(single_keyword) is False


def test_resume_text_blocked_end_to_end(logs_dir: Path) -> None:
    resume = _resume_like_text()
    assert "138-0000-0000" in resume  # 前提：样例确实包含敏感原文

    logging.getLogger(_LOGGER_NAME).info(resume)
    app = _read(logs_dir / "app.log")

    # 原文与敏感内容均未落盘
    assert "教育经历" not in app
    assert "138-0000-0000" not in app
    assert "电商中台" not in app
    # 占位符 + 告警各出现一次
    assert app.count("[S3拦截] 疑似简历全文未写入日志") == 1
    assert app.count("简历全文禁止落日志") == 1
    # error.log 不受影响（原级别是 INFO，告警为 WARNING）
    assert "教育经历" not in _read(logs_dir / "error.log")


def test_resume_block_does_not_fire_for_short_messages(logs_dir: Path) -> None:
    logging.getLogger(_LOGGER_NAME).info("求职意向：后端工程师岗位（短消息不应拦截）")
    app = _read(logs_dir / "app.log")
    assert "求职意向" in app
    assert "[S3拦截]" not in app
