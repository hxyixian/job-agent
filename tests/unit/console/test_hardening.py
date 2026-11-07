"""S5a 控制台六项加固测试 [A-CORE-14]（设计文档 §10.1；httpx TestClient）。

六项逐条正反例：

1. 仅监听 127.0.0.1：ConsoleConfig Literal 锁定 + uvicorn 装配取值；
2. Host 校验中间件：∈{localhost,127.0.0.1} 放行（带端口/大小写），其余 403；
3. 非 GET 要求 X-Job-Agent: console：缺失/取值不符 403，正确即放行；
4. GET 无副作用：重复 GET 响应一致 + DB 内容零变化 + 只读视图写阻断；
5. 敏感字段打码：****尾4位（短值全打码；空值=未配置不误导）+ 响应中间件兜底；
6. 不注册 CORSMiddleware + OPTIONS 一律 405 + Origin 存在时空或同源。

另覆盖：异常统一 JSON 信封、指令队列注入接口（§5.5 I6）、静态目录挂载。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from job_agent import __version__
from job_agent.config import Config, ConsoleConfig
from job_agent.console import (
    ConsoleHardeningMiddleware,
    create_app,
    issue_command,
    mask_sensitive_fields,
    serve_console,
)
from job_agent.console.snapshot import ReadAttemptError, ReadOnlyDatabase
from job_agent.core import runstate
from job_agent.core.orchestrator import (
    KEY_ENGINE_STATE,
    KEY_LAST_COMMAND,
    EngineCommand,
    make_command_queue,
)
from job_agent.db import open_database

# ---- 基础装配 ----


@pytest.fixture()
def db(tmp_path: Any) -> Any:
    database = open_database(tmp_path / "console.db")
    yield database
    database.close()


def make_client(app: Any, **kwargs: Any) -> TestClient:
    """TestClient：base_url 即同源基准（Host=127.0.0.1:8000，scheme=http）。"""
    return TestClient(app, base_url="http://127.0.0.1:8000", **kwargs)


@pytest.fixture()
def app(db: Any) -> Any:
    return create_app(db=db)


@pytest.fixture()
def client(app: Any) -> TestClient:
    return make_client(app)


# ---- 第 1 项：仅监听 127.0.0.1 ----


class TestLocalhostOnlyBind:
    def test_config_default_host_is_loopback(self) -> None:
        assert ConsoleConfig().host == "127.0.0.1"

    def test_config_rejects_non_loopback_host(self) -> None:
        for host in ("0.0.0.0", "192.168.1.5", "localhost", "::1"):
            with pytest.raises(ValidationError):
                ConsoleConfig(host=host)

    def test_uvicorn_assembly_binds_loopback_only(self) -> None:
        server = serve_console(Config())  # 只构造不启动（不占端口）
        assert server.host == "127.0.0.1"
        assert server.port == Config().console.port


# ---- 第 2 项：Host 校验中间件 ----


class TestHostValidation:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "127.0.0.1:8000",
            "localhost",
            "localhost:8000",
            "LOCALHOST:8000",
            "127.0.0.1:65535",
        ],
    )
    def test_allowed_hosts_pass(self, client: TestClient, host: str) -> None:
        resp = client.get("/api/health", headers={"host": host})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "version": __version__}

    @pytest.mark.parametrize(
        "host",
        [
            "evil.example",
            "127.0.0.1.nip.io",
            "sub.localhost",
            "localhost.:8000",
            "[::1]:8000",
            "169.254.169.254",
            "127.0.0.1.evil.com:8000",
        ],
    )
    def test_foreign_hosts_rejected_403(self, client: TestClient, host: str) -> None:
        resp = client.get("/api/health", headers={"host": host})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden_host"

    def test_host_check_covers_static_and_api(self, client: TestClient) -> None:
        for path in ("/static/index.html", "/api/status"):
            resp = client.get(path, headers={"host": "evil.example"})
            assert resp.status_code == 403


# ---- 第 3 项：非 GET 要求 X-Job-Agent: console ----


class TestConsoleHeaderRequired:
    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_missing_header_rejected(self, client: TestClient, method: str) -> None:
        resp = client.request(method, "/api/health")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "console_header_required"

    @pytest.mark.parametrize("value", ["", "admin", "Console", "console ", "bearer"])
    def test_wrong_header_value_rejected(self, client: TestClient, value: str) -> None:
        # "console "（尾随空白）会被 strip 后放行——取值语义宽松，仅防跨站简单请求
        resp = client.post("/api/health", headers={"X-Job-Agent": value})
        if value.strip() == "console":
            assert resp.status_code == 405  # 头通过 → 路由层按方法不匹配 405
        else:
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "console_header_invalid"

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_valid_header_passes_middleware(self, client: TestClient, method: str) -> None:
        # S5a 无业务写路由：放行后由路由层返回统一 JSON 的 405（S5b/S5c 接管业务语义）
        resp = client.request(method, "/api/health", headers={"X-Job-Agent": "console"})
        assert resp.status_code == 405
        assert resp.json()["error"]["code"] == "method_not_allowed"

    def test_get_and_head_exempt(self, client: TestClient) -> None:
        assert client.get("/api/health").status_code == 200
        # FastAPI APIRoute 默认不自动映射 HEAD：405 来自路由层（非 403），
        # 证明中间件层不要求 HEAD 携带 X-Job-Agent；StaticFiles 原生支持 HEAD
        assert client.head("/api/health").status_code == 405
        assert client.head("/static/index.html").status_code == 200


# ---- 第 4 项：GET 无副作用 ----


def _dump_all_tables(db: Any) -> dict[str, list[tuple[Any, ...]]]:
    tables = [
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    return {t: [tuple(r) for r in db.query(f"SELECT * FROM {t}")] for t in tables}


class TestGetSideEffectFree:
    def test_repeated_get_identical_and_db_untouched(self, db: Any, app: Any) -> None:
        client = make_client(app)
        before = _dump_all_tables(db)
        r1 = client.get("/api/status")
        r2 = client.get("/api/status")
        r3 = client.get("/api/health")
        assert r1.status_code == r2.status_code == 200
        assert r1.json() == r2.json()
        assert r3.json()["status"] == "ok"
        assert _dump_all_tables(db) == before

    def test_readonly_view_allows_reads(self, db: Any) -> None:
        view = ReadOnlyDatabase(db)
        rows = view.query("SELECT count(*) AS n FROM run_state")
        assert rows[0]["n"] == 0
        assert view.query_one("SELECT 1 AS one") is not None

    def test_readonly_view_blocks_write_sql(self, db: Any) -> None:
        view = ReadOnlyDatabase(db)
        with pytest.raises(ReadAttemptError):
            view.query("INSERT INTO run_state (key, value_json) VALUES ('k', '1')")
        with pytest.raises(ReadAttemptError):
            view.query_one("UPDATE run_state SET value_json = 'x'")
        with pytest.raises(ReadAttemptError):
            view.query("DELETE FROM run_state")

    def test_readonly_view_blocks_write_attributes(self, db: Any) -> None:
        view = ReadOnlyDatabase(db)
        with pytest.raises(AttributeError, match="只读视图"):
            view.execute("DELETE FROM run_state")  # type: ignore[attr-defined]
        with pytest.raises(AttributeError, match="只读视图"):
            view.transaction()  # type: ignore[attr-defined]


# ---- 第 5 项：敏感字段打码（****尾4位）----


class TestSensitiveMasking:
    def test_mask_helper_nested_structures(self) -> None:
        masked = mask_sensitive_fields(
            {
                "ai": {"api_key": "sk-1234567890abcd", "model": "deepseek-chat"},
                "notify": {
                    "feishu": {
                        "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef12",
                        "secret": "abc12",
                    },
                    "email": {"password": "hunter2-secret", "user": "a@b.c"},
                },
                "count": 3,
                "note": None,
                "list": [{"api-key": "AKIA1234567890"}],
            }
        )
        assert masked["ai"]["api_key"] == "****abcd"
        assert masked["ai"]["model"] == "deepseek-chat"
        # webhook URL 自带长令牌，按"webhook secret"口径一并打码（与 S5b 同口径）
        assert masked["notify"]["feishu"]["webhook"] == "****ef12"
        assert masked["notify"]["feishu"]["secret"] == "****"  # 6 位 < 8 → 全打码
        assert masked["notify"]["email"]["password"] == "****cret"
        assert masked["notify"]["email"]["user"] == "a@b.c"
        assert masked["count"] == 3
        assert masked["note"] is None
        assert masked["list"][0]["api-key"] == "****7890"  # 连字符/驼峰键归一化命中

    def test_mask_helper_empty_means_unset(self) -> None:
        out = mask_sensitive_fields({"api_key": "", "secret": None})
        assert out["api_key"] == ""  # 空值=未配置，原样保留（打码会误导"已配置"）
        assert out["secret"] is None

    def test_mask_helper_idempotent_with_source_masking(self) -> None:
        # S5b/S5c 源头打码（****尾4位，短值也留尾4位）经本层不改写——精确幂等
        out = mask_sensitive_fields(
            {"api_key": "****abcd", "secret": "****ab", "webhook": "****ef12"}
        )
        assert out == {"api_key": "****abcd", "secret": "****ab", "webhook": "****ef12"}

    def test_response_middleware_masks_any_json(self, app: Any) -> None:
        @app.get("/api/test/secrets")
        def _secrets() -> dict[str, Any]:
            return {"ai": {"api_key": "sk-abcdefgh1234"}, "safe": "text"}

        client = make_client(app)
        resp = client.get("/api/test/secrets")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ai"]["api_key"] == "****1234"
        assert data["safe"] == "text"
        assert "sk-abcdefgh1234" not in resp.text

    def test_unhandled_error_leaks_nothing(self, app: Any) -> None:
        @app.get("/api/test/boom")
        def _boom() -> dict[str, str]:
            raise RuntimeError("内部细节 sk-1234567890abcd")

        client = make_client(app, raise_server_exceptions=False)
        resp = client.get("/api/test/boom")
        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "internal_error"
        assert "sk-1234567890abcd" not in resp.text


# ---- 第 6 项：不注册 CORSMiddleware + OPTIONS 拒绝 + Origin 校验 ----


class TestNoCorsAndOrigin:
    def test_no_cors_middleware_registered(self, app: Any) -> None:
        names = [middleware.cls.__name__ for middleware in app.user_middleware]
        assert "CORSMiddleware" not in names
        assert ConsoleHardeningMiddleware.__name__ in names

    def test_options_preflight_always_rejected(self, client: TestClient) -> None:
        resp = client.options("/api/health")
        assert resp.status_code == 405
        assert resp.json()["error"]["code"] == "method_not_allowed"

        resp2 = client.options(
            "/api/health",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert resp2.status_code == 405

    def test_absent_or_empty_origin_allowed(self, client: TestClient) -> None:
        assert client.get("/api/health").status_code == 200  # 不带 Origin
        assert client.get("/api/health", headers={"Origin": ""}).status_code == 200

    def test_same_origin_allowed(self, client: TestClient) -> None:
        resp = client.get("/api/health", headers={"Origin": "http://127.0.0.1:8000"})
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        "origin",
        [
            "http://evil.example",
            "https://127.0.0.1:8000",  # 协议不同
            "http://localhost:8000",  # localhost 对 127.0.0.1 属跨源（浏览器口径）
            "http://127.0.0.1",  # 端口不同（默认 80）
            "http://127.0.0.1:9000",
            "null",  # 沙箱 iframe
            "ftp://127.0.0.1:8000",
        ],
    )
    def test_cross_origin_rejected(self, client: TestClient, origin: str) -> None:
        resp = client.get("/api/health", headers={"Origin": origin})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "cross_origin"

    def test_origin_check_also_guards_post_with_header(self, client: TestClient) -> None:
        headers = {"X-Job-Agent": "console", "Origin": "http://evil.example"}
        resp = client.post("/api/health", headers=headers)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "cross_origin"


# ---- 异常统一 JSON ----


class TestUnifiedErrorJson:
    def test_404_json_envelope(self, client: TestClient) -> None:
        resp = client.get("/api/no-such-endpoint")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "not_found"
        assert body["error"]["message"]


# ---- 引擎指令队列注入（§5.5 I6）----


class TestCommandQueueInjection:
    def test_issue_command_enqueues_i6_message(self) -> None:
        q = make_command_queue()
        issued = issue_command(q, "pause")
        assert isinstance(issued, EngineCommand)
        got = q.get_nowait()
        assert got is issued  # 同一对象入队（queue.Queue 线程安全）
        assert got.cmd == "pause"
        assert got.req_id == issued.req_id
        assert got.issued_at.tzinfo is not None  # UTC tz-aware（与 utc_clock 口径一致）
        assert got.payload == {}

    def test_issue_command_with_payload(self) -> None:
        q = make_command_queue()
        issue_command(q, "approve_outbox", {"outbox_id": 7})
        got = q.get_nowait()
        assert got.cmd == "approve_outbox"
        assert got.payload == {"outbox_id": 7}

    def test_unknown_command_rejected(self) -> None:
        q = make_command_queue()
        with pytest.raises(ValueError, match="未知引擎指令"):
            issue_command(q, "self_destruct")
        assert q.empty()

    def test_status_exposes_queue_depth_readonly(self) -> None:
        q = make_command_queue()
        client = make_client(create_app(command_queue=q))
        assert client.get("/api/status").json()["command_queue_depth"] == 0
        issue_command(q, "login_mode", {"closed": True})
        assert client.get("/api/status").json()["command_queue_depth"] == 1
        assert q.qsize() == 1  # GET 不消费队列


# ---- /api/status 数据源（DB 快照，不触碰引擎对象）----


class TestStatusSnapshot:
    def test_status_without_engine_writes(self, client: TestClient) -> None:
        data = client.get("/api/status").json()
        assert data["engine"]["state"] == "unknown"
        assert data["last_command"] is None
        assert data["command_queue_depth"] is None

    def test_status_reads_run_state_written_by_engine(self, db: Any) -> None:
        runstate.set_state(
            db, KEY_ENGINE_STATE, {"state": "running", "dry_run": True, "risk_paused": False}
        )
        runstate.set_state(db, KEY_LAST_COMMAND, {"req_id": "r-1", "cmd": "start", "ok": True})
        client = make_client(create_app(db=db))
        data = client.get("/api/status").json()
        assert data["engine"]["state"] == "running"
        assert data["engine"]["dry_run"] is True
        assert data["last_command"]["cmd"] == "start"
        assert data["command_queue_depth"] is None


# ---- 静态目录挂载 ----


class TestStaticMount:
    def test_static_index_served(self, client: TestClient) -> None:
        resp = client.get("/static/index.html")
        assert resp.status_code == 200
        assert "job-agent" in resp.text

    def test_root_redirects_to_static(self, client: TestClient) -> None:
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/static/"
        followed = client.get("/")
        assert followed.status_code == 200
        assert "job-agent" in followed.text
