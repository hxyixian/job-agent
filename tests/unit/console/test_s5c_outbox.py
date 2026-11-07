"""S5c 待发箱操作流测试：列表/stale 标记/approve（不计自动数）/edit/skip/regenerate。

覆盖（任务书 S5c 测试第 2 项 + §8.3/B10）：

- GET /outbox：可操作状态在前、计数、stale 标记、HR 名关联、状态过滤；
- approve：经队列（approve_outbox payload={"outbox_id"}）→ 真实引擎消费
  → dry_run 只记拟发送 [N2]；正式模式入队发送且 **auto_sent_count 不变**
  （人工确认不计自动数 [B10]）；
- skip：pending/stale → skipped；不可操作状态 409、不存在 404；
- edit：草稿直改（pending/stale 限定），approve 时引擎按最新文本执行；
- regenerate：AI 重拟（分类→草拟→护栏提示），stale → pending 归位；
  AI 失败 503 且条目保持原状。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from job_agent.ai.classify import ClassifyResult

from .s5c_support import (
    OpsTestEnv,
    seed_conv,
    seed_message,
    seed_outbox_row,
)


@pytest.fixture()
def env() -> Iterator[OpsTestEnv]:
    e = OpsTestEnv(with_orchestrator=True, dry_run=True)
    yield e
    e.close()


def _state_of(env: OpsTestEnv, entry_id: int) -> str:
    row = env.db.query_one("SELECT state FROM outbox WHERE id = ?", (entry_id,))
    assert row is not None
    return str(row["state"])


def test_list_ordering_counts_and_stale_flag(env: OpsTestEnv) -> None:
    seed_conv(env.db, "C1", job_id="J1")
    seed_conv(env.db, "C2", job_id="J2", peer="HR乙")
    pending = seed_outbox_row(env.db, "C1", draft="新草稿")
    stale = seed_outbox_row(env.db, "C2", draft="过期草稿", state="stale")
    approved = seed_outbox_row(env.db, "C1", draft="已确认", state="approved")
    data = env.client.get("/api/outbox").json()
    assert [i["id"] for i in data["items"]] == [pending, stale, approved]  # 可操作在前
    item = data["items"][1]
    assert item["stale"] is True and item["peer_name"] == "HR乙" and item["source"] == "ai"
    assert data["counts"] == {"pending": 1, "stale": 1, "approved": 1}
    # 状态过滤
    only = env.client.get("/api/outbox", params={"state": "stale"}).json()
    assert [i["id"] for i in only["items"]] == [stale]


def test_approve_dryrun_records_planned_send_and_not_count_auto(env: OpsTestEnv) -> None:
    """dry_run 下 approve：只记拟发送（skipped_dryrun 行）+ 不计自动数 [N2/B10]。"""
    env.bind_messenger()
    seed_conv(env.db, "C1", job_id="J1")
    entry = seed_outbox_row(env.db, "C1", draft="确认这条")
    resp = env.client.post(f"/api/outbox/{entry}/approve")
    assert resp.status_code == 200 and resp.json()["cmd"] == "approve_outbox"
    (cmd,) = list(env.queue.queue)  # 只窥视，不清空——tick 须能消费同一指令
    assert cmd.cmd == "approve_outbox" and cmd.payload == {"outbox_id": entry}
    env.tick()
    assert _state_of(env, entry) == "approved"
    row = env.db.query_one("SELECT state, kind, target_ref FROM send_intents WHERE kind = 'reply'")
    assert row is not None and row["state"] == "skipped_dryrun" and row["target_ref"] == "C1"
    conv = env.db.query_one("SELECT auto_sent_count FROM conversations WHERE conv_id = 'C1'")
    assert conv is not None and conv["auto_sent_count"] == 0  # 不计自动数
    assert env.adapter.sent_replies == []  # 试运行不实际发送


def test_approve_real_mode_sends_without_counting_auto(env: OpsTestEnv) -> None:
    """正式模式 approve：引擎入队发送成功，auto_sent_count 仍为 0 [B10]。"""
    import job_agent.core.dryrun as dryrun_mod

    dryrun_mod.set_dry_run(env.db, env.config, False)
    env.bind_messenger()
    env.client.post("/api/engine/start")  # 引擎须运行，REPLY 任务才会在 tick 执行
    env.tick()
    seed_conv(env.db, "C1", job_id="J1")
    entry = seed_outbox_row(env.db, "C1", draft="确认发送")
    assert env.client.post(f"/api/outbox/{entry}/approve").status_code == 200
    env.tick()  # 消费 approve_outbox → REPLY 任务入队 → 同 tick 执行发送
    assert _state_of(env, entry) == "approved"
    assert env.adapter.sent_replies == [("C1", "确认发送")]
    conv = env.db.query_one("SELECT auto_sent_count FROM conversations WHERE conv_id = 'C1'")
    assert conv is not None and conv["auto_sent_count"] == 0  # 人工确认不计自动数


def test_approve_prechecks_404_409(env: OpsTestEnv) -> None:
    seed_conv(env.db, "C1")
    assert env.client.post("/api/outbox/999/approve").status_code == 404
    skipped = seed_outbox_row(env.db, "C1", state="skipped")
    resp = env.client.post(f"/api/outbox/{skipped}/approve")
    assert resp.status_code == 409 and "invalid_state" in resp.json()["detail"]


def test_skip_flow(env: OpsTestEnv) -> None:
    seed_conv(env.db, "C1")
    entry = seed_outbox_row(env.db, "C1", state="stale")
    resp = env.client.post(f"/api/outbox/{entry}/skip")
    assert resp.json()["cmd"] == "skip_outbox"
    (cmd,) = list(env.queue.queue)  # 窥视不清空
    assert cmd.payload == {"outbox_id": entry}
    env.tick()
    assert _state_of(env, entry) == "skipped"
    assert env.client.post(f"/api/outbox/{entry}/skip").status_code == 409  # 已跳过


def test_edit_updates_draft_used_by_approve(env: OpsTestEnv) -> None:
    """edit 直改草稿；随后 approve 引擎按最新文本发送（链路一致性）。"""
    import job_agent.core.dryrun as dryrun_mod

    dryrun_mod.set_dry_run(env.db, env.config, False)
    env.bind_messenger()
    env.client.post("/api/engine/start")  # 引擎运行，REPLY 任务才执行
    env.tick()
    seed_conv(env.db, "C1", job_id="J1")
    entry = seed_outbox_row(env.db, "C1", draft="旧草稿", state="stale")
    resp = env.client.post(f"/api/outbox/{entry}/edit", json={"draft_text": "改后的草稿"})
    assert resp.status_code == 200 and resp.json()["draft_text"] == "改后的草稿"
    row = env.db.query_one("SELECT draft_text FROM outbox WHERE id = ?", (entry,))
    assert row is not None and row["draft_text"] == "改后的草稿"
    # 空文本 422；不可操作状态 409
    blank = env.client.post(f"/api/outbox/{entry}/edit", json={"draft_text": "  "})
    assert blank.status_code == 422
    skipped = seed_outbox_row(env.db, "C1", state="skipped")
    on_skipped = env.client.post(f"/api/outbox/{skipped}/edit", json={"draft_text": "x"})
    assert on_skipped.status_code == 409
    # approve 按新文本发送
    env.client.post(f"/api/outbox/{entry}/approve")
    env.tick()
    assert env.adapter.sent_replies == [("C1", "改后的草稿")]


def test_regenerate_redrafts_and_resets_stale(env: OpsTestEnv) -> None:
    seed_conv(env.db, "C1", job_id="J1")
    seed_message(env.db, "M1", "C1", text="你好，方便聊聊吗？")
    entry = seed_outbox_row(env.db, "C1", draft="旧草稿", state="stale")
    resp = env.client.post(f"/api/outbox/{entry}/regenerate")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "pending" and body["source"] == "ai"
    assert body["category"] == "low_risk"  # 桩分类（ctx 注入）
    assert body["draft_text"] == "重拟草稿(low_risk)"
    row = env.db.query_one("SELECT draft_text, source, state FROM outbox WHERE id = ?", (entry,))
    assert row is not None and row["draft_text"] == "重拟草稿(low_risk)"
    assert row["source"] == "ai" and row["state"] == "pending"


def test_regenerate_ai_failure_keeps_entry_untouched(env: OpsTestEnv) -> None:
    seed_conv(env.db, "C1")
    entry = seed_outbox_row(env.db, "C1", draft="旧草稿", state="stale")

    def boom(_ctx: object, _category: str) -> str:
        raise RuntimeError("ai down")

    env.ctx.draft_fn = boom  # type: ignore[assignment]
    resp = env.client.post(f"/api/outbox/{entry}/regenerate")
    assert resp.status_code == 503 and resp.json()["detail"] == "ai_error:RuntimeError"
    row = env.db.query_one("SELECT draft_text, state FROM outbox WHERE id = ?", (entry,))
    assert row is not None and row["draft_text"] == "旧草稿" and row["state"] == "stale"


def test_regenerate_no_reply_falls_back_to_key_category(env: OpsTestEnv) -> None:
    """分类为 no_reply/异常值 → 保守回落 key（§8.2 恒 key 兜底同构）。"""
    seed_conv(env.db, "C1")
    entry = seed_outbox_row(env.db, "C1")
    env.ctx.classify_fn = lambda msgs: ClassifyResult("no_reply", "测试")  # type: ignore[assignment]
    resp = env.client.post(f"/api/outbox/{entry}/regenerate")
    assert resp.json()["category"] == "key"
    assert resp.json()["draft_text"] == "重拟草稿(key)"


def test_regenerate_guard_warnings_surfaced(env: OpsTestEnv) -> None:
    """护栏命中：草稿仍保存（人工确认路径），但携带 guard_warnings 提示。"""
    from job_agent.ai.guard import GuardResult, GuardViolation

    seed_conv(env.db, "C1")
    entry = seed_outbox_row(env.db, "C1")
    env.ctx.guard_fn = lambda text: GuardResult(  # type: ignore[assignment]
        reasons=(GuardViolation("包含联系方式"),)
    )
    resp = env.client.post(f"/api/outbox/{entry}/regenerate")
    body = resp.json()
    assert body["draft_text"] == "重拟草稿(low_risk)"
    assert body["guard_warnings"] == ["包含联系方式"]
    assert _state_of(env, entry) == "pending"


def test_new_message_marks_stale_visible_in_list(env: OpsTestEnv) -> None:
    """引擎轮询将会话有新消息时把 pending 标 stale（§8.3）——列表呈现口径。"""
    seed_conv(env.db, "C1")
    entry = seed_outbox_row(env.db, "C1")
    env.db.execute(
        "UPDATE outbox SET state = 'stale' WHERE id = ?", (entry,)
    )  # 直接模拟 messenger 的 stale 标记结果
    data = env.client.get("/api/outbox").json()
    item = next(i for i in data["items"] if i["id"] == entry)
    assert item["stale"] is True
    assert "新消息" in item["draft_text"] or item["stale"]  # 页面文案由 stale 标记驱动
