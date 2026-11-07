# 开发文档 - Stage 4：站点适配与引擎

> 所属 Version：v1.0.0 ｜ 所属阶段：阶段三-开发 ｜ 前置依赖：Stage 2 + Stage 3 ｜ 并行状态：S4a/c/d/e 可并行；**S4b 已解锁（M4 确认 S1b；正式开工以第二轮 X4 通过为准）**

---

## SubStage: S4a SiteAdapter 基类与 MockSiteAdapter

- 依赖前置：S2a ｜ 并行：可立即开工（**本 Stage 的关键路径，优先做**）

### 任务内容
1. `adapters/base.py`：设计文档 §5.2 全部——`SiteAdapter` 抽象（11 方法，**全同步**）、`LoginState/RiskSignal`（含 EVICTED [G2]）、异常树（LoginRequiredError/RiskDetectedError/AdapterBrokenError(连续 N 次定位失败, 默认3)/PlatformLimitError/TransientError/AdapterError）+ 行为契约注释（每异常调用方处理方式表）；注册表 `register_adapter(site_id, cls)` / `create_adapter(site_id, ...)`.
2. `adapters/mock/adapter.py`：全方法实现，数据源 `tests/fixtures/sim/*.json`（职位池、会话池、消息脚本）；能力开关：dry_run 语义、驱逐注入（URL→about:blank 模拟 EVICTED）、**安全拦截注入（URL 带 _security_check / 导航弹回首页模拟 SECURITY_CHECK / 预期内容缺失模拟内容级健康检查失败）[H1]**、风控页注入、限额注入、崩溃点注入（"已发送未落库" [A-CORE-2]）、非我方消息注入（接管检测）。
3. 测试：契约级测试（每方法的正常/异常路径）、注册切换。

### 验收标准
Mock 支撑 A-CORE-1~9 全部场景注入；与 ZhipinAdapter 同注册机制。

### 禁止事项
Mock 不做任何网络/浏览器操作。

---

## SubStage: S4b 浏览器会话层（S1b / Patchright，M4 确认）

- 所属 Stage：Stage 4 ｜ 依赖前置：第二轮 X4 通过（定型）+ 设计 §5.3/§5.5 ｜ 并行：其余 S4 SubStage 不依赖本项

### 任务内容（契约见设计文档 §5.3 [K2/H1/V9 版] 与 §5.5）
1. `browser/strategy_base.py`：`BrowserSession` 接口（goto/wait_for_selector/click/query_text/query_attrs/scroll/screenshot/get_url + profile 生命周期 + 双标签页模型：会话页常驻 [Q22]）。
2. S1b 实现（browser/patchright_session.py）：`patchright==1.64.4`，官方 Best Practice 参数（channel=chrome、headless=False、no_viewport=True、**默认启动参数**、无注入、无自定义 headers/UA）[V4]；启动记录 Chrome 版本 + 主版本变化联动降档 [V9]。
3. EVICTED / SECURITY_CHECK 检测 [G2/H1]：URL 轮询（2s 粒度）+ 安全验证文字探测 + 导航落点校验（连续 2 次非目标页）；浏览器重建限 1 次/小时，拦截前置不重建。
4. 登录模式（M3/G5）：`launch_login_window()` 零自动化参数启动，窗口存活期间引擎互斥。
5. 测试：mock page 对象驱动；真实浏览器验证 = 第二轮 X4 + TEST3。

### 禁止事项
实验未定型前不写任何真实站点交互代码。

---

## SubStage: S4c 投递流水线

- 依赖前置：S4a（adapter 接口）、S2b/S2c ｜ 并行：接口冻结后即可开发（用 Mock）

### 任务内容
`core/delivery.py`：设计文档 §7 十步流水线；要点：列表过滤（S2a rules，**匿名卡跳过 [v13]**）→ 详情（浏览计数+1 [G3]）→ 评估缓存（jobs/evaluations 表，命中跳过 AI）→ 判定（verdict≠skip ∧ score≥min_score，矛盾从保守）→ dry_run 分支（begin(dry_run=True)→skipped_dryrun + applications(dry_run=1)）→ 发送（state_machine.begin→adapter.send_greeting→resolve）→ 去重登记 → 8-20s 节流；**每日首次 send_greeting 成功后的招呼语核验 [F1]：读回该会话我方首条消息（adapter.get_messages）与当前模板归一化比较（去空白/标点）——不一致→暂停投递+EventType.GREETING_MISMATCH（中级），该消息不参与接管判定**；被动招呼计入投递限额 [D0]；PlatformLimitError → 当日停投 [B6]；浏览上限触发 → EventType.BROWSE_LIMIT_REACHED（与 PLATFORM_LIMIT 区分 [F10]）。

### 验收标准
A-CORE-1（投递半链路）/A-CORE-3/8 逻辑全绿。

---

## SubStage: S4d 消息路由与轮询引擎

- 依赖前置：S4a、S3b、S2d ｜ 并行：可并行

### 任务内容
`core/messenger.py`：轮询循环（时段闸门 [P1]）→ list_conversations → get_messages(since) → **六级路由表**（§8：SYSTEM→状态推断[§10.4 规则]；CONTACT_REQUEST_CARD→待发箱+中级**永不自动**；RESUME_REQUEST→FR3.8 开关；INTERVIEW→待发箱+中级；多模态→待发箱+中级；文本→AI 分类四分流）；`no_reply` 不发不计数不告警 [B2]；分类失败→key；计数与转人工（S2d.session）；**手动接管检测**（digest 匹配 [B3/G6]）；待发箱规则（人工确认不计自动数、stale 标记、超时合并单条高级 [B10/s6]、时段外到期并入下轮）。

### 验收标准
A-CORE-6/7/10（待发箱部分）全绿。

---

## SubStage: S4e 简历解析装配

- 依赖前置：S3c ｜ 并行：可并行

### 任务内容
`resume/` 装配：上传 API 后端逻辑（文件校验/pypdf/AI 结构化/版本入库/手动修正读写）；职位方向生成编排（directions 表 + 确认流）。测试全离线。

---

## 编排器（并入 S4d 交付）

`core/orchestrator.py`：单执行者优先级队列（检测>回复>投递>浏览 [B5]）；熔断总开关（任一 fatal 信号 → 全停+对应告警+控制台状态）；主循环 tick 5s；防休眠 `SetThreadExecutionState`（时段内）[G11/Q21]；时钟跳变检测 [B18]；dry_run 全局开关与放量状态联动 [FR3.9]。
