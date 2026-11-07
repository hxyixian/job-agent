"""S5c 总览与通知中心测试：status 扩展字段 / alerts 横幅 / prereq 清单 /
试运行清单 / notifications 查询 / 四页静态约束。

覆盖（任务书 S5c 测试第 5 项 + §10.3）：

- ``GET /api/status``（S5a 端点 + S5c 扩展）：缺省态、engine_state 回显、
  dry_run/放量期/平台限额、响应确定性（GET 幂等）；
- ``GET /api/alerts``：风控（EVICTED/SECURITY_CHECK 信号）、登录失效、
  适配器失效、平台限额红条、登录模式互斥提示；
- ``GET /api/prereq``：五项清单（缺项亮红数据 + 指引 + run_state 运行时项
  + dry_run 下登录态缓冲）；
- ``GET /api/dryrun/planned``：拟发送清单（greeting 补岗位标题、reply 补
  HR 名）；
- ``GET /api/notifications``：级别过滤/类型过滤/分页/计数/payload 解析；
- 四页静态约束：≤300 行、无 CDN/module 引用、关键控件存在、POST 带
  X-Job-Agent 头、九页导航。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_agent.console.api import status as status_routes_module
from job_agent.core import dryrun, events, rampup, runstate
from job_agent.core.orchestrator import KEY_ENGINE_STATE
from job_agent.models import RiskSignal
from job_agent.timeutil import beijing_date_str

from .s5c_support import BASE_TIME, OpsTestEnv, seed_conv, seed_job

STATIC_DIR = Path(status_routes_module.__file__).resolve().parent.parent / "static"

MY_PAGES = ["dashboard.html", "outbox.html", "records.html", "notifications.html"]


@pytest.fixture()
def env() -> Iterator[OpsTestEnv]:
    e = OpsTestEnv()
    yield e
    e.close()


# ---- /api/status（S5a 端点 + S5c 扩展字段）----


def test_status_defaults_when_engine_never_ran(env: OpsTestEnv) -> None:
    data = env.client.get("/api/status").json()
    assert data["engine"]["state"] == "unknown"
    assert data["last_command"] is None
    assert data["dry_run"] is True  # wide_config(dry_run=True) 回退
    assert data["rampup"] == {"state": "unknown", "days_left": None}
    assert data["risk_pause"] is None and data["platform_limit_today"] is False


def test_status_reflects_engine_state_and_is_deterministic(env: OpsTestEnv) -> None:
    runstate.set_state(
        env.db,
        KEY_ENGINE_STATE,
        {
            "state": "running",
            "fatal_reason": None,
            "risk_paused": False,
            "login_mode": False,
            "dry_run": False,
            "updated_at": "2026-11-05T10:00:00.000Z",
        },
    )
    runstate.set_state(env.db, dryrun.KEY_DRY_RUN, False)
    runstate.set_state(
        env.db,
        rampup.KEY_RAMPUP,
        {
            "state": "active",
            "enabled_days": 3,
            "effective_days": ["2026-11-03", "2026-11-04"],
            "started_at": "2026-11-03T02:00:00.000Z",
        },
    )
    first = env.client.get("/api/status").json()
    second = env.client.get("/api/status").json()
    assert first == second  # GET 幂等（响应确定性）
    assert first["engine"]["state"] == "running"
    assert first["dry_run"] is False
    assert first["rampup"] == {"state": "active", "days_left": 1}


def test_status_platform_limit_today_flag(env: OpsTestEnv) -> None:
    # /api/status 是 S5a 端点（无注入时钟）：平台限额日按真实“今天”判定
    runstate.set_state(env.db, "platform_limit_date", beijing_date_str(datetime.now(UTC)))
    assert env.client.get("/api/status").json()["platform_limit_today"] is True


# ---- /api/alerts ----


def test_alerts_empty_when_healthy(env: OpsTestEnv) -> None:
    assert env.client.get("/api/alerts").json()["alerts"] == []


@pytest.mark.parametrize("signal", [RiskSignal.EVICTED, RiskSignal.SECURITY_CHECK])
def test_alerts_risk_pause_banner_with_signal(env: OpsTestEnv, signal: RiskSignal) -> None:
    rampup.handle_risk_signal(env.db, env.config, signal, BASE_TIME)
    alerts = env.client.get("/api/alerts").json()["alerts"]
    assert len(alerts) == 1
    banner = alerts[0]
    assert banner["type"] == "risk_pause" and banner["level"] == "high"
    assert banner["signal"] == signal.name and banner["action"] == "clear_risk_pause"
    # m5 标志：试运行期间 → 文案带 M5 提示
    assert "M5" in banner["message"]


def test_alerts_login_expired_and_platform_limit(env: OpsTestEnv) -> None:
    runstate.set_state(
        env.db,
        KEY_ENGINE_STATE,
        {"state": "running", "fatal_reason": "login_expired", "login_mode": True},
    )
    runstate.set_state(env.db, "platform_limit_date", beijing_date_str(BASE_TIME))
    alerts = env.client.get("/api/alerts").json()["alerts"]
    by_type = {a["type"]: a for a in alerts}
    assert by_type["login_expired"]["level"] == "high"
    assert "登录模式" in by_type["login_expired"]["guide"]
    assert by_type["platform_limit"]["level"] == "high"
    assert by_type["login_mode"]["level"] == "info"  # 互斥提示非红条


def test_alerts_adapter_broken(env: OpsTestEnv) -> None:
    runstate.set_state(
        env.db, KEY_ENGINE_STATE, {"state": "running", "fatal_reason": "adapter_broken"}
    )
    banner = env.client.get("/api/alerts").json()["alerts"][0]
    assert banner["type"] == "adapter_broken" and banner["action"] == "relaunch_browser"


# ---- /api/prereq（§4 五项，B14）----


def test_prereq_all_missing_highlights_red_items(env: OpsTestEnv) -> None:
    data = env.client.get("/api/prereq").json()
    items = {i["key"]: i for i in data["items"]}
    assert set(items) == {
        "ai_connectivity",
        "resume_parsed",
        "directions_confirmed",
        "greeting_finalized",
        "login_valid",
    }
    # DB 可判项缺 → False（亮红）+ 指引；运行时项未知 → null（不亮红）
    assert items["resume_parsed"]["ok"] is False and items["resume_parsed"]["guide"]
    assert items["directions_confirmed"]["ok"] is False
    assert items["greeting_finalized"]["ok"] is False
    assert items["ai_connectivity"]["ok"] is None
    assert items["login_valid"]["ok"] is None
    assert data["all_met"] is False


def test_prereq_db_items_turn_green_and_runtime_detail(env: OpsTestEnv) -> None:
    env.db.execute("INSERT INTO resume_snapshots (structured_json, is_current) VALUES ('{}', 1)")
    env.db.execute(
        "INSERT INTO job_directions (keywords_json, is_confirmed) VALUES ('[\"Python\"]', 1)"
    )
    env.db.execute(
        "INSERT INTO greeting_templates (kind, content, version, is_current)"
        " VALUES ('active', '定稿', 1, 1)"
    )
    data = env.client.get("/api/prereq").json()
    items = {i["key"]: i for i in data["items"]}
    assert items["resume_parsed"]["ok"] is True
    assert items["directions_confirmed"]["ok"] is True
    assert items["greeting_finalized"]["ok"] is True
    assert data["all_met"] is False  # ai_connectivity 未知仍不算全满足
    # 引擎写入运行时详情 → ai/login 变为布尔；dry_run 下 login 不计入 all_met
    runstate.set_state(env.db, "prereq_detail", {"ai": True, "login": False})
    data = env.client.get("/api/prereq").json()
    items = {i["key"]: i for i in data["items"]}
    assert items["ai_connectivity"]["ok"] is True
    assert items["login_valid"]["ok"] is False
    assert data["dry_run"] is True and data["all_met"] is True  # ⑤ 缓冲（§4-⑤）
    # 正式模式下 login=False 重新计入 → 不满足
    runstate.set_state(env.db, dryrun.KEY_DRY_RUN, False)
    assert env.client.get("/api/prereq").json()["all_met"] is False


# ---- /api/dryrun/planned（试运行清单，FR3.9）----


def test_planned_list_enriches_targets(env: OpsTestEnv) -> None:
    seed_job(env.db, "J1", title="后端工程师")
    seed_conv(env.db, "C1", peer="HR乙")
    dryrun.record_planned_send(env.db, "greeting", "J1", "d1")
    dryrun.record_planned_send(env.db, "reply", "C1", "d2")
    data = env.client.get("/api/dryrun/planned").json()
    assert data["total"] == 2
    by_kind = {i["kind"]: i for i in data["items"]}
    assert by_kind["greeting"]["target_label"] == "后端工程师"
    assert by_kind["reply"]["target_label"] == "HR乙"
    assert by_kind["greeting"]["target_ref"] == "J1"


# ---- /api/notifications（全量事件流 + 级别过滤）----


def _seed_events(env: OpsTestEnv) -> None:
    events.emit(env.db, "LOGIN_EXPIRED", {"reason": "登录失效"})
    events.emit(env.db, "KEY_QUESTION", {"conv_id": "C1"})
    events.emit(env.db, "DAILY_DIGEST", {"delivered": 3})
    events.emit(env.db, "PAGE_EVICTED", {"signal": "EVICTED"})


def test_notifications_list_order_and_level_filter(env: OpsTestEnv) -> None:
    _seed_events(env)
    data = env.client.get("/api/notifications").json()
    assert data["total"] == 4
    assert data["counts"] == {"high": 2, "medium": 1, "low": 1}
    assert data["items"][0]["event_type"] == "PAGE_EVICTED"  # 最新在前
    assert isinstance(data["items"][0]["payload"], dict)
    high = env.client.get("/api/notifications", params={"level": "high"}).json()
    assert {i["event_type"] for i in high["items"]} == {"LOGIN_EXPIRED", "PAGE_EVICTED"}
    assert high["total"] == 2
    etype = env.client.get("/api/notifications", params={"event_type": "KEY_QUESTION"}).json()
    assert etype["total"] == 1 and etype["items"][0]["level"] == "medium"
    # 非法级别 → 422
    assert env.client.get("/api/notifications", params={"level": "urgent"}).status_code == 422


def test_notifications_pagination(env: OpsTestEnv) -> None:
    _seed_events(env)
    page1 = env.client.get("/api/notifications", params={"limit": 2}).json()
    assert len(page1["items"]) == 2 and page1["total"] == 4
    page2 = env.client.get("/api/notifications", params={"limit": 2, "offset": 2}).json()
    assert len(page2["items"]) == 2
    assert page1["items"][-1]["id"] != page2["items"][0]["id"]


# ---- 四页静态约束（A-BOOT-1 的静态前置）----


def _page(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def test_my_four_pages_present_and_within_line_budget() -> None:
    for page in MY_PAGES:
        html = _page(page)
        lines = len(html.strip("\n").splitlines())
        assert lines <= 300, f"{page} 超过 300 行：{lines}"
        inline = sum(
            len(block.strip("\n").splitlines())
            for block in re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        )
        assert inline <= 300, f"{page} 内联 JS 超过 300 行：{inline}"


def test_my_pages_no_build_tooling_and_use_security_header() -> None:
    for page in MY_PAGES:
        html = _page(page)
        assert "https://cdn" not in html and 'type="module"' not in html
        assert "node_modules" not in html
        if page != "notifications.html":  # 通知中心纯 GET；有 POST 的页面须带头
            assert "X-Job-Agent" in html  # 非 GET 携带安全头（§10.1-3）


def test_dashboard_page_key_controls_present() -> None:
    html = _page("dashboard.html")
    for needle in (
        "btnStart",
        "btnPause",
        "btnResume",
        "btnStop",  # 四指令按钮
        "btnLogin",
        "btnLoginDone",  # 登录模式开/关
        "/api/browser/relaunch",  # 重新拉起浏览器
        "/api/engine/clear-risk-pause",  # M5 恢复入口
        "试运行中",  # dry_run 显著标识
        "/api/dryrun/planned",  # 试运行清单入口
        "/api/prereq",
        "/api/alerts",
        "/api/status",
    ):
        assert needle in html, f"dashboard.html 缺少关键元素：{needle}"


def test_outbox_records_notifications_page_anchors() -> None:
    outbox = _page("outbox.html")
    for needle in ("/api/outbox", "confirm(", "重新生成", "stale"):
        assert needle in outbox, f"outbox.html 缺少：{needle}"
    records = _page("records.html")
    for needle in (
        "/api/applications",
        "/api/stats",
        "/api/evaluations/export",
        "/api/evaluations/labels/import",
        "手动标记",
        "推断规则",
    ):
        assert needle in records, f"records.html 缺少：{needle}"
    notifications = _page("notifications.html")
    assert "/api/notifications" in notifications and "high" in notifications


def test_my_pages_nine_page_navigation() -> None:
    links = [
        "dashboard.html",
        "resume.html",
        "greetings.html",
        "config.html",
        "outbox.html",
        "records.html",
        "notify.html",
        "ai.html",
        "notifications.html",
    ]
    for page in MY_PAGES:
        html = _page(page)
        for link in links:
            assert f'href="{link}"' in html, f"{page} 导航缺 {link}"
