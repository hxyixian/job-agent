"""S5c 投递记录与看板 API（§10.2 记录行；§10.4 统计口径 [B16]；G7 标注工具）。

- ``GET /api/applications``：投递记录列表（状态筛选 + 公司搜索 + dry_run
  切换 + 分页；岗位标题/公司名取自 jobs 缓存，评估分取自 evaluations）；
- ``POST /api/applications/{id}/status``：手动标记投递情况（FR8.2 状态集；
  **手动标记永远优先于自动推断**——标记值直接写 applications.status
  自由文本列，0001 迁移注释即为此预留）；
- ``GET /api/stats?window=7|30``：§10.4 四指标 + 两个分布（口径见各
  查询 docstring；A-CORE-16 构造数据对公式）+ FR8.2 推断规则表展示；
- ``GET /api/evaluations``：评估标注视图数据（含 user_label）；
- ``GET /api/evaluations/export`` / ``POST /api/evaluations/labels/import``：
  G7 标注工具往返（CSV；A-CORE-15）。

FR8.2 状态码（稳定英文码，页面映射中文）：``communicating`` 沟通中 /
``read_no_reply`` 已读不回 / ``rejected`` 明确拒绝 / ``closed`` 已关闭 /
``interviewed`` 已面试。推断（保守，§10.4）：SYSTEM 已读 → read_no_reply；
HR 文本命中拒绝关键词 → rejected；会话关闭 SYSTEM 信号 → closed；检测到
面试邀约只提示确认（interview_pending_confirm），不自动标记 interviewed。
"""

from __future__ import annotations

import csv
import io
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from job_agent.core.messenger import infer_system_status
from job_agent.db import Database
from job_agent.models import MsgType
from job_agent.timeutil import day_start_utc

from .context import OpsContext, company_of_detail

__all__ = ["attach_records_routes"]

#: FR8.2 投递情况状态集（手动标记合法值；与流水线状态 sent/failed/unknown/
#: skipped_dryrun 无交集）
FR82_STATUSES: tuple[str, ...] = (
    "communicating",
    "read_no_reply",
    "rejected",
    "closed",
    "interviewed",
)
#: §10.4 HR 拒绝关键词（保守推断，其余保持"沟通中"）
_REJECT_MARKERS: tuple[str, ...] = ("不合适", "不匹配", "遗憾", "另寻")
#: §10.4 推断规则表（页面展示用，A-CORE 对齐 §10.4 表）
FR82_RULES: list[dict[str, str]] = [
    {"signal": "SYSTEM 消息含「已读」", "status": "read_no_reply"},
    {"signal": "HR 文本命中拒绝关键词（不合适/不匹配/遗憾/另寻）", "status": "rejected"},
    {"signal": "会话关闭 SYSTEM 信号", "status": "closed"},
    {
        "signal": "存在面试邀约且用户确认",
        "status": "interviewed",
        "note": "半自动：仅提示用户确认，不自动标记",
    },
]

_LABEL_VALUES = ("want", "not_want", "")


class ApplicationStatusBody(BaseModel):
    """POST /applications/{id}/status 请求体（FR8.2 手动标记）。"""

    status: Literal["communicating", "read_no_reply", "rejected", "closed", "interviewed"]


# ---- FR8.2 推断（保守；手动标记优先）----


def _manual_status_by_job(db: Database) -> dict[str, str]:
    """job_id → 最新手动标记（仅 dry_run=0 且 status ∈ FR8.2 集）。"""
    placeholders = ", ".join("?" for _ in FR82_STATUSES)
    rows = db.query(
        f"SELECT job_id, status FROM applications WHERE dry_run = 0"
        f" AND status IN ({placeholders}) ORDER BY id",
        FR82_STATUSES,
    )
    return {str(r["job_id"]): str(r["status"]) for r in rows}


