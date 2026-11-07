"""S5c 标注工具往返测试（G7/L1；A-CORE-15：60 条 → 导出 CSV → 改 → 导入 → 查询）。

覆盖（任务书 S5c 测试第 4 项）：

- 导出：行数=评估数、列头齐全、CSV 引号/逗号/换行转义正确、BOM（Excel
  中文可读）、Content-Disposition 附件头；
- 导入：want/not_want 落库一致、空值清除标注、未知 id 记 not_found、
  非法标签值记 invalid（不阻断其余行）、缺列 422；
- 视图：GET /evaluations 的 labeled 过滤与计数。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator

import pytest

from .s5c_support import OpsTestEnv, seed_evaluation, seed_job

N_ROWS = 60


@pytest.fixture()
def env() -> Iterator[OpsTestEnv]:
    e = OpsTestEnv()
    yield e
    e.close()


def _seed_sixty(env: OpsTestEnv) -> list[int]:
    """60 条评估：标签交错 + 含逗号/引号/换行的 reason（CSV 转义压力）。"""
    ids = []
    for i in range(N_ROWS):
        job = f"JOB{i:03d}"
        seed_job(env.db, job, title=f"岗位{i}")
        label = "want" if i % 3 == 0 else ("not_want" if i % 3 == 1 else None)
        reason = f'理由{i}, 含"引号"\n换行' if i % 10 == 0 else f"普通理由{i}"
        ids.append(
            seed_evaluation(
                env.db,
                job,
                score=(i * 7) % 101,
                verdict=("recommend" if i % 2 else "neutral"),
                reason=reason,
                skipped_by=("low_score" if i % 7 == 0 else None),
                user_label=label,
                dry_run=i % 5 == 0,
            )
        )
    return ids


def _labels_in_db(env: OpsTestEnv) -> dict[int, str | None]:
    rows = env.db.query("SELECT id, user_label FROM evaluations ORDER BY id")
    return {int(r["id"]): (None if r["user_label"] is None else str(r["user_label"])) for r in rows}


def test_export_round_trip_layout(env: OpsTestEnv) -> None:
    ids = _seed_sixty(env)
    resp = env.client.get("/api/evaluations/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.text.startswith("\ufeff")  # utf-8-sig BOM
    reader = csv.DictReader(io.StringIO(resp.text.lstrip("\ufeff")))
    rows = list(reader)
    assert len(rows) == N_ROWS == len(ids)
    assert rows[0]["evaluation_id"] == str(ids[0])
    # 含逗号/引号/换行的 reason 完整往返（csv 模块转义）
    tricky = next(r for r in rows if "引号" in r["reason"])
    assert '含"引号"' in tricky["reason"] and "\n" in tricky["reason"]
    assert tricky["user_label"] in ("want", "not_want", "")


def test_import_updates_labels_matching_csv(env: OpsTestEnv) -> None:
    """A-CORE-15 主链：导出 → 翻转标注 → 导入 → evaluations.user_label 与 CSV 一致。"""
    _seed_sixty(env)
    text = env.client.get("/api/evaluations/export").text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    expected: dict[int, str | None] = {}
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["evaluation_id", "user_id_ignore", "user_label"])
    for row in reader:
        rid = int(row["evaluation_id"])
        if rid % 2 == 0:
            label = "not_want" if row["user_label"] != "not_want" else "want"
        elif row["user_label"]:
            label = ""  # 清除既有标注
        else:
            label = "want" if rid % 3 == 0 else ""
        expected[rid] = label or None
        writer.writerow([rid, "x", label])
    resp = env.client.post(
        "/api/evaluations/labels/import",
        content=out.getvalue(),
        headers={"Content-Type": "text/csv"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["not_found"] == [] and body["invalid"] == []
    assert _labels_in_db(env) == expected  # 与 CSV 逐行一致


def test_import_reports_not_found_and_invalid_rows(env: OpsTestEnv) -> None:
    _seed_sixty(env)
    csv_body = (
        "evaluation_id,user_label\n"
        "1,want\n"
        "999999,want\n"  # 未知 id
        "2,maybe\n"  # 非法标签
        "abc,want\n"  # 非法 id
        "3,not_want\n"
    )
    resp = env.client.post(
        "/api/evaluations/labels/import", content=csv_body, headers={"Content-Type": "text/csv"}
    )
    body = resp.json()
    assert body["updated"] == 2  # 1 与 3
    assert body["not_found"] == ["999999"]
    assert sorted(body["invalid"]) == ["2", "abc"]
    assert _labels_in_db(env)[1] == "want"
    assert _labels_in_db(env)[3] == "not_want"
    assert _labels_in_db(env)[2] == "not_want"  # 非法行未改动原值（id=2 种子即 not_want）


def test_import_rejects_missing_columns(env: OpsTestEnv) -> None:
    resp = env.client.post(
        "/api/evaluations/labels/import",
        content="id,label\n1,want\n",
        headers={"Content-Type": "text/csv"},
    )
    assert resp.status_code == 422


def test_evaluations_view_label_filter(env: OpsTestEnv) -> None:
    _seed_sixty(env)
    data = env.client.get("/api/evaluations").json()
    assert data["total"] == N_ROWS
    assert data["labeled"] == 40  # 2/3 有标注（i%3 ∈ {0,1}）
    unlabeled = env.client.get("/api/evaluations", params={"labeled": "false"}).json()
    assert len(unlabeled["items"]) == 20
    assert all(i["user_label"] is None for i in unlabeled["items"])
    labeled = env.client.get("/api/evaluations", params={"labeled": "true", "limit": 5}).json()
    assert all(i["user_label"] in ("want", "not_want") for i in labeled["items"])
    item = labeled["items"][0]
    assert item["title"] and item["verdict"] in ("recommend", "neutral")
