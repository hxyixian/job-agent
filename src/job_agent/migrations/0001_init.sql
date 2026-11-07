-- 0001_init.sql — job-agent v1.0.0 初始 schema
-- 依据：设计文档 §6（字段名为跨 SubStage 契约，不得偏离）。
--
-- 约定（§6 未规定的实现自决，全部 SubStage 需遵守）：
--   * 时间戳：TEXT，ISO-8601 UTC（YYYY-MM-DDTHH:MM:SS.sssZ，字典序==时间序）；
--     有默认值者由 SQLite 生成，业务代码亦可用 job_agent.db.utcnow() 生成。
--   * 布尔：INTEGER 0/1，附 CHECK (col IN (0, 1))。
--   * 枚举：TEXT + CHECK，取值仅来自设计文档（§5.1 / §6 / §8 / §11），
--     未枚举的自由文本列（如 applications.status / evaluations.skipped_by /
--     messages.text 等）不加 CHECK，避免限制后续 SubStage。
--   * JSON 列：TEXT（调用方负责 json.dumps/loads）。
--
-- 外键说明：仅对“强归属且与 dry_run 无关”的关系设外键
--   （messages→conversations、msg_routing→messages、outbox→conversations）。
--   刻意不设 applications.company_key→companies、applications.job_id→jobs、
--   evaluations.job_id→jobs：dry_run 模式下 companies 不写入 [N2]、jobs 缓存
--   写入时机未在 §7 固定，外键会造成跨模式插入顺序耦合。
--
-- outbox.state 的 CHECK 含 'stale'：§8.3 要求草稿因新消息标记 stale（§6 枚举
-- 未列出，此处按 §8.3 补齐，二者取并集）。

-- 1. companies — 去重名单 [FR3.6]；试运行不写入 [N2]
CREATE TABLE companies (
    company_key   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    mode          TEXT NOT NULL CHECK (mode IN ('permanent', 'cooldown')),
    blocked_until TEXT,
    source        TEXT
);

-- 2. jobs — 岗位缓存（含评估缓存键）
CREATE TABLE jobs (
    job_id        TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    company_key   TEXT NOT NULL,
    detail_json   TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 3. evaluations — 评估结果与跳过留痕 [FR4.3, B9]；dry_run 与正式共用缓存
CREATE TABLE evaluations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL,
    score      INTEGER,
    verdict    TEXT CHECK (verdict IN ('recommend', 'neutral', 'skip')),
    reason     TEXT,
    skipped_by TEXT,
    user_label TEXT CHECK (user_label IN ('want', 'not_want')),  -- [G7] L1 标注，NULL=未标注
    dry_run    INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX idx_evaluations_job_id ON evaluations (job_id);
CREATE INDEX idx_evaluations_created_at ON evaluations (created_at);

-- 4. applications — 投递记录；dry_run 行独立存放 [N2]
CREATE TABLE applications (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           TEXT NOT NULL,
    company_key      TEXT NOT NULL,
    greeting_version INTEGER,
    eval_id          INTEGER,
    status           TEXT,
    dry_run          INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1)),
    sent_at          TEXT
);
CREATE INDEX idx_applications_job_id ON applications (job_id);
CREATE INDEX idx_applications_company_key ON applications (company_key);
CREATE INDEX idx_applications_dry_run_sent_at ON applications (dry_run, sent_at);
-- [R6-3] 库层兜底"绝不重投"（FR8.5）：同一岗位只允许一条正式投递记录（试运行不限）
CREATE UNIQUE INDEX uq_applications_job_real ON applications (job_id) WHERE dry_run = 0;

-- 5. send_intents — 发送状态机 [B4]；试运行拟发送=skipped_dryrun [G1]
CREATE TABLE send_intents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL CHECK (kind IN ('greeting', 'reply', 'resume')),
    target_ref     TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    state          TEXT NOT NULL CHECK (state IN ('pending', 'sent', 'failed', 'unknown', 'skipped_dryrun')),
    dry_run        INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1)),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at    TEXT,
    -- [R6-3] 试运行语义一致性：skipped_dryrun 状态与 dry_run=1 严格绑定
    CHECK ((state = 'skipped_dryrun') = (dry_run = 1))
);
CREATE INDEX idx_send_intents_created_at ON send_intents (created_at);
CREATE INDEX idx_send_intents_payload_digest ON send_intents (payload_digest);