def _conversation_signals(db: Database) -> dict[str, dict[str, Any]]:
    """conv_id → 推断信号聚合（closed / read / rejected / interview_invite）。"""
    signals: dict[str, dict[str, Any]] = {}
    rows = db.query(
        "SELECT conv_id, sender, direction, msg_type, text FROM messages"
        " WHERE sender = 'system' OR (direction = 'in' AND sender = 'hr')"
    )
    for r in rows:
        conv_id, sender = str(r["conv_id"]), str(r["sender"])
        sig = signals.setdefault(
            conv_id, {"closed": False, "read": False, "rejected": False, "interview": False}
        )
        text = str(r["text"] or "")
        if sender == "system":
            inferred = infer_system_status(text)
            if inferred == "closed":
                sig["closed"] = True
            elif inferred == "read_no_reply":
                sig["read"] = True
        elif str(r["msg_type"]) == MsgType.INTERVIEW_CARD.value:
            sig["interview"] = True
        elif any(marker in text for marker in _REJECT_MARKERS):
            sig["rejected"] = True
    return signals


def infer_status(signals: dict[str, dict[str, Any]] | None, manual: str | None) -> tuple[str, bool]:
    """(推断或手动状态, 存在待确认面试邀约)——手动优先，其余保守推断。"""
    if manual is not None:
        return manual, False
    if signals is None:
        return "communicating", False
    pending = bool(signals.get("interview"))
    if signals.get("closed"):
        return "closed", pending
    if signals.get("rejected"):
        return "rejected", pending
    if signals.get("read"):
        return "read_no_reply", pending
    return "communicating", pending


def _conversation_status_map(
    db: Database,
) -> tuple[dict[str, tuple[str, bool]], dict[str, tuple[str, bool]], dict[str, str]]:
    """(conv_id → (状态, 待确认邀约), job_id → (状态, 待确认邀约), job_id → 手动标记)。

    job_id 视图供投递记录行关联（conversations.job_id ↔ applications.job_id
    [F1 关联依据]；同岗位多会话取任意一条的非手动推断即可——推断信号
    按会话聚合，取不到时保守回落 communicating）。
    """
    signals = _conversation_signals(db)
    manual = _manual_status_by_job(db)
    by_conv: dict[str, tuple[str, bool]] = {}
    by_job: dict[str, tuple[str, bool]] = {}
    for row in db.query("SELECT conv_id, job_id FROM conversations"):
        conv_id = str(row["conv_id"])
        job_id = row["job_id"]
        status = infer_status(
            signals.get(conv_id), manual.get(str(job_id)) if job_id is not None else None
        )
        by_conv[conv_id] = status
        if job_id is not None and str(job_id) not in by_job:
            by_job[str(job_id)] = status
    return by_conv, by_job, manual


def _effective_application_status(
    row: Any, job_status: dict[str, tuple[str, bool]], manual: dict[str, str]
) -> str:
    """单条投递记录的展示状态：手动标记 > 会话推断 > 流水线状态。"""
    stored = str(row["status"])
    if int(row["dry_run"]) == 1:
        return "planned"  # 拟发送（试运行行不参与投递情况 [N2]）
    if stored in FR82_STATUSES:
        return stored
    if stored in ("sent", "unknown"):
        job_id = str(row["job_id"])
        if job_id in manual:
            return manual[job_id]
        return job_status.get(job_id, ("communicating", False))[0]
    return stored  # failed 等流水线状态原样展示


