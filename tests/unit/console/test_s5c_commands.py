"""S5c 指令入队测试：POST → EngineCommand（req_id）→ 引擎消费 ack 落 run_state。

覆盖（任务书 S5c 测试第 1 项）：

- 六指令按钮（start/stop/pause/resume）+ relaunch_browser + login_mode
  （开窗/``closed`` 通知两态 [S4d payload 契约]）+ clear_risk_pause
  （M5 唯一恢复通道）——入队对象的 cmd/payload/req_id 逐项断言；
- **ack 往返**：真实 Orchestrator（FakeAdapter）消费同一队列 →
  ``run_state.last_command`` 的 req_id 与 API 返回一致、ok/detail 正确；
- 风控暂停 → 总览红条（alerts）→ 清除恢复全链；
- 非法 action（422）/ 未知指令（404）负例。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from job_agent.core import rampup, runstate
from job_agent.core.orchestrator import KEY_ENGINE_STATE, KEY_LAST_COMMAND
from job_agent.models import RiskSignal

from .s5c_support import OpsTestEnv


@pytest.fixture()
def env() -> Iterator[OpsTestEnv]:
    e = OpsTestEnv(with_orchestrator=True, dry_run=True)
    yield e
    e.close()


def _queued(env: OpsTestEnv) -> list[Any]:
    items = list(env.queue.queue)
    env.queue.queue.clear()
    return items


def test_engine_commands_enqueue_with_reqid(env: OpsTestEnv) -> None:
    """start/stop/pause/resume → 队列内 EngineCommand 字段逐项正确。"""
    for action in ("start", "pause", "resume", "stop"):
        resp = env.client.post(f"/api/engine/{action}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cmd"] == action and body["queued"] is True
        (cmd,) = _queued(env)
        assert cmd.cmd == action
        assert cmd.req_id == body["req_id"] and len(cmd.req_id) >= 32  # uuid4
        assert cmd.payload == {}
        assert cmd.issued_at.tzinfo is not None  # tz-aware（I6 口径）


def test_relaunch_and_login_mode_payload(env: OpsTestEnv) -> None:
    """relaunch + login_mode 两态：默认开窗；{"closed": true} = 关窗通知。"""
    assert env.client.post("/api/browser/relaunch").json()["cmd"] == "relaunch_browser"
    (relaunch,) = _queued(env)
    assert relaunch.cmd == "relaunch_browser"

    resp = env.client.post("/api/browser/login-mode")  # 无体 = 打开登录窗口
    assert resp.status_code == 200
    (open_cmd,) = _queued(env)
    assert open_cmd.cmd == "login_mode" and open_cmd.payload == {}

    resp = env.client.post("/api/browser/login-mode", json={"closed": True})
    assert resp.status_code == 200
    (close_cmd,) = _queued(env)
    assert close_cmd.cmd == "login_mode" and close_cmd.payload == {"closed": True}


def test_unknown_action_rejected(env: OpsTestEnv) -> None:
    """非法 action：Literal 校验 422；不入队。"""
    resp = env.client.post("/api/engine/explode")
    assert resp.status_code == 422
    assert env.queue.empty()


def test_ack_round_trip_start_pause_resume_stop(env: OpsTestEnv) -> None:
    """API 返回的 req_id 与引擎消费后的 run_state.last_command 逐字一致。"""
    req_ids = []
    for action in ("start", "pause", "resume", "stop"):
        req_ids.append(env.client.post(f"/api/engine/{action}").json()["req_id"])
        env.tick()
    ack = runstate.get_state(env.db, KEY_LAST_COMMAND)
    assert isinstance(ack, dict)
    assert ack["req_id"] == req_ids[-1]
    assert ack["cmd"] == "stop" and ack["ok"] is True
    engine = runstate.get_state(env.db, KEY_ENGINE_STATE)
    assert isinstance(engine, dict) and engine["state"] == "stopped"
    # GET /api/status（S5a 端点 + S5c 扩展字段）回显 ack
    data = env.client.get("/api/status").json()
    assert data["last_command"]["req_id"] == req_ids[-1]
    assert data["engine"]["state"] == "stopped"
    assert data["dry_run"] is True


def test_risk_pause_banner_and_clear_risk_pause_full_chain(env: OpsTestEnv) -> None:
    """风控暂停：横幅红条 → clear-risk-pause 指令 → 引擎清除 + ack + 补对账标记。"""
    env.bind_messenger()  # 轮询任务驱动检测（every_loop）→ EVICTED 触发 M5
    env.client.post("/api/engine/start")
    env.tick()
    env.adapter.risk_signal = RiskSignal.EVICTED
    env.tick()  # poll 任务执行前检测 → M5 暂停 + PAGE_EVICTED 高级事件
    assert rampup.is_risk_paused(env.db) is True

    alerts = env.client.get("/api/alerts").json()["alerts"]
    risk = next(a for a in alerts if a["type"] == "risk_pause")
    assert risk["level"] == "high" and risk["signal"] == "EVICTED"
    assert risk["action"] == "clear_risk_pause" and "M5" in risk["message"]

    status = env.client.get("/api/status").json()
    assert status["risk_pause"]["awaiting_user"] is True

    env.adapter.risk_signal = RiskSignal.NONE  # 环境恢复（否则清除后又被检测重置）
    resp = env.client.post("/api/engine/clear-risk-pause")
    assert resp.status_code == 200 and resp.json()["cmd"] == "clear_risk_pause"
    (cmd,) = list(env.queue.queue)  # 窥视不清空
    assert cmd.payload == {"decided_by": "console"}
    env.tick()
    assert rampup.is_risk_paused(env.db) is False
    ack = runstate.get_state(env.db, KEY_LAST_COMMAND)
    assert isinstance(ack, dict) and ack["cmd"] == "clear_risk_pause"
    assert ack["ok"] is True and ack["detail"] == "risk_pause_cleared"
    assert env.client.get("/api/alerts").json()["alerts"] == []  # 红条消失


def test_clear_risk_pause_idempotent_when_not_paused(env: OpsTestEnv) -> None:
    """未暂停时清除：幂等 no-op（detail=not_paused，仍 ok）。"""
    env.client.post("/api/engine/clear-risk-pause")
    env.tick()
    ack = runstate.get_state(env.db, KEY_LAST_COMMAND)
    assert isinstance(ack, dict) and ack["detail"] == "not_paused"


def test_login_mode_open_rejected_while_running(env: OpsTestEnv) -> None:
    """引擎运行中开登录窗口：指令被引擎拒绝（M3/G5 互斥），ack 如实回传。"""
    env.client.post("/api/engine/start")
    env.tick()
    req = env.client.post("/api/browser/login-mode").json()
    env.tick()
    ack = runstate.get_state(env.db, KEY_LAST_COMMAND)
    assert isinstance(ack, dict)
    assert ack["req_id"] == req["req_id"] and ack["ok"] is False
    assert ack["detail"] == "engine_running"
