"""S5c 统计口径测试：构造已知数据集 → GET /stats 四指标+分布（§10.4 公式手算）。

覆盖（任务书 S5c 测试第 3 项 / A-CORE-16 / B16）：

- 每日投递数（北京自然日；unknown 行无 sent_at 时回退评估时间戳）；
- 回复率（≥1 条 HR 入站消息的投递会话 / 已发送投递；窗口 7/30 可切、
  窗口外数据不计）；
- 通过率（被正式投递引用的评估 / 评估总数 + skipped_by 归因细分）；
- 匹配度分布（已投递评估分 10 分一档，100 并入 90-100）；
- 会话状态分布（FR8.2：手动标记优先 + SYSTEM 已读/HR 拒绝词/关闭信号/
  面试邀约待确认的保守推断）；
- 投递记录列表：状态筛选/公司搜索/effective_status/手动标记。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from core.s4d_support import BASE_TIME

from .s5c_support import (
    OpsTestEnv,
    seed_application,
    seed_conv,
    seed_evaluation,
    seed_job,
    seed_message,
)


@pytest.fixture()
def env() -> Iterator[OpsTestEnv]:
    e = OpsTestEnv()  # 无编排器：纯数据面统计
    yield e
    e.close()


def _dataset(env: OpsTestEnv) -> None:
    """构造数据集（基准时刻 BASE_TIME=2026-11-05 10:00 UTC，北京 18:00）。

    - 当日（北京 11-05，窗口起点 11-04 16:00 UTC）正式投递 3 条：
      J1/J2 已发送（sent_at = 1 小时前，同北京日），J3 无 sent_at
      （unknown，评估时间在当日 → 回退口径计入当日投递数）；
    - 窗口 7 天内已发送共 3 条（J1/J2/J5），其中 J1/J2 会话有 HR 入站；
      J4 发送于 40 天前（窗口外）；
    - 评估（dry_run=0，窗口内）共 5 条：J1/J2/J3 被投递引用（通过 3），
      J5 low_score、J1 二评 verdict_skip 跳过；另有 40 天前 1 条（窗口外）；
      试运行评估 1 条（dry_run=1，不计通过率分母）；
    - 会话：C1=已读不回（SYSTEM 已读）、C2=明确拒绝（HR 拒绝词）、
      C3=已关闭（SYSTEM 关闭）、C4=待确认面试（INTERVIEW_CARD）、
      C5=手动标记 interviewed（applications.status）优先于其已读信号。
    """
    db = env.db
    recent = BASE_TIME - timedelta(hours=1)
    outside = BASE_TIME - timedelta(days=40)
    for job in ("J1", "J2", "J3", "J4", "J5"):
        seed_job(db, job)
    e1 = seed_evaluation(db, "J1", score=95, created_at=recent)
    e2 = seed_evaluation(db, "J2", score=85, created_at=recent)
    e3 = seed_evaluation(db, "J3", score=62, verdict="neutral", created_at=BASE_TIME)
    seed_evaluation(db, "J4", score=100, created_at=outside)  # 窗口外评估
    seed_evaluation(db, "J5", score=30, verdict="skip", skipped_by="low_score", created_at=recent)
    seed_evaluation(
        db, "J1", verdict="skip", skipped_by="verdict_skip", created_at=recent
    )  # J1 二次评估（未投递引用）
    seed_evaluation(db, "JX", score=99, dry_run=1, created_at=recent)  # 试运行评估

    seed_application(db, "J1", eval_id=e1, status="sent", sent_at=recent)
    seed_application(db, "J2", eval_id=e2, status="sent", sent_at=recent)
    seed_application(db, "J3", eval_id=e3, status="unknown", sent_at=None)
    seed_application(db, "J4", status="sent", sent_at=outside)
    # C5 手动标记（优先于推断）：interviewed
    seed_application(db, "J5", status="interviewed", sent_at=recent)

    seed_conv(db, "C1", job_id="J1")
    seed_conv(db, "C2", job_id="J2")
    seed_conv(db, "C3", job_id="J3")
    seed_conv(db, "C4", job_id="J4")
    seed_conv(db, "C5", job_id="J5")
    seed_message(db, "M1", "C1", sender="system", msg_type="SYSTEM", text="消息已读")
    seed_message(db, "M2", "C1", sender="hr", text="您好，我们看过简历了")  # HR 回复
    seed_message(db, "M3", "C2", sender="hr", text="很遗憾，与岗位不匹配")
    seed_message(db, "M4", "C3", sender="system", msg_type="SYSTEM", text="会话已关闭")
    seed_message(db, "M5", "C4", sender="hr", msg_type="INTERVIEW_CARD", text="")
    seed_message(db, "M6", "C5", sender="system", msg_type="SYSTEM", text="消息已读")  # 被手动覆盖


def test_stats_daily_count_beijing_day(env: OpsTestEnv) -> None:
    _dataset(env)
    data = env.client.get("/api/stats", params={"window": 7}).json()
    # 当日（北京 11-05）：J1/J2（sent_at=1 小时前）+ J3（unknown 无 sent_at →
    # 回退评估 created_at 当日）+ J5（interviewed 行 sent_at 当日）= 4；
    # J4 在 40 天前
    assert data["daily_count"] == 4


def test_stats_reply_rate_window(env: OpsTestEnv) -> None:
    _dataset(env)
    data = env.client.get("/api/stats", params={"window": 7}).json()
    rr = data["reply_rate"]
    # 已发送（sent_at 非空）窗口内：J1/J2/J5(interviewed 也有 sent_at)=3
    # 有 HR 入站消息的关联会话：C1(J1)=有、C2(J2)=有 → 2
    assert rr["sent_total"] == 3
    assert rr["replied_sessions"] == 2
    assert rr["rate"] == round(2 / 3, 4)
    # 窗口 30 同（J4 仍在外：40 天前）
    data30 = env.client.get("/api/stats", params={"window": 30}).json()
    assert data30["reply_rate"]["sent_total"] == 3


def test_stats_pass_rate_and_skipped_by(env: OpsTestEnv) -> None:
    _dataset(env)
    data = env.client.get("/api/stats", params={"window": 7}).json()
    pr = data["pass_rate"]
    # 窗口内 dry_run=0 评估：J1(95)/J2(85)/J3(62)/J5(low_score)/J1(verdict_skip)=5
    assert pr["evaluated_total"] == 5
    # 被正式投递引用：J1/J2/J3 = 3（J5 行是手动标记 interviewed 但无 eval_id）
    assert pr["delivered"] == 3
    assert pr["rate"] == round(3 / 5, 4)
    assert pr["skipped_by"] == {"low_score": 1, "verdict_skip": 1}


def test_stats_score_distribution(env: OpsTestEnv) -> None:
    _dataset(env)
    data = env.client.get("/api/stats", params={"window": 7}).json()
    dist = data["score_distribution"]
    # 已投递（窗口内 sent 的 J1/J2 + J5 无 eval）：J1=95、J2=85；J3 unknown 无
    # sent_at 不入分布（口径=已发送投递）；J4 窗口外
    assert dist["90-100"] == 1 and dist["80-89"] == 1
    assert sum(dist.values()) == 2


def test_stats_conversation_status_distribution(env: OpsTestEnv) -> None:
    _dataset(env)
    data = env.client.get("/api/stats", params={"window": 7}).json()
    cs = data["conversation_status"]
    assert cs["read_no_reply"] == 1  # C1
    assert cs["rejected"] == 1  # C2（HR 拒绝词）
    assert cs["closed"] == 1  # C3
    assert cs["communicating"] == 1  # C4（面试邀约仅提示，不自动标记）
    assert cs["interviewed"] == 1  # C5 手动标记优先于已读信号
    assert cs["interview_pending_confirm"] == 1  # C4 的 INTERVIEW_CARD
    # FR8.2 推断规则表随响应提供（页面展示）
    assert any(r["status"] == "interviewed" for r in data["fr82_rules"])


def test_score_100_goes_to_top_bin(env: OpsTestEnv) -> None:
    seed_job(env.db, "J9")
    eval_id = seed_evaluation(env.db, "J9", score=100)
    seed_application(env.db, "J9", eval_id=eval_id)
    dist = env.client.get("/api/stats", params={"window": 7}).json()["score_distribution"]
    assert dist["90-100"] == 1 and dist.get("100-109") is None


def test_applications_filters_and_effective_status(env: OpsTestEnv) -> None:
    _dataset(env)
    # 默认 dry_run=0；公司搜索命中 company_key
    data = env.client.get("/api/applications", params={"company": "ck-J1"}).json()
    assert data["total"] == 1 and data["items"][0]["job_id"] == "J1"
    assert data["items"][0]["company_name"] == "示例科技"
    assert data["items"][0]["title"] == "Python 后端"
    # 状态筛选（存储值）
    data = env.client.get("/api/applications", params={"status": "unknown"}).json()
    assert [i["job_id"] for i in data["items"]] == ["J3"]
    # effective_status：sent + 会话推断（C1 已读 → read_no_reply）；
    # 手动 interviewed；unknown 无关联信号 → communicating；标题搜索
    data = env.client.get("/api/applications", params={"company": "Python"}).json()
    effective = {i["job_id"]: i["effective_status"] for i in data["items"]}
    assert effective["J1"] == "read_no_reply"
    assert effective["J5"] == "interviewed"
    assert effective["J3"] == "closed"  # unknown 行走会话推断（C3 关闭信号）
    assert effective["J2"] == "rejected"


def test_manual_status_marking_priority(env: OpsTestEnv) -> None:
    """手动标记永远优先：标记后 effective_status 与会话分布立即生效。"""
    _dataset(env)
    app_id = env.db.query_one("SELECT id FROM applications WHERE job_id = 'J1'")["id"]
    resp = env.client.post(f"/api/applications/{app_id}/status", json={"status": "closed"})
    assert resp.status_code == 200 and resp.json()["status"] == "closed"
    row = env.client.get("/api/applications", params={"company": "ck-J1"}).json()
    assert row["items"][0]["effective_status"] == "closed"
    cs = env.client.get("/api/stats", params={"window": 7}).json()["conversation_status"]
    assert cs["closed"] == 2 and cs["read_no_reply"] == 0  # C1 手动覆盖已读推断
    # 负例：非法状态 422、不存在 404、dry_run 行 409
    bad = env.client.post(f"/api/applications/{app_id}/status", json={"status": "x"})
    assert bad.status_code == 422
    missing = env.client.post("/api/applications/999/status", json={"status": "closed"})
    assert missing.status_code == 404
    dry_id = seed_application(env.db, "JD", status="skipped_dryrun", dry_run=1)
    on_dry = env.client.post(f"/api/applications/{dry_id}/status", json={"status": "closed"})
    assert on_dry.status_code == 409


def test_dryrun_rows_excluded_and_flagged(env: OpsTestEnv) -> None:
    seed_job(env.db, "JD")
    seed_application(env.db, "JD", status="skipped_dryrun", dry_run=1)
    assert env.client.get("/api/applications").json()["total"] == 0  # 默认正式
    data = env.client.get("/api/applications", params={"dry_run": 1}).json()
    assert data["total"] == 1 and data["items"][0]["effective_status"] == "planned"
    # dry_run 参数非法 → 422
    assert env.client.get("/api/applications", params={"dry_run": 2}).status_code == 422
