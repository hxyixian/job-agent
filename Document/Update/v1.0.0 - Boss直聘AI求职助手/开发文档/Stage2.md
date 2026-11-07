# 开发文档 - Stage 2：核心域（纯逻辑，全部可离线测试）

> 所属 Version：v1.0.0 ｜ 所属阶段：阶段三-开发 ｜ 前置依赖：Stage 1 全部 ｜ 并行状态：四个 SubStage 间有少量契约依赖，按注记并行

- 上游：[设计文档.md](../设计文档.md) §5.1（models）、§6（表）、§7（流水线闸门语义）、§8.1（计数）、§13（状态机/放量）；本 Stage **不依赖浏览器与 AI 实现**（AI 通过接口注入夹具）。

---

## SubStage: S2a 领域模型与判定规则

- 所属 Stage：Stage 2 ｜ 依赖前置：S1a ｜ 并行：可立即开工 ｜ 所属阶段：阶段三 - 开发

### 任务内容
1. `models.py`：设计文档 §5.1 全部模型（JobRef/JobDetail/Conversation/MsgType/Message/SendOutcome/LoginState/RiskSignal）；`Message.is_ours_auto` 注释标明"核心层计算" [G6]。
2. `rules.py`（纯函数）：
   - `exclusion_match(job, groups, blacklist) -> bool`：按 §4 `exclude_groups`（id/kind/enabled/keywords/scope）结构执行，scope∈{company_name,jd_text,tags}；v1 仅 kind=preset 生效 [G9]；**匿名公司卡跳过**（`is_anonymous(job)`，"某知名企业"类模式 [v13]）；
   - `dedup_check(company_key, companies_row, mode, now) -> bool`；
   - ~~salary_overlap~~ **已移除 [R13：DOM 薪资字体加密，维度废止]**。
3. 单元测试：排除组命中与 scope、匿名卡识别、去重两模式与过期。

### 验收标准
全绿；判定函数 100% 纯函数（无 IO），供 S4c 组合。

### 禁止事项
不读数据库不做网络；不实现 SiteAdapter。

---

## SubStage: S2b 发送状态机与对账

- 依赖前置：S1b、S2a（SendOutcome）｜ 并行：可与 S2c/S2d 并行 ｜ 所属阶段：阶段三 - 开发

### 任务内容
1. `core/state_machine.py`：`SendStateMachine`——`begin(kind,target,digest)->intent_id`（写 pending）、`resolve(intent_id, outcome)`、`startup_recover()`（残留 pending→unknown，**绝不重发**）[B4]；
2. `reconcile(conv_messages, intents)` [F1 匹配规则]：**greeting 按关联岗位 job_id + ±10min 窗口（不做文本摘要比对——实际文本来自平台设置）；reply/resume 按 payload_digest + ±10min 窗口（digest 归一化：去空白/截断 64 字符 hash）**；匹配不上 → 该我方消息标记"人工"（供 B3 接管）；
3. `quota.py`：`used(window, kind)` 从 `send_intents` 推算——`state IN (pending,sent,unknown,skipped_dryrun)` **不分 dry_run** [G1]；小时窗口=滑动 60min，**日窗口=北京自然日（起点用 `timeutil.day_start_utc`，R6-4 契约，禁止用 UTC 日期）** [B18]；含 `browse_used()`（详情/翻页计数，同不分模式）[G3]。
4. 测试：T4-1 崩溃恢复、unknown 计入限额、同日切模式额度连续、digest 匹配三态。

### 验收标准
A-CORE-2/3 的逻辑层在此全绿；任何路径不得出现"自动重发"代码。

---

## SubStage: S2c 调度器（时段/频率/闸门）

- 依赖前置：S2b（quota）｜ 并行：与 S2d 并行 ｜ 所属阶段：阶段三 - 开发

### 任务内容
1. `core/scheduler.py`：`RunSchedule.is_active(now)`（weekday∈配置、start≤now<end；**工作日=周一~五**，不处理节假日 [s2]）；轮询时段可独立（默认继承投递）[P1]；
2. `Gate` 组合闸门：时段 ∧ 投递限额（I1）∧ 浏览上限（**I1.current_limits 综合放量档位/Chrome 降档折算** [H2/V9]）∧ **操作级最小间隔**（I1.min_interval_s：详情/搜索 ≥30s，独立于 8-20s 随机节流之外的下限 [H2]）∧ 熔断标志 ∧ 前置条件；
3. 时间注入接口（`clock: Callable[[], datetime]`）供测试传固定时间 [T4-7]；
4. 测试：周五 16:01 不投递、周六不投、轮询独立时段生效、滑动窗口边界、四级限额触发、**最小间隔独立生效（随机节流值 < 30s 时仍强制 30s）、浏览放量档位翻倍与回落、Chrome 降档联动** [H2]。

### 验收标准
A-CORE-3/9 逻辑层全绿；禁止在调度器内做任何 IO。

---

## SubStage: S2d 会话状态与放量状态机

- 依赖前置：S2a、S1b ｜ 并行：与 S2c 并行 ｜ 所属阶段：阶段三 - 开发

### 任务内容
1. `core/session.py`：会话计数（`auto_sent_count` 含招呼语不含简历、`resume_sent_count`≤1 [A2]）；`manual_takeover` 置位（按 §5.5.I2/I8 匹配规则，greeting 核验读回消息不触发 [F1]）；基线快照写入与过滤 [B3]；**每日 greeting 核验标记（is_greeting_probe_done/mark [F1/§I8]）**；
2. `core/rampup.py`：放量状态机——`remaining_days=3`、**有效运行日**判定（当日发生投递动作；被限额拦住的当日不计 [G10/Q20]）、期间标志（低风险回复关、FR3.7/3.8 关）、期满自动恢复+低级通知事件（EventType.RAMPUP_DONE）、手动提前/延长接口；**试运行浏览放量档位**（首日 15/时·60/天，无风控日翻倍，风控信号回首档 [H2]）；**放量/试运行期间任何风控信号 → 暂停全部任务 + EventType 高级告警 + 等用户决策（M5：无 fallback_semi_auto、无自动恢复）**；
3. `dryrun.py`：模式标志、拟发送记录写入（skipped_dryrun）、隔离查询助手（去重/统计/会话计数过滤 dry_run=1 [N2]）；
4. 测试：计数口径（A-CORE-6 逻辑层）、放量日历（含被限额日不计）、dry_run 隔离（N2 重点用例）。

### 验收标准
全部纯逻辑可离线测试；不 import adapters/ai。
