# 开发文档 - Stage 6：ZhipinAdapter（真实站点适配）

> 所属 Version：v1.0.0 ｜ 所属阶段：阶段三-开发 ｜ 前置依赖：**S4 全部 + S1b 定型（第二轮 X4 通过）+ B0 素材齐备（来源：第二轮 X4 只读采集脱敏存 `private/survey/manual/` + 用户手动观察 A1/A5，见开工闸门）+ 设计文档 §5.4 假设表 A1-A6 已核对** ｜ 并行状态：本 Stage 内三 SubStage 可并行

- ⚠️ **开工闸门**（缺一不开工，W4 修订）：
  ① 第二轮 X4 通过、S1b 定型；
  ② B0 素材 = 第二轮 X4 只读采集的搜索页/详情页/会话页 HTML（脱敏后存 `private/survey/manual/`，含 V3 内容检查记录）；
  ③ **A1（点击"立即沟通"后平台是否自动发送预设招呼语）与 A5（附件简历发送入口）由用户在普通浏览器手动投递一次观察并截图存档**——只读实验无法回答，必须人工；
  ④ §5.4 假设逐项核对并回填设计文档。

---

## SubStage: S6a 登录/风控检测与搜索

- 所属 Stage：Stage 6 ｜ 依赖前置：开工闸门 ｜ 并行状态：与 S6b/S6c 并行 ｜ 所属阶段：阶段三-开发

### 任务内容
1. `zhipin/selectors.yaml`：集中选择器配置（语义优先、禁 hash class、fallback 链 [选择器策略]）；基于 B0 素材编写，每选择器 ≥2 级 fallback。
2. `check_login_state()`：URL/元素复合判定（登录页标志 vs 用户标志）。
3. `detect_risk()`：验证码页/风控页特征；**EVICTED**（about:blank/非站点域）与 **SECURITY_CHECK**（URL 含 `_security_check`/验证类路径、页面出现"安全验证/环境异常/验证码"文字、导航落点非目标页连续 2 次）[G2/H1]；适配器失效计数（关键元素连续 3 次失败 [B7]）；**内容级健康检查**（搜索页零卡片/详情页无 JD 正文计入 B7 计数 [H1]）。
4. `search_jobs(filters, page)`：URL 参数构造（query/city/经验/规模；**薪资参数已移除 [R13/F7]**——薪资维度 v1 不做）；+ 列表解析（JobRef 全字段，**薪资仅保留 salary_text 原文展示字段 [F3]**）；翻页计入 search_page_hourly [G3]；**company_key 取值来源 [F4]：公司/品牌链接中的平台 ID → 后备公司名元素（禁止用 HR 姓名/boss-name），离线测试：同公司不同 HR 两卡 → 同一 company_key**。
5. 测试：**离线**——B0 脱敏 HTML 快照作夹具（解析器纯函数化：html→JobRef 列表）[T5]。

## SubStage: S6b 详情与发送

- 所属 Stage：Stage 6 ｜ 依赖前置：开工闸门 ｜ 并行状态：与 S6a/S6c 并行 ｜ 所属阶段：阶段三-开发

### 任务内容
1. `get_job_detail()`：详情页解析（jd_text/experience/scale/福利标签）+ 浏览计数；离线快照测试。
2. `send_greeting()`（**R12 机制 + 图像佐证细化**）：= 定位岗位卡/详情卡"立即沟通"按钮并**点击**——平台自动发送已同步模板；**成功确认信号 = "已向BOSS发送消息"弹窗出现**（按钮文案点击后不变，不得以按钮为判据）；前置校验：控制台已确认模板同步（FR5.3，平台设置路径【消息通知-设置招呼语】）。
3. `send_resume()`：附件简历入口（A5 已核实：会话页可达、可见送达情况）；**前置校验：HR 已回复的会话才可发送**（R12 实测约束），不满足时 FAILED + 明确原因、不重试；多份简历选择=配置名 [B8]。
4. **全程不触真实站点**：开发与测试只用快照 + Mock；真实验证 = TEST3 试运行。

## SubStage: S6c 会话与基线

- 所属 Stage：Stage 6 ｜ 依赖前置：开工闸门 ｜ 并行状态：与 S6a/S6b 并行 ｜ 所属阶段：阶段三-开发

### 任务内容
1. `list_conversations()`：会话列表解析（含关联岗位标识，A3 假设）。
2. `get_messages(since)`：消息解析（MsgType 分类：文本/图片/语音/文件/职位卡/索要简历卡[ A2]/联系方式卡/面试卡/系统消息[A4]）。
3. `get_conversation_job()`；`snapshot_conversation_baseline()`（B3）。
4. 离线快照测试；A2-A4 假设任一被素材推翻 → 停止并上报（按 §5.4 调整列）。

### 禁止事项（全 Stage）
- 不得在开发期访问真实站点（快照/mock only）；
- 选择器硬编码于 Python 源码（必须 selectors.yaml）；
- 任何登录操作代码。
