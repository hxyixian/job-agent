"""分级日志初始化：app.log（INFO，10MB×7 轮转）+ error.log（ERROR+）+ 控制台输出。

设计依据：设计文档 §3 末尾日志规范（[S3]）与开发文档 Stage1 SubStage S1c。

- 打码过滤器 ``SanitizingFilter``：消息中的 api_key / webhook secret / SMTP password /
  Bearer·Basic 凭据 / 裸 ``sk-`` 开头密钥等敏感值一律替换为 ``****`` + 尾 4 位
  （短于 8 个字符的值完全打码为 ``****``，避免尾 4 位泄露过多）；
- 简历全文禁止落日志 [S3]：长度达到阈值且命中足量简历特征关键词 → 原文替换为
  拦截占位符，同时额外记一条 WARNING 告警（“长度阈值 + 关键词拦截”）；
- 过滤器挂在 handler 层（而非某个 logger），对包括第三方库在内的全部记录生效；
- 仅使用标准库 logging，不引入第三方日志库。

已知边界：``exc_info`` 堆栈文本与 ``LogRecord`` 附加字段不参与打码，调用方不得把
敏感值放进异常参数或结构化字段；本模块无 import 副作用，测试应显式传入临时目录。
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = [
    "APP_LOG_NAME",
    "BACKUP_COUNT",
    "ERROR_LOG_NAME",
    "LOG_FORMAT",
    "MASK_PREFIX",
    "MASK_TAIL_KEEP",
    "MIN_SECRET_LENGTH_FOR_TAIL",
    "RESUME_KEYWORDS",
    "RESUME_KEYWORD_MIN_HITS",
    "RESUME_TEXT_LENGTH_THRESHOLD",
    "SanitizingFilter",
    "looks_like_resume_text",
    "mask_secret_value",
    "mask_secrets",
    "setup_logging",
    "shutdown_logging",
]

# ---------------------------------------------------------------------------
# 轮转与格式常量（设计文档 §3：app.log INFO 10MB×7；error.log ERROR+）
# ---------------------------------------------------------------------------

APP_LOG_NAME = "app.log"
ERROR_LOG_NAME = "error.log"
MAX_BYTES = 10 * 1024 * 1024  # 10MB
BACKUP_COUNT = 7  # ×7 份备份
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

# ---------------------------------------------------------------------------
# 打码规则（[S3]：API Key/Token/webhook secret/SMTP password 一律打码）
# ---------------------------------------------------------------------------

MASK_PREFIX = "****"
MASK_TAIL_KEEP = 4  # 保留尾 4 位，与控制台 API 打码口径一致（设计文档 §10.1）
MIN_SECRET_LENGTH_FOR_TAIL = 8  # 短于该长度的敏感值完全打码，不保留尾 4 位

# 键名形态：api_key / api-key / apikey / webhook / webhook_url / secret /
# token / password / passwd / pwd / authorization / 密码（含中文冒号分隔）
_KEY_NAMES = (
    r"api [_-]? key | apikey | webhook (?: [_-]? url )? | secret | token "
    r"| password | passwd | pwd | authorization | 密码"
)
_KEY_QUOTE = r" [\"']? "  # 兼容 JSON 形态："api_key": ...
_SEP = r" \s* [:=：] \s* "

# 裸 sk- 开头的 API 密钥（如 DeepSeek key），出现在任意位置都打码
_SK_KEY_RE = re.compile(r"\b(?P<val>sk-[A-Za-z0-9_-]{6,})")

# HTTP 凭据头：Bearer / Basic <token>
_BEARER_BASIC_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+(?P<val>[A-Za-z0-9._~+/=-]{8,})")

# 飞书自定义机器人 webhook URL 中的 hook 令牌
_WEBHOOK_PATH_RE = re.compile(r"(?i)(open-apis/bot/v2/hook/)(?P<val>[0-9A-Za-z_-]{8,})")

# 键值形态（带引号）：api_key: "sk-xxx" / {"api_key": "sk-xxx"} / password: 'p@ss'
_KV_QUOTED_RE = re.compile(
    rf"""(?ix)
    (?:{_KEY_NAMES}) {_KEY_QUOTE} {_SEP}
    (?P<q>["'])
    (?P<val>.+?)
    (?P=q)
    """
)

# 键值形态（裸值）：api_key=sk-xxx / smtp_password=xxx / 密码：xxx
# 负向前瞻：Bearer/Basic 凭据已由专属模式处理，避免把凭据方案名误当作值吞掉
_KV_BARE_RE = re.compile(
    rf"""(?ix)
    (?:{_KEY_NAMES}) {_KEY_QUOTE} {_SEP}
    (?P<val>(?!(?:bearer|basic)\b)[^\s,;)\]}}\"']+)"""
)

_MASK_PATTERNS: tuple[re.Pattern[str], ...] = (
    _SK_KEY_RE,
    _BEARER_BASIC_RE,
    _WEBHOOK_PATH_RE,
    _KV_QUOTED_RE,
    _KV_BARE_RE,
)

# ---------------------------------------------------------------------------
# 简历全文拦截（[S3]：简历全文不落日志；长度阈值 + 关键词）
# ---------------------------------------------------------------------------

RESUME_TEXT_LENGTH_THRESHOLD = 500  # pypdf 提取的简历全文通常远超此长度
RESUME_KEYWORDS: tuple[str, ...] = (
    "教育经历",
    "教育背景",
    "工作经历",
    "项目经历",
    "项目经验",
    "专业技能",
    "自我评价",
    "求职意向",
    "个人信息",
    "主修课程",
    "语言能力",
)
RESUME_KEYWORD_MIN_HITS = 2  # 需命中至少 2 个不同关键词，降低误拦