-- 6. conversations — 会话状态与计数 [FR6.3, A2, B3]
CREATE TABLE conversations (
    conv_id           TEXT PRIMARY KEY,
    peer_name         TEXT NOT NULL,
    job_id            TEXT,
    is_baseline       INTEGER NOT NULL DEFAULT 0 CHECK (is_baseline IN (0, 1)),
    manual_takeover   INTEGER NOT NULL DEFAULT 0 CHECK (manual_takeover IN (0, 1)),
    auto_sent_count   INTEGER NOT NULL DEFAULT 0,
    resume_sent_count INTEGER NOT NULL DEFAULT 0,
    blocked_reason    TEXT,
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX idx_conversations_updated_at ON conversations (updated_at);

-- 7. messages — 消息记录
CREATE TABLE messages (
    platform_msg_id TEXT PRIMARY KEY,
    conv_id         TEXT NOT NULL REFERENCES conversations (conv_id) ON DELETE CASCADE,
    direction       TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    sender          TEXT NOT NULL CHECK (sender IN ('hr', 'me', 'system')),
    msg_type        TEXT NOT NULL CHECK (msg_type IN (
                        'TEXT', 'IMAGE', 'VOICE', 'FILE', 'JOB_CARD',
                        'RESUME_REQUEST_CARD', 'CONTACT_REQUEST_CARD', 'INTERVIEW_CARD', 'SYSTEM')),
    text            TEXT NOT NULL DEFAULT '',
    sent_at         TEXT NOT NULL,
    is_ours_auto    INTEGER CHECK (is_ours_auto IN (0, 1))  -- NULL=匹配不上，按人工消息处理 [G6]
);
CREATE INDEX idx_messages_conv_id_sent_at ON messages (conv_id, sent_at);

-- 8. msg_routing — 路由决策留痕（category 取值 = §8 路由优先级表枚举）
CREATE TABLE msg_routing (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_msg_id TEXT NOT NULL REFERENCES messages (platform_msg_id) ON DELETE CASCADE,
    category        TEXT NOT NULL CHECK (category IN (
                        'system', 'contact_request', 'resume_request', 'interview_invite',
                        'multimodal', 'low_risk', 'key', 'positive_response', 'no_reply')),
    action          TEXT NOT NULL,
    handled_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX idx_msg_routing_platform_msg_id ON msg_routing (platform_msg_id);

-- 9. outbox — 待发箱 [FR6, B10]
CREATE TABLE outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id     TEXT NOT NULL REFERENCES conversations (conv_id) ON DELETE CASCADE,
    draft_text  TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('ai', 'manual')),
    state       TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'skipped', 'expired', 'stale')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    reminded_at TEXT
);
CREATE INDEX idx_outbox_conv_id ON outbox (conv_id);
CREATE INDEX idx_outbox_state_created_at ON outbox (state, created_at);

-- 10. notification_events — 通知中心全量落库 [FR7.1]（level 枚举 = §11）
CREATE TABLE notification_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type     TEXT NOT NULL,
    level          TEXT NOT NULL CHECK (level IN ('high', 'medium', 'low')),
    payload_json   TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    delivered_json TEXT
);
CREATE INDEX idx_notification_events_created_at ON notification_events (created_at);

-- 11. notify_dedup — 免打扰期延后合并计数 [B11]
CREATE TABLE notify_dedup (
    event_type   TEXT PRIMARY KEY,
    window_start TEXT NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0
);

-- 12. greeting_templates — 双模板版本化 [FR5, B14]
CREATE TABLE greeting_templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL CHECK (kind IN ('active', 'passive')),
    content    TEXT NOT NULL,
    version    INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    is_current INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
    -- [R6-3/F1] "已同步到平台"确认存储：hash 与当前 content 的哈希一致 = 已同步
    platform_synced_hash TEXT,
    platform_synced_at   TEXT,
    UNIQUE (kind, version)
);
CREATE INDEX idx_greeting_templates_kind_is_current ON greeting_templates (kind, is_current);
-- [R6-3] 每种模板只能有一个"当前版本"
CREATE UNIQUE INDEX uq_greeting_current ON greeting_templates (kind) WHERE is_current = 1;

-- 13. resume_snapshots — 结构化简历版本 [FR2.5, B14]
CREATE TABLE resume_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    structured_json  TEXT NOT NULL,
    source_pdf_hash  TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    is_current       INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1))
);
-- [R6-3] 结构化简历只能有一个"当前版本"
CREATE UNIQUE INDEX uq_resume_current ON resume_snapshots (is_current) WHERE is_current = 1;

-- 14. job_directions — 职位方向 [FR2.2]
CREATE TABLE job_directions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    keywords_json TEXT NOT NULL,
    rationale     TEXT,
    is_confirmed  INTEGER NOT NULL DEFAULT 0 CHECK (is_confirmed IN (0, 1))
);

-- 15. run_state — 断点续跑 [FR8.5, B4]
CREATE TABLE run_state (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