def _score_bin(score: int) -> str:
    """10 分一档的分桶键（0-9 … 80-89、90-100；100 并入末档）。"""
    idx = min(score // 10, 9)
    return f"{idx * 10}-{idx * 10 + 9}" if idx < 9 else "90-100"


# ---- 路由注册 ----


def attach_records_routes(router: APIRouter, ctx: OpsContext, window_start: Any) -> None:
    """注册投递记录/看板/标注路由（由 ops.create_ops_router 调用）。"""

    @router.get("/applications")
    def get_applications(
        status: str | None = None,
        company: str | None = None,
        dry_run: int = 0,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """投递记录（status 精确匹配存储值；company 模糊匹配公司键/岗位标题/
        详情 JSON；dry_run 默认 0——正式记录页，1=试运行行）。"""
        if dry_run not in (0, 1):
            raise HTTPException(status_code=422, detail="dry_run_must_be_0_or_1")
        conditions = ["a.dry_run = ?"]
        params: list[Any] = [dry_run]
        if status is not None:
            conditions.append("a.status = ?")
            params.append(status)
        if company:
            pattern = f"%{company}%"
            conditions.append(
                "(a.company_key LIKE ? OR EXISTS (SELECT 1 FROM jobs j WHERE j.job_id = a.job_id"
                " AND (j.title LIKE ? OR j.detail_json LIKE ?)))"
            )
            params.extend([pattern, pattern, pattern])
        where = " AND ".join(conditions)
        total = ctx.db.query_one(f"SELECT COUNT(*) AS n FROM applications a WHERE {where}", params)
        rows = ctx.db.query(
            f"SELECT a.id, a.job_id, a.company_key, a.greeting_version, a.eval_id, a.status,"
            f" a.dry_run, a.sent_at FROM applications a WHERE {where}"
            f" ORDER BY a.sent_at IS NULL, a.sent_at DESC, a.id DESC LIMIT ? OFFSET ?",
            (*params, max(1, min(limit, 200)), max(0, offset)),
        )
        _conv_status, job_status, manual = _conversation_status_map(ctx.db)
        jobs = _jobs_index(ctx.db, [str(r["job_id"]) for r in rows])
        evals = _evals_index(ctx.db, [r["eval_id"] for r in rows if r["eval_id"] is not None])
        items = []
        for r in rows:
            job = jobs.get(str(r["job_id"]), {})
            evaluation = evals.get(int(r["eval_id"]), {}) if r["eval_id"] is not None else {}
            items.append(
                {
                    "id": int(r["id"]),
                    "job_id": str(r["job_id"]),
                    "title": job.get("title", ""),
                    "company_name": job.get("company_name", ""),
                    "company_key": str(r["company_key"]),
                    "status": str(r["status"]),
                    "effective_status": _effective_application_status(r, job_status, manual),
                    "sent_at": str(r["sent_at"]) if r["sent_at"] else None,
                    "dry_run": int(r["dry_run"]),
                    "greeting_version": r["greeting_version"],
                    "eval_score": evaluation.get("score"),
                    "eval_verdict": evaluation.get("verdict"),
                }
            )
        return {
            "items": items,
            "total": int(total["n"]) if total is not None else 0,
            "limit": limit,
            "offset": offset,
        }

    @router.post("/applications/{app_id}/status")
    def post_application_status(app_id: int, body: ApplicationStatusBody) -> dict[str, Any]:
        """手动标记投递情况（FR8.2；永远优先于自动推断）。"""
        row = ctx.db.query_one(
            "SELECT id, dry_run, status FROM applications WHERE id = ?", (app_id,)
        )
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        if int(row["dry_run"]) == 1:
            raise HTTPException(status_code=409, detail="dryrun_row_not_markable")
        ctx.db.execute("UPDATE applications SET status = ? WHERE id = ?", (body.status, app_id))
        return {"id": app_id, "status": body.status}

    @router.get("/stats")
    def get_stats(window: int = 7) -> dict[str, Any]:
        """§10.4 看板指标（B16；构造数据对公式，A-CORE-16）。

        - 每日投递数：当日 ``applications(dry_run=0)`` 行数（时间戳取
          ``COALESCE(sent_at, evaluations.created_at)``——failed/unknown 行
          无 sent_at，回退评估时间；北京自然日窗口 [R6-4]）；
        - 回复率：窗口内已发送投递中，关联会话收到 ≥1 条 HR 入站消息的
          会话数 / 已发送投递数（关联 = conversations.job_id ↔
          applications.job_id [F1]）；
        - 通过率：窗口内被正式投递引用的评估数 / 窗口内评估总数
          （evaluations.dry_run=0）；按 skipped_by 归因细分；
        - 匹配度分布：已投递岗位评估分直方图（10 分一档，90-100 含 100）；
        - 会话状态分布：全量 conversations 按 FR8.2（手动优先 + 保守推断）。

        ``window``：查询参数为字符串文本，先 int 解析再白名单 {7, 30}
        （Literal[int] 不接受字符串查询值，故手动校验 → 422）。
        """
        if window not in (7, 30):
            raise HTTPException(status_code=422, detail="window_must_be_7_or_30")
        now = ctx.clock()
        since = window_start(now, window)
        db = ctx.db
        daily = db.query_one(
            "SELECT COUNT(*) AS n FROM applications a LEFT JOIN evaluations e"
            " ON e.id = a.eval_id WHERE a.dry_run = 0"
            " AND COALESCE(a.sent_at, e.created_at) >= ?",
            (day_start_utc(now),),
        )
        sent_total = db.query_one(
            "SELECT COUNT(*) AS n FROM applications"
            " WHERE dry_run = 0 AND sent_at IS NOT NULL AND sent_at >= ?",
            (since,),
        )
        replied = db.query_one(
            "SELECT COUNT(DISTINCT c.conv_id) AS n FROM conversations c"
            " WHERE c.job_id IN (SELECT job_id FROM applications"
            "   WHERE dry_run = 0 AND sent_at IS NOT NULL AND sent_at >= ?)"
            " AND EXISTS (SELECT 1 FROM messages m WHERE m.conv_id = c.conv_id"
            "   AND m.direction = 'in' AND m.sender = 'hr')",
            (since,),
        )
        eval_total = db.query_one(
            "SELECT COUNT(*) AS n FROM evaluations WHERE dry_run = 0 AND created_at >= ?",
            (since,),
        )
        delivered = db.query_one(
            "SELECT COUNT(DISTINCT a.eval_id) AS n FROM applications a"
            " JOIN evaluations e ON e.id = a.eval_id"
            " WHERE a.dry_run = 0 AND a.eval_id IS NOT NULL AND e.created_at >= ?",
            (since,),
        )
        skipped: dict[str, int] = {}
        for r in db.query(
            "SELECT skipped_by, COUNT(*) AS n FROM evaluations"
            " WHERE dry_run = 0 AND created_at >= ? AND skipped_by IS NOT NULL"
            " GROUP BY skipped_by",
            (since,),
        ):
            skipped[str(r["skipped_by"])] = int(r["n"])
        scores = [
            int(r["score"])
            for r in db.query(
                "SELECT e.score AS score FROM applications a"
                " JOIN evaluations e ON e.id = a.eval_id"
                " WHERE a.dry_run = 0 AND a.sent_at >= ? AND e.score IS NOT NULL",
                (since,),
            )
        ]
        distribution = {_score_bin(i * 10): 0 for i in range(10)}
        for score in scores:
            distribution[_score_bin(score)] += 1
        conv_status, _job_status, _manual = _conversation_status_map(db)
        status_counts = dict.fromkeys(FR82_STATUSES, 0)
        interview_pending = 0
        for status, pending in conv_status.values():
            status_counts[status] = status_counts.get(status, 0) + 1
            if pending and status != "interviewed":
                interview_pending += 1
        sent_n = int(sent_total["n"]) if sent_total is not None else 0
        replied_n = int(replied["n"]) if replied is not None else 0
        eval_n = int(eval_total["n"]) if eval_total is not None else 0
        delivered_n = int(delivered["n"]) if delivered is not None else 0
        return {
            "window_days": window,
            "daily_count": int(daily["n"]) if daily is not None else 0,
            "reply_rate": {
                "replied_sessions": replied_n,
                "sent_total": sent_n,
                "rate": round(replied_n / sent_n, 4) if sent_n else None,
            },
            "pass_rate": {
                "delivered": delivered_n,
                "evaluated_total": eval_n,
                "rate": round(delivered_n / eval_n, 4) if eval_n else None,
                "skipped_by": skipped,
            },
            "score_distribution": distribution,
            "conversation_status": {
                **status_counts,
                "interview_pending_confirm": interview_pending,
            },
            "fr82_rules": FR82_RULES,
            "generated_at": ctx.clock().isoformat(),
        }

    # ---- G7 标注工具（A-CORE-15 往返）----

    @router.get("/evaluations")
    def get_evaluations(labeled: bool | None = None, limit: int = 100) -> dict[str, Any]:
        """评估标注视图（label 视图数据源；labeled 过滤可选）。"""
        conditions: list[str] = []
        params: list[Any] = []
        if labeled is True:
            conditions.append("user_label IS NOT NULL")
        elif labeled is False:
            conditions.append("user_label IS NULL")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = ctx.db.query(
            "SELECT e.id, e.job_id, j.title, e.score, e.verdict, e.reason, e.skipped_by,"
            f" e.user_label, e.dry_run, e.created_at FROM evaluations e"
            f" LEFT JOIN jobs j ON j.job_id = e.job_id {where}"
            " ORDER BY e.id DESC LIMIT ?",
            (*params, max(1, min(limit, 500))),
        )
        items = [
            {
                "id": int(r["id"]),
                "job_id": str(r["job_id"]),
                "title": str(r["title"] or ""),
                "score": r["score"],
                "verdict": r["verdict"],
                "reason": str(r["reason"] or ""),
                "skipped_by": r["skipped_by"],
                "user_label": r["user_label"],
                "dry_run": int(r["dry_run"]),
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]
        total = ctx.db.query_one("SELECT COUNT(*) AS n FROM evaluations")
        labeled_n = ctx.db.query_one(
            "SELECT COUNT(*) AS n FROM evaluations WHERE user_label IS NOT NULL"
        )
        return {
            "items": items,
            "total": int(total["n"]) if total is not None else 0,
            "labeled": int(labeled_n["n"]) if labeled_n is not None else 0,
        }

    @router.get("/evaluations/export")
    def get_export() -> Response:
        """导出评估标注 CSV（G7/L1：want/not_want 往返的导出侧）。"""
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "evaluation_id",
                "job_id",
                "score",
                "verdict",
                "reason",
                "skipped_by",
                "user_label",
                "dry_run",
                "created_at",
            ]
        )
        for r in ctx.db.query(
            "SELECT id, job_id, score, verdict, reason, skipped_by, user_label, dry_run,"
            " created_at FROM evaluations ORDER BY id"
        ):
            writer.writerow(
                [
                    r["id"],
                    r["job_id"],
                    "" if r["score"] is None else r["score"],
                    "" if r["verdict"] is None else r["verdict"],
                    "" if r["reason"] is None else r["reason"],
                    "" if r["skipped_by"] is None else r["skipped_by"],
                    "" if r["user_label"] is None else r["user_label"],
                    r["dry_run"],
                    r["created_at"],
                ]
            )
        # utf-8-sig BOM：Excel 直接打开中文不乱码
        return Response(
            content="\ufeff" + buffer.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="evaluations_labels.csv"'},
        )

    @router.post("/evaluations/labels/import")
    async def post_import(request: Request) -> dict[str, Any]:
        """导入标注 CSV（与导出同格式；evaluation_id + user_label 必需）。

        - ``user_label`` ∈ {want, not_want, 空}（空 = 清除标注）；
        - 未知 evaluation_id 记入 ``not_found``；行格式问题记入 ``invalid``；
          二者不阻断其余行的导入。
        """
        raw = await request.body()
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = {(name or "").strip().lower() for name in (reader.fieldnames or [])}
        if "evaluation_id" not in fieldnames or "user_label" not in fieldnames:
            raise HTTPException(status_code=422, detail="csv_columns_required")
        updated, cleared = 0, 0
        not_found: list[str] = []
        invalid: list[str] = []
        for row in reader:
            raw_id = (row.get("evaluation_id") or "").strip()
            label = (row.get("user_label") or "").strip().lower()
            if not raw_id.isdigit() or label not in _LABEL_VALUES:
                invalid.append(raw_id or "<empty>")
                continue
            row_id = int(raw_id)
            exists = ctx.db.query_one("SELECT 1 AS x FROM evaluations WHERE id = ?", (row_id,))
            if exists is None:
                not_found.append(raw_id)
                continue
            if label == "":
                ctx.db.execute("UPDATE evaluations SET user_label = NULL WHERE id = ?", (row_id,))
                cleared += 1
            else:
                ctx.db.execute(
                    "UPDATE evaluations SET user_label = ? WHERE id = ?", (label, row_id)
                )
                updated += 1
        return {"updated": updated, "cleared": cleared, "not_found": not_found, "invalid": invalid}


# ---- 批量索引助手 ----


def _jobs_index(db: Database, job_ids: list[str]) -> dict[str, dict[str, Any]]:
    unique = sorted(set(job_ids))
    if not unique:
        return {}
    placeholders = ", ".join("?" for _ in unique)
    rows = db.query(
        f"SELECT job_id, title, detail_json FROM jobs WHERE job_id IN ({placeholders})", unique
    )
    return {
        str(r["job_id"]): {
            "title": str(r["title"]),
            "company_name": company_of_detail(str(r["detail_json"])),
        }
        for r in rows
    }


def _evals_index(db: Database, eval_ids: list[int]) -> dict[int, dict[str, Any]]:
    unique = sorted(set(eval_ids))
    if not unique:
        return {}
    placeholders = ", ".join("?" for _ in unique)
    rows = db.query(
        f"SELECT id, score, verdict FROM evaluations WHERE id IN ({placeholders})", unique
    )
    return {int(r["id"]): {"score": r["score"], "verdict": r["verdict"]} for r in rows}
