"""S1a 配置模块单元测试（全部离线，TEST1 口径）。

覆盖：内置默认值（需求文档 第 10 节）、示例配置加载合并、部分覆盖、
校验失败路径（auto_reply_limit / 端口 / 枚举 / 时间格式 / 范围对 / 未知字段）、
写回往返（load → write_back → load 相等）、same_as_delivery 标记、入口横幅。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from job_agent.config import Config, load_config, write_back

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PATH = REPO_ROOT / "config.example.yaml"


# ---- 默认值（需求文档 第 10 节 / 设计文档 §4）----


def test_builtin_defaults() -> None:
    config = Config()
    # site
    assert config.site.adapter == "zhipin"
    assert config.site.browser_strategy == "S1b_patchright"
    assert config.site.channel == "chrome"
    assert config.site.headless is False
    # target
    assert config.target.city == ""
    # delivery
    assert config.delivery.run_schedule.weekdays == [1, 2, 3, 4, 5]
    assert config.delivery.run_schedule.start == "10:00"
    assert config.delivery.run_schedule.end == "16:00"
    assert config.delivery.hourly_limit == 25
    assert config.delivery.daily_limit == 100
    assert config.delivery.action_delay_range_s == [8, 20]
    assert config.delivery.polling.interval_range_min == [3, 5]
    assert config.delivery.polling.schedule is None  # = same_as_delivery
    assert config.delivery.login_patrol.every_loop is True
    assert config.delivery.login_patrol.interval_min == 10
    assert config.delivery.dedup.mode == "permanent_blacklist"
    assert config.delivery.dedup.cooldown_days == 30
    assert config.delivery.browse_limits.detail_hourly == 40
    assert config.delivery.browse_limits.detail_daily == 200
    assert config.delivery.browse_limits.search_page_hourly == 30
    assert config.delivery.min_interval_s.detail == 30
    assert config.delivery.min_interval_s.search == 30
    assert config.delivery.browse_rampup.enabled is True
    assert config.delivery.browse_rampup.first_day.detail_hourly == 15
    assert config.delivery.browse_rampup.first_day.detail_daily == 60
    assert config.delivery.browse_rampup.double_per_clean_day is True
    assert config.delivery.dry_run is True  # 首次安装默认开启（FR3.9）
    assert config.delivery.ramp_up.enabled_days == 3
    assert config.delivery.ramp_up.daily_limit == 10
    assert config.delivery.ramp_up.auto_recover is True
    # messaging
    assert config.messaging.auto_reply_limit == 2
    assert config.messaging.resume_auto_send_limit_per_conv == 1
    assert config.messaging.passive_greeting_enabled is False
    assert config.messaging.positive_response_resume_enabled is False
    assert config.messaging.outbox_timeout_min == 60
    # ai
    assert config.ai.base_url == "https://api.deepseek.com/v1"
    assert config.ai.api_key == ""
    assert config.ai.model == "deepseek-chat"
    assert config.ai.temperature == 0.3
    assert config.ai.proxy == ""
    assert config.ai.min_score == 60
    assert config.ai.neutral_invest is True
    assert config.ai.daily_budget_calls == 1000
    assert config.ai.preferences == []  # v1 恒空（字段存在但不使用，N5）
    # match
    assert [g.id for g in config.match.exclude_groups] == [
        "outsourcing",
        "headhunter",
        "trainee_intern_parttime",
    ]
    assert all(g.kind == "preset" and g.enabled is False for g in config.match.exclude_groups)
    assert config.match.company_blacklist == []
    # notify
    assert config.notify.feishu.webhook == ""
    assert config.notify.feishu.levels == ["high", "medium"]
    assert config.notify.email.smtp_port == 465
    assert config.notify.dnd.enabled is False
    assert config.notify.dnd.start == "23:00"
    assert config.notify.dnd.end == "07:30"
    assert config.notify.dnd.high_bypass is True
    # console / data
    assert config.console.host == "127.0.0.1"
    assert config.console.port == 8000
    assert config.data.db_path == Path("data/agent.db")
    assert config.data.db_path.as_posix() == "data/agent.db"


def test_defaults_are_independent_between_instances() -> None:
    """可变默认值（列表/嵌套模型）不得在实例间共享。"""
    first = Config()
    second = Config()
    first.match.exclude_groups[0].keywords.append("污染探针")
    first.delivery.run_schedule.weekdays.append(7)
    assert second.match.exclude_groups[0].keywords == ["外包", "人力资源服务", "软件外包"]
    assert second.delivery.run_schedule.weekdays == [1, 2, 3, 4, 5]


# ---- 示例配置 ----


def test_example_yaml_loads_and_merges_cleanly() -> None:
    """验收标准 3：config.example.yaml 加载后与内置默认值合并无异常（值即默认值）。"""
    config = load_config(EXAMPLE_PATH)
    assert config == Config()


def test_example_yaml_contains_overwrite_warning() -> None:
    """示例必须含"手写注释会被控制台覆盖"提示（S1a 任务书第 6 项）。"""
    assert "手写注释会被控制台覆盖" in EXAMPLE_PATH.read_text(encoding="utf-8")


# ---- 加载与合并 ----


def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "不存在.yaml") == Config()


def test_empty_file_returns_defaults(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("", encoding="utf-8")
    assert load_config(p) == Config()


def test_partial_config_overrides_defaults(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        "target: {city: 上海}\nai: {api_key: sk-test}\nconsole: {port: 9000}\n",
        encoding="utf-8",
    )
    config = load_config(p)
    assert config.target.city == "上海"
    assert config.ai.api_key == "sk-test"
    assert config.console.port == 9000
    # 未写字段回落内置默认值
    assert config.ai.model == "deepseek-chat"
    assert config.messaging.auto_reply_limit == 2
    assert config.delivery.dry_run is True


def test_non_mapping_root_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("- 只是\n- 一个列表\n", encoding="utf-8")
    with pytest.raises(ValueError, match="根节点必须是映射"):
        load_config(p)


# ---- 写回往返 ----


def test_write_back_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        '# 用户手写注释：写回后不应保留\ntarget:\n  city: "上海"\n'
        'delivery:\n  hourly_limit: 12\n  run_schedule:\n    start: "09:30"\n',
        encoding="utf-8",
    )
    original = load_config(p)
    write_back(original, p)
    reloaded = load_config(p)
    assert reloaded == original  # load → write_back → load 相等
    text = p.read_text(encoding="utf-8")
    assert "用户手写注释" not in text  # 全量重写，无注释保留 [B14]
    assert "hourly_limit: 12" in text  # 用户值保留
    assert "09:30" in text
    assert "dry_run: true" in text  # 未写字段补全为默认值（全量快照）
    assert "data/agent.db" in text  # 路径统一 posix 分隔符（§4 字面形态）
    assert not (tmp_path / "config.yaml.tmp").exists()  # 临时文件已被原子替换


def test_write_back_round_trip_with_full_overrides(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        "site: {adapter: mock, headless: true}\n"
        "delivery:\n"
        "  polling:\n"
        "    interval_range_min: [1, 2]\n"
        '    schedule: {weekdays: [6, 7], start: "20:00", end: "22:00"}\n'
        "  dedup: {mode: cooldown, cooldown_days: 7}\n"
        "messaging: {auto_reply_limit: 5}\n"
        "match:\n"
        "  exclude_groups:\n"
        "    - {id: custom_x, kind: custom, enabled: true, keywords: [夜班], scope: [jd_text]}\n"
        "notify:\n"
        "  feishu: {levels: [high]}\n",
        encoding="utf-8",
    )
    original = load_config(p)
    write_back(original, p)
    assert load_config(p) == original


# ---- polling.schedule 的 same_as_delivery 标记 ----


def test_schedule_same_as_delivery_token_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("delivery:\n  polling:\n    schedule: same_as_delivery\n", encoding="utf-8")
    config = load_config(p)
    assert config.delivery.polling.schedule is None
    write_back(config, p)
    assert "same_as_delivery" in p.read_text(encoding="utf-8")  # 标记按 §4 字面还原
    assert load_config(p) == config


def test_schedule_independent_config_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        "delivery:\n  polling:\n    schedule:\n"
        '      weekdays: [6, 7]\n      start: "20:00"\n      end: "22:00"\n',
        encoding="utf-8",
    )
    config = load_config(p)
    schedule = config.delivery.polling.schedule
    assert schedule is not None
    assert schedule.weekdays == [6, 7]
    assert schedule.start == "20:00"
    write_back(config, p)
    assert load_config(p) == config


# ---- 校验失败路径 ----


@pytest.mark.parametrize("bad_value", [0, -3])
def test_auto_reply_limit_must_be_at_least_one(bad_value: int, tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(f"messaging: {{auto_reply_limit: {bad_value}}}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="auto_reply_limit"):
        load_config(p)


@pytest.mark.parametrize("bad_port", [0, -1, 65536, 100000])
def test_console_port_out_of_range(bad_port: int, tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(f"console: {{port: {bad_port}}}\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


def test_smtp_port_out_of_range(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("notify: {email: {smtp_port: 70000}}\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


def test_unknown_field_rejected(tmp_path: Path) -> None:
    """拼写错误的字段必须报错，不得被静默忽略。"""
    p = tmp_path / "config.yaml"
    p.write_text("site: {adptr: zhipin}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="adptr"):
        load_config(p)


def test_bad_time_format_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text('delivery: {run_schedule: {start: "25:00"}}\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


def test_bad_weekday_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("delivery: {run_schedule: {weekdays: [0, 8]}}\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


def test_bad_range_pair_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("delivery: {action_delay_range_s: [20, 8]}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="下限不能大于上限"):
        load_config(p)


def test_range_pair_wrong_length_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("delivery: {action_delay_range_s: [8]}\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


def test_bad_dedup_mode_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("delivery: {dedup: {mode: whatever}}\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


# ---- 入口最小闭环（S1a 目标：加载配置 → 日志初始化 → 横幅 → 退出）----


def test_python_m_job_agent_prints_banner() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "job_agent"],
        capture_output=True,
        text=True,
        encoding="utf-8",  # [R6-8] 入口强制 UTF-8 输出，Windows 管道解码需显式指定
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "job-agent" in proc.stdout
    assert "v1.0.0" in proc.stdout