_INTERCEPT_PLACEHOLDER = "[S3拦截] 疑似简历全文未写入日志（原始长度 {length} 字符）"
_RESUME_WARNING_TEMPLATE = (
    "检测到疑似简历全文尝试写入日志（原始长度 {length} 字符），已拦截——简历全文禁止落日志 [S3]"
)

# handler 管理标记与过滤器放行标记
_MANAGED_ATTR = "_job_agent_managed_handler"
_SANITIZED_ATTR = "_job_agent_sanitized"
_RESUME_BLOCKED_ATTR = "_job_agent_resume_blocked"

_WARN_LOGGER = logging.getLogger("job_agent.logging_setup")


def mask_secret_value(value: str) -> str:
    """把单个敏感值打码为 ``****`` + 尾 4 位；短值（<8 字符）完全打码。"""
    if len(value) < MIN_SECRET_LENGTH_FOR_TAIL:
        return MASK_PREFIX
    return MASK_PREFIX + value[-MASK_TAIL_KEEP:]


def _mask_match(match: re.Match[str]) -> str:
    """正则替换回调：仅替换命中的敏感值部分，保留键名/前缀等非敏感上下文。"""
    value = match.group("val")
    return match.group(0).replace(value, mask_secret_value(value))


def mask_secrets(text: str) -> str:
    """对整段文本执行全部打码规则；幂等（重复打码结果不变）。"""
    for pattern in _MASK_PATTERNS:
        text = pattern.sub(_mask_match, text)
    return text


def looks_like_resume_text(text: str) -> bool:
    """长度达到阈值且命中足量简历特征关键词 → 判定为简历全文。"""
    if len(text) < RESUME_TEXT_LENGTH_THRESHOLD:
        return False
    hits = sum(1 for keyword in RESUME_KEYWORDS if keyword in text)
    return hits >= RESUME_KEYWORD_MIN_HITS


def _emit_resume_warning(length: int) -> None:
    """发出简历拦截 WARNING 告警（带放行标记，避免过滤器递归）。"""
    if not _WARN_LOGGER.isEnabledFor(logging.WARNING):
        return
    record = _WARN_LOGGER.makeRecord(
        _WARN_LOGGER.name,
        logging.WARNING,
        __file__,
        0,
        _RESUME_WARNING_TEMPLATE.format(length=length),
        (),
        None,
    )
    setattr(record, _SANITIZED_ATTR, True)
    _WARN_LOGGER.handle(record)


class SanitizingFilter(logging.Filter):
    """handler 级过滤器：先拦截简历全文，再对剩余文本打码。

    直接修改 ``record.msg`` / ``record.args``（以 ``getMessage()`` 结果整体替换），
    因此对惰性格式化参数同样生效；恒返回 ``True``（只清洗、不丢弃记录）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, _SANITIZED_ATTR, False):
            # 本过滤器自产的告警记录，直接放行，防止递归
            return True
        try:
            message = record.getMessage()
        except (TypeError, ValueError):  # pragma: no cover - 防御性兜底
            message = str(record.msg)

        if looks_like_resume_text(message):
            _emit_resume_warning(len(message))
            record.msg = _INTERCEPT_PLACEHOLDER.format(length=len(message))
            record.args = None
            setattr(record, _RESUME_BLOCKED_ATTR, True)
            return True

        masked = mask_secrets(message)
        if masked != message:
            record.msg = masked
            record.args = None
        return True


# ---------------------------------------------------------------------------
# 装配 / 卸载
# ---------------------------------------------------------------------------


def _remove_managed_handlers(logger: logging.Logger) -> None:
    """关闭并移除本模块装配的全部 handler（保证 setup_logging 可重复调用）。"""
    for handler in list(logger.handlers):
        if getattr(handler, _MANAGED_ATTR, False):
            logger.removeHandler(handler)
            handler.close()


def setup_logging(
    logs_dir: str | Path = "logs",
    *,
    level: int = logging.INFO,
    console: bool = True,
) -> logging.Logger:
    """初始化分级日志（在 root logger 上装配，覆盖第三方库记录）。

    - ``logs_dir/app.log``：RotatingFileHandler，INFO+，10MB×7；
    - ``logs_dir/error.log``：RotatingFileHandler，ERROR+，同样 10MB×7；
    - 控制台（stdout）：INFO+（``console=False`` 可关闭，供测试静音）；
    - 全部 handler 挂 ``SanitizingFilter``。

    重复调用安全：会先移除旧的自装配 handler，不会重复输出。
    返回装配后的 root logger。
    """
    root = logging.getLogger()
    _remove_managed_handlers(root)

    directory = Path(logs_dir)
    directory.mkdir(parents=True, exist_ok=True)

    sanitizer = SanitizingFilter()
    formatter = logging.Formatter(LOG_FORMAT)

    app_handler = RotatingFileHandler(
        directory / APP_LOG_NAME,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    app_handler.setLevel(logging.INFO)

    error_handler = RotatingFileHandler(
        directory / ERROR_LOG_NAME,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)

    handlers: list[logging.Handler] = [app_handler, error_handler]
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        handlers.append(console_handler)

    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(sanitizer)
        setattr(handler, _MANAGED_ATTR, True)
        root.addHandler(handler)

    root.setLevel(level)
    return root


def shutdown_logging() -> None:
    """关闭并移除本模块装配的 handler（测试收尾用，避免占用临时目录句柄）。"""
    _remove_managed_handlers(logging.getLogger())
