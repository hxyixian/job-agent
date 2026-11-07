# DSH 子代理跨 Provider 路由授权指南(deepseek-official / deepseek-flash 实战)

> 适用版本:DeepSeek Harness 桌面版(本次验证于 2026-11-06,Windows,DSH desktop profile)。
> 场景:想在会话里用非父会话路由(如 `deepseek-official/deepseek-flash`)拉起 subagent,却报错
> `child LLM route "deepseek-official/deepseek-flash" is not allowed for this Session`。

---

## 1. 问题现象

在会话中调用 `subagent` 工具并显式指定 `provider: deepseek-official`、`model: deepseek-flash` 时,直接失败:

```
Error: child LLM route "deepseek-official/deepseek-flash" is not allowed for this Session;
without a recorded model-selection policy, explicit selection (including reasoning_effort)
is limited to the parent route
```

同时 `list_subagent_models` 只显示父会话当前路由(如 `zai-coding-cn`),看不到 `deepseek-official`。

**注意:这不代表 deepseek-official 没配置。** base 层始终挂载 `llm-deepseek` 适配器,只要凭据库里有 `DEEPSEEK_API_KEY`,provider 就是活的——它只是被会话级白名单(p policy)过滤掉了。

## 2. 根因(源码分析)

相关代码位于安装目录的 `app.asar` 内(解包方法见附录 B):

```
dsh/node_modules/@deepseek-ai/dsh-tool-subagent/lib/index.js                  # 路由检查
dsh/node_modules/@deepseek-ai/dsh-tool-subagent/lib/model-selection-settings.js # policy 写入时机
```

### 2.1 路由检查逻辑(`index.js:89-96`)

```js
function assertAllowedModelSelection(policy, parentOptions, requested, request) {
    if (!hasDelegationModelRequest(request)) return;   // 只有显式传 provider/model/effort 才检查
    const provider = requested?.provider ?? parentOptions.provider;
    const model = requested?.model ?? parentOptions.model;
    // policy 为空 → 只允许父会话自己的路由;有 policy → 必须命中 policy.routes
    if (policy === void 0
        ? provider === parentOptions.provider && model === parentOptions.model
        : policy.routes.some((route) => route.provider === provider && route.model === model)) return;
    throw new Error(`child LLM route "${provider}/${model}" is not allowed for this Session` + ...);
}
```

要点:

- **只有显式选择才受限**。调用 `subagent` 时不传 `provider/model`,或工具实例用 `agentOptions` 配置了默认路由,则完全绕过此检查(但那需要改 loader 配置,不在本方案内)。
- `list_subagent_models` 的展示同样按 policy 过滤:无 policy 时只展示父路由,所以"看不到 provider"≠"provider 不存在"。

### 2.2 policy 的写入时机(`model-selection-settings.js:83-101`)——关键坑

policy 不是每次调用时实时读设置,而是一个**会话日志事件**(`subagent/model-selection-policy`)驱动的 projection:

```js
ctx.on("agent/created", async ({ agent }) => {
    const target = agent.session;
    if (subagentModelSelectionPolicy(policy, target) !== void 0) return; // 已有则跳过
    // ... 从宿主设置读取 enabled/allowedModels ...
    if (allowedModels !== void 0) target.append("subagent/model-selection-policy", { allowedModels });
});
```

并且仅当 `target.firstLiveSeq === 0 && eventAt(0)?.type !== "session/end-seed"`(全新顶层会话)才写入。

**推论(踩坑清单):**

| 场景 | 能否生效 |
|---|---|
| 改设置后,当前已存在的会话 | ❌ 永远不生效(policy 事件只在创建时追加,无任何 API 可补录) |
| 改设置后新建的会话(GUI 新标签页) | ✅ 创建即写入 policy |
| `subagent_fork` 的子会话 | ❌ seq 0 是 end-seed,只继承父会话的 policy(父没有则没有) |
| `subagent` 工具拉起的子代理会话 | ❌ 同上,只继承父会话 policy |

