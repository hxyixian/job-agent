"""S5c 控制台测试支撑：四页路由最小装配 + 数据种子（TEST1 全离线）。

装配方式与生产一致：``register_ops_routes``（S5c 工厂）+ S5a 的
``api.status`` 路由（经 ``app.state.console`` 的 ConsoleContext，覆盖
/api/status 扩展字段）。S5a 六项加固中间件不在此层（test_hardening 已覆盖）。

orchestrator 集成（指令 ack / outbox 消费）复用 ``tests/unit/core/
s4d_support.py`` 的 FakeAdapter / FakeClock / wide_config。
"""

from __future__ import annotations

import json
import queue
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from core.s4d_support import BASE_TIME, FakeAdapter, FakeClock, wide_config
from fastapi import FastAPI
from fastapi.testclient import TestClient

from job_agent.ai.classify import ClassifyResult
from job_agent.config import Config
from job_agent.console.api import status as status_routes
from job_agent.console.api.context import OpsContext
from job_agent.console.api.ops import create_ops_router
from job_agent.console.context import ConsoleContext
from job_agent.console.snapshot import ReadOnlyDatabase
from job_agent.core.messenger import Messenger
from job_agent.core.orchestrator import Orchestrator
from job_agent.core.session import ConversationState
from job_agent.core.state_machine import SendStateMachine
from job_agent.db import Database, open_database
from job_agent.timeutil import utc_str

__all__ = [
    "OpsTestEnv",
    "seed_application",
    "seed_conv",
    "seed_evaluation",
    "seed_job",
    "seed_message",
    "seed_outbox_row",
]


class OpsTestEnv:
    """一个临时库 + S5c 路由 + TestClient（可选绑定真实 Orchestrator）。"""

    def __init__(
        self,
        *,
        config: Config | None = None,
        clock: Callable[[], datetime] | None = None,
        with_orchestrator: bool = False,
        dry_run: bool = True,
    ) -> None:
        import tempfile

        tmp = tempfile.TemporaryDirectory(prefix="job_agent_s5c_")
        self._tmp = tmp
        self.db: Database = open_database(Path(tmp.name) / "ops.db")
        self.config = config if config is not None else wide_config(dry_run=dry_run)
        self.queue: queue.Queue[Any] = queue.Queue()
        self.clock_fn = clock if clock is not None else FakeClock(BASE_TIME)
        self.adapter = FakeAdapter()
        self.orch: Orchestrator | None = None
        self.sent_sleeps: list[float] = []
        if with_orchestrator:
            self.orch = Orchestrator(
                self.db,
                self.adapter,
                config_loader=lambda: self.config,
                command_queue=self.queue,  # type: ignore[arg-type]
                clock=self.clock_fn,
                sleep=self.sent_sleeps.append,
                rng=lambda lo, hi: 0.0,
            )
        self.ctx = OpsContext(
            db=self.db,
            command_queue=self.queue,
            config_loader=lambda: self.config,
            classify_fn=lambda msgs: ClassifyResult("low_risk", "测试注入"),
            draft_fn=lambda ctx, category: f"重拟草稿({category})",
            clock=self.clock_fn,
        )
        app = FastAPI()
        app.state.console = ConsoleContext(
            db=ReadOnlyDatabase(self.db),
            command_queue=self.queue,
            config_loader=lambda: self.config,
        )
        app.include_router(status_routes.router)
        app.include_router(create_ops_router(self.ctx))
        self.client = TestClient(app)

    def bind_messenger(self) -> Messenger:
        """给 Orchestrator 绑定 Messenger（approve 流的引擎侧执行体）。"""
        assert self.orch is not None
        messenger = Messenger(
            self.db,
            self.adapter,
            config_provider=lambda: self.config,
            gate_provider=lambda: self.orch.gate if self.orch is not None else None,
            session=ConversationState(self.db, self.config),
            intents=SendStateMachine(self.db),
            classify_fn=lambda msgs: ClassifyResult("low_risk", "测试注入"),
            draft_fn=lambda ctx, category: "自动草稿",
            clock=self.clock_fn,
            sleep=lambda _s: None,
        )
        self.orch.bind_messenger(messenger)
        return messenger

    def tick(self) -> None:
        assert self.orch is not None
        self.orch.tick()

    def close(self) -> None:
        self.db.close()
        self._tmp.cleanup()


# ---- 数据种子（显式时间戳，统计窗口口径可控）----


def seed_job(
    db: Database, job_id: str, title: str = "Python 后端", company: str = "示例科技"
) -> None:
    detail = {
        "job_id": job_id,
        "title": title,
        "company_name": company,
        "company_key": f"ck-{job_id}",
    }
    db.execute(
        "INSERT INTO jobs (job_id, title, company_key, detail_json) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(job_id) DO UPDATE SET title = excluded.title,"
        " detail_json = excluded.detail_json",
        (job_id, title, f"ck-{job_id}", json.dumps(detail, ensure_ascii=False)),
    )


def seed_evaluation(
    db: Database,
    job_id: str,
    *,
    score: int | None = 75,
    verdict: str | None = "recommend",
    reason: str = "匹配",
    skipped_by: str | None = None,
    user_label: str | None = None,
    dry_run: int = 0,
    created_at: datetime = BASE_TIME,
) -> int:
    return db.execute_returning_id(
        "INSERT INTO evaluations (job_id, score, verdict, reason, skipped_by, user_label,"
        " dry_run, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, score, verdict, reason, skipped_by, user_label, dry_run, utc_str(created_at)),
    )


def seed_application(
    db: Database,
    job_id: str,
    *,
    eval_id: int | None = None,
    status: str = "sent",
    dry_run: int = 0,
    sent_at: datetime | None = BASE_TIME,
    greeting_version: int = 1,
) -> int:
    return db.execute_returning_id(
        "INSERT INTO applications (job_id, company_key, greeting_version, eval_id, status,"
        " dry_run, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            f"ck-{job_id}",
            greeting_version,
            eval_id,
            status,
            dry_run,
            utc_str(sent_at) if sent_at is not None else None,
        ),
    )


def seed_conv(db: Database, conv_id: str, *, job_id: str | None = None, peer: str = "HR甲") -> None:
    db.execute(
        "INSERT INTO conversations (conv_id, peer_name, job_id) VALUES (?, ?, ?)"
        " ON CONFLICT(conv_id) DO UPDATE SET peer_name = excluded.peer_name,"
        " job_id = COALESCE(excluded.job_id, conversations.job_id)",
        (conv_id, peer, job_id),
    )


def seed_message(
    db: Database,
    msg_id: str,
    conv_id: str,
    *,
    direction: str = "in",
    sender: str = "hr",
    msg_type: str = "TEXT",
    text: str = "",
    at: datetime = BASE_TIME,
) -> None:
    db.execute(
        "INSERT INTO messages (platform_msg_id, conv_id, direction, sender, msg_type, text,"
        " sent_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (msg_id, conv_id, direction, sender, msg_type, text, utc_str(at)),
    )


def seed_outbox_row(
    db: Database,
    conv_id: str,
    *,
    draft: str = "草稿内容",
    source: str = "ai",
    state: str = "pending",
    at: datetime = BASE_TIME,
) -> int:
    return db.execute_returning_id(
        "INSERT INTO outbox (conv_id, draft_text, source, state, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (conv_id, draft, source, state, utc_str(at)),
    )
