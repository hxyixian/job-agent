# 开发文档 - Stage 1：工程骨架

> 所属 Version：v1.0.0 ｜ 所属阶段：阶段三-开发 ｜ 前置依赖：无 ｜ 并行状态：本文件内三个 SubStage 可并行

- 上游文档：[设计文档.md](../设计文档.md)（本文引用其章节号，如 §4=config）；[需求文档.md](../需求文档.md)（R10）
- 自包含声明：本文为 SubAgent 任务书，规范文件不入库，本文不引用规范，全部契约以设计文档为准。

---

## SubStage: S1a 工程骨架与配置模块

- 所属 Stage：Stage 1
- 依赖前置：无依赖，可立即开工
- 并行状态：可与 S1b/S1c 并行
- 所属阶段：阶段三 - 开发

### 背景与目标
从零建立 Python 工程骨架与配置子系统，使 `uv run python -m job_agent` 可完成"加载配置→打印启动横幅→退出"的最小闭环。

### 当前代码状态
仓库无任何代码；目录规划见设计文档 §3。

### 任务内容
1. `pyproject.toml`：项目元数据（`job-agent`，包 `job_agent`，src 布局）；依赖：`patchright==1.64.4, fastapi, uvicorn, openai, pydantic, pydantic-settings, pyyaml, pypdf, jinja2(如需)`（**浏览器驱动 = Patchright，不依赖 playwright** [M4/K5]）；dev 依赖组：`pytest, pytest-cov, ruff, mypy, aiosmtpd, httpx`；`[tool.ruff]` 行长 100、常用规则集；`[tool.mypy]` 对 `src/job_agent` 启用严格子集（disallow_untyped_defs 等）；`[tool.pytest.ini_options]` `-W error` + 豁免白名单。
2. `.python-version`：执行 `uv python list` 后锁定 3.12.x 最新 patch 版 [E3]。
3. `.gitattributes`：`* text=auto eol=lf` + 二进制白名单（png/pdf/db 等）[D2]。
4. `src/job_agent/__init__.py`（版本号 1.0.0）、`__main__.py`（现阶段：加载配置→日志初始化→打印横幅退出，预留引擎/控制台装配位）。
5. `config.py`：按设计文档 §4 的 Pydantic 模型树（site/target/delivery/messaging/ai/match/notify/console/data 各节）；`load_config(path)`、`write_back(config, path)`（全量重写 YAML，无注释保留）；字段默认值与 §4/需求文档 §10 完全一致；**校验**：`auto_reply_limit>=1`、端口范围、`preferences` 字段存在但 v1 不使用 [N5]。
6. `config.example.yaml`：全字段 + 中文注释（含"手写注释会被控制台覆盖"提示）。

### 输入输出
- 输入：无；输出：可安装工程 + `uv run python -m job_agent` 横幅输出。

### 验收标准
1. `uv sync --locked` 成功；`uv run ruff check`/`mypy`/`compileall` 全过（A-BUILD-1/2）；
2. 单元测试：配置加载/默认值/校验失败路径/写回往返（load→write→load 相等）≥ 6 用例全绿；
3. `config.example.yaml` 被加载后与内置默认值合并无异常。

### 禁止事项
- 不实现任何业务逻辑（调度/数据库/AI 均不在本 SubStage）；
- 不改动 `Document/`、`private/`、`.gitignore`。

---

## SubStage: S1b 数据库与迁移

- 所属 Stage：Stage 1
- 依赖前置：无
- 并行状态：可与 S1a/S1c 并行
- 所属阶段：阶段三 - 开发

### 背景与目标
建立 SQLite 访问层与第一版 schema，支撑后续所有核心表。

### 任务内容
1. `db.py`：连接管理（WAL、foreign_keys=ON）、`run_migrations(db_path, migrations_dir)`（按序号应用、`schema_migrations` 记录、事务包裹、幂等）；线程策略：引擎线程单连接 + check_same_thread=False + 锁。
2. `migrations/0001_init.sql`：设计文档 §6 全部 15 表（companies/jobs/evaluations/applications/send_intents/conversations/messages/msg_routing/outbox/notification_events/notify_dedup/greeting_templates/resume_snapshots/job_directions/run_state），字段与索引以 §6 为准（含 G1 的 `skipped_dryrun`、G7 的 `user_label`）。
3. 单元测试：迁移幂等（跑两遍）、WAL 生效、外键约束生效、每表 insert/select 冒烟。

### 验收标准
迁移在空库/已有库两态执行成功；测试 ≥ 8 用例全绿。

### 禁止事项
- 表结构不得偏离设计文档 §6（字段名是跨 SubStage 契约）；不引入 ORM。

---

## SubStage: S1c 日志、单实例锁与工具

- 所属 Stage：Stage 1
- 依赖前置：无
- 并行状态：可与 S1a/S1b 并行
- 所属阶段：阶段三 - 开发

### 任务内容
1. `logging_setup.py`：`logs/app.log`(INFO, 10MB×7 轮转) + `logs/error.log`(ERROR+) + 控制台；**打码过滤器**：消息中的 api_key/webhook secret/smtp password 替换为 `****`；简历全文禁止落日志（长度阈值+关键词拦截告警）[S3]。
2. `single_instance.py`：`%LOCALAPPDATA%/job_agent/instance.lock` 独占句柄；重复启动抛 `InstanceExistsError`（退出码 2 + 友好提示）[B5]。
3. 单元测试：轮转配置、打码过滤器（各敏感形态）、锁互斥（子进程模拟第二实例）。

### 验收标准
测试全绿；A-BOOT-2 双实例行为在此层可测。

### 禁止事项
不引入第三方日志库（标准库 logging 足够）。