### 2.3 设置本身是实时的

`SubagentModelSelectionConfig` 的 `enabled`/`allowedModels` 是 volatile 字段,写入后 `applies: "live"` 立即对**之后创建的会话**生效,无需重启宿主。设置会持久化到当前 profile 的 `cordis.patch.yml`。

## 3. 解决方案

### 方式 A:GUI 操作(推荐,最简单)

1. GUI → **设置 → Plugins/Subagent** 页面(`dsh-client-ui-settings-subagent` 提供的卡片)。
2. 开启 subagent 模型选择开关,勾选 `deepseek-official / deepseek-flash`(以及你想保留的其他路由)。
3. 保存(等效于调用 `settings/mutate`,见方式 B)。
4. **新建一个会话**(新标签页)。新会话里 `list_subagent_models` 会列出 `deepseek-official`,即可:

```
subagent(provider: "deepseek-official", model: "deepseek-flash", prompt: "...", description: "...")
```

### 方式 B:直接调网关 RPC(脚本/自动化场景)

当无法点 UI(例如让 agent 自己修配置)时,可以复刻浏览器设置页的写法。工作区里留有现成脚本 `dsh-settings-mutate.js`,要点如下:

**协议**:`POST http://127.0.0.1:<port>/api/settings/mutate`

```json
{
  "type": "client-request",
  "rpcId": "<uuid>",
  "method": "settings/mutate",
  "payload": {
    "args": {
      "ns": "subagent-model-selection-settings",
      "ops": [
        { "op": "set", "path": ["enabled"], "value": true },
        { "op": "set", "path": ["allowedModels"], "value": [
          { "provider": "deepseek-official", "model": "deepseek-flash" },
          { "provider": "zai-coding-cn",    "model": "glm-5.3" }
        ] }
      ]
    }
  }
}
```

**鉴权**:网关校验签名 Cookie。Cookie 用 `~/.dsh/.credentials.yaml` 中 `client-connection/browser-session` 的 secret 自签:

- Cookie 名:`dsh-auth-` + base64url(sha256(`<host:port>`)),如 authority 为 `127.0.0.1:51461`(HTTP 用 host,不带协议)。
- Cookie 值:`v1.<base64url(json 载荷)>.<base64url(HMAC-SHA256(secret, body))>`
- 载荷:`{"version":1,"authority":"127.0.0.1:51461","issuedAt":<ms>,"expiresAt":<ms>}`(有效期默认 30 天内)。
- secret 从 credentials 文件的 `client-connection/browser-session.payload.secret`(base64url,32 字节)解出。

**踩坑**:
- `payload` 必须是 `{ args: {...} }` 单字段包裹,直接平铺参数会报 `Remote payload must contain exactly one plain-object args field`。
- 返回 `result.value.applies` 应为 `"live"`;同时配置会持久化到**当前活动 profile** 的 `cordis.patch.yml`(注意:实际生效的 profile 可能是 `desktop` 而不是 `web`,以写入后文件变更为准)。

### 方式 C:手动编辑 profile patch(离线,需重启)

直接在 `~/.dsh/profiles/<active-profile>/cordis.patch.yml` 追加:

```yaml
- id: subagent-model-selection-settings
  name: "@deepseek-ai/dsh-tool-subagent/model-selection-settings"
  config:
    enabled: true
    allowedModels:
      - provider: deepseek-official
        model: deepseek-flash
```

文件层配置**不会**热加载,需要重启 DSH 宿主后才对新建会话生效。GUI/RPC 写入则会同时热生效+落盘,所以优先方式 A/B。

## 4. 前置条件核查(每次排障先做)

1. **API Key**:`~/.dsh/.credentials.yaml` 的 `refs.DEEPSEEK_API_KEY` 存在且有效(缺失时 provider 请求会报 `MISSING_CREDENTIAL`,提示走 web Models 页写入或环境变量 `DEEPSEEK_API_KEY`)。
2. **Provider 注册**:base 层恒挂载 `llm-deepseek`(注册 `deepseek-official`,默认目录含 `deepseek-flash` 与 `deepseek-v4-pro`,上下文 1,048,576 token)。`deepseek-flash` 支持文本+图像。
3. **默认模型**:base 层 `agent-default-model` 默认就是 `deepseek-official/deepseek-flash`;本机被 profile patch 覆盖成了 `zai-coding-cn/glm-5.3`,因此父路由不是 deepseek,才触发了白名单限制。

## 5. 验证清单

新会话中依次执行:

```
list_subagent_models()                                  # 无参:应列出 deepseek-official
list_subagent_models(provider: "deepseek-official")     # 应列出 deepseek-flash(及 v4-pro)
subagent(provider: "deepseek-official", model: "deepseek-flash",
         description: "连通性测试", prompt: "...")       # 应返回 started subagent <id>
```

失败排查:

| 症状 | 原因 |
|---|---|
| 仍是 "not allowed for this Session"(不带分号后缀) | 会话创建早于设置写入 → 换新会话 |
| `LLM provider ... is not registered` | adapter 未挂载/被禁用,查 profile 组合 |
| `MISSING_CREDENTIAL` | `DEEPSEEK_API_KEY` 未配置或无效 |
| `settings/mutate` 报 not volatile | 写入路径不在 volatile 字段内,检查 ops 的 path |

## 6. 已在本机完成的配置(现状)

- 白名单已开启并持久化(desktop profile `cordis.patch.yml`):
  `deepseek-official/deepseek-flash`、`zai-coding-cn/glm-5.3`、`zai-coding-cn/glm-5.3-flash`。
- 之后**任何新建会话**都可直接拉起 deepseek-flash 子代理,无需再做任何操作。
- 旧的存量会话(包括本次调试的会话)永远无法补授权,直接换新会话即可。

---

## 附录 A:相关文件速查

| 路径 | 说明 |
|---|---|
| `~/.dsh/.credentials.yaml` | 凭据库:`DEEPSEEK_API_KEY`、browser-session 签名 secret |
| `~/.dsh/profiles/desktop/cordis.patch.yml` | 活动配置补丁层(设置写入落盘处) |
| `~/.dsh/sessions/<workspace>/session-*.jsonl.zstd` | 会话日志(policy 事件持久化在此,创建时写入) |
| `<安装目录>/resources/app.asar` | DSH 宿主代码(asar 包,内含 `dsh/node_modules/@deepseek-ai/*`) |
| 工作区 `dsh-settings-mutate.js` | 方式 B 的可直接运行脚本(Node ≥ 18,用 ELECTRON_RUN_AS_NODE 跑自带 Electron 即可) |
| 工作区 `extract-asar.js` | 附录 B 的 asar 解包脚本 |

## 附录 B:如何解包 app.asar 查源码

```powershell
# 1. 复制出 asar(Electron 的 fs 包装会拦截 .asar 路径,必须先改名复制)
Copy-Item "<安装目录>\resources\app.asar" "$env:TEMP\app-copy.bin"

# 2. 用自带 Electron 以 Node 模式运行解包脚本(见工作区 extract-asar.js)
$env:ELECTRON_RUN_AS_NODE=1
& "<安装目录>\DeepSeek Harness.exe" extract-asar.js "$env:TEMP\app-copy.bin" "$env:TEMP\dsh-asar" "<安装目录>\resources\app.asar.unpacked"
```

asar 头部格式(自实现解析时):前 8 字节为两个 u32;`[4]`=header pickle 长度,pickle 自偏移 8 起;JSON 头位于 pickle 中 `{"files"` 处,其长度在起始位置前 4 字节;文件数据基准偏移 = `8 + pickleSize`,各文件 offset 相对基准。

之后即可全文检索,例如定位路由检查:

```
rg "is not allowed for this Session" "$env:TEMP\dsh-asar\dsh\node_modules\@deepseek-ai\dsh-tool-subagent\lib"
```
