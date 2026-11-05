# Codex 融合评估

> 读取对象：`openai/codex` 源码（2026-10-21 快照，`/tmp/codex-main`，128 个 Rust crate）。
> 本机未安装 `codex` 可执行文件；本文是架构评估与融合方案，不是已完成集成。

## 结论

Codex 是**开发者本地的编码代理**：沙箱 + 审批 + 执行策略 + 会话回放，优化的是
“在仓库里把事做完”。FCode 是**金融级可审计运行时**：模型只提议，Host 门控、
预算、执行、复算，哈希链留证，优化的是“这个结论敢不敢签字”。

两者最像的是工具/审批/会话协议；最不该混的是**失败语义**。Codex 在沙箱拒绝后
会升级权限重试，FCode 的 `unknown` 永不自动重试。融合时必须保住 FCode 的
四条不变量：执行权在 Host、状态进哈希链、成功由独立复算产生、未知不重试。

推荐两条路线：

1. **把 Codex 当受控执行器**（快）：FCode 把 `codex exec --json` 包成一个工具，
   由 Host 授权、限时、限目录、独立校验 diff；Codex 的模型不参与 FCode 决策。
2. **只借架构模式**（稳）：execpolicy 预门控、MCP 工具适配、AGENTS.md 哈希上下文、
   apply_patch 校验、typed approval/event 协议；不改 FCode 的 runtime 信任模型。

不建议移植 Rust runtime，也不建议让 Codex 的审批/沙箱逻辑替代 FCode 的 gate。

## 一、Codex 的关键实现

### 分层

```text
protocol        Op / EventMsg / AskForApproval / PermissionProfile / SandboxType
config          TOML 分层、requirements、permission profiles
tools           模型可见的 ToolSpec / ToolPayload / ToolExposure
execpolicy      Starlark 规则引擎（prefix_rule / network_rule）
sandboxing      Seatbelt / bwrap+seccomp / Windows sandbox
core            session/turn loop、approvals、MCP、skills、AGENTS.md、apply_patch
rollout/history 追加式 JSONL + SQLite 索引
app-server      JSON-RPC API；tui/exec 是客户端
```

依赖方向是 hub-and-spoke：`core` 依赖协议/配置/工具/策略/沙箱；`tui`、`exec`、
`app-server` 依赖 `core`，不反向。和 FCode 一样，域/界面不污染内核。

### 信任顺序

`core/src/tools/orchestrator.rs` 的 `ToolOrchestrator::run` 顺序很明确：

1. **审批**：`ExecApprovalRequirement::{Skip, NeedsApproval, Forbidden}`；
2. **沙箱**：`SandboxManager::select_initial` 选择 Seatbelt / LinuxSeccomp / Windows；
3. **执行**：在沙箱内跑；
4. **升级重试**：沙箱拒绝后，经新一轮审批可扩大权限或绕过沙箱重试。

审批策略是 `AskForApproval::{UnlessTrusted, OnRequest, Granular, Never}`；
granular 还区分 sandbox/rules/skill/MCP 等类别。审批有 `ApprovedForSession`
缓存，规则可以由审批追加并热加载。

### 执行策略

`execpolicy` 是一个 Starlark DSL：

```python
prefix_rule(
    pattern=["git", "status"],
    decision="allow",
    match=[["git", "status"]],
    not_match=[["git", "push"]],
    justification="read-only repository status",
)
network_rule(host="api.example.com", protocol="https", decision="prompt")
```

规则按首 token 建索引；决策聚合是 `max(Forbidden > Prompt > Allow)`；
规则集有 `fingerprint()`，可用于审计。未匹配命令走启发式兜底，危险命令会被拒。
execpolicy 在审批之前运行，`prompt` 会变成人工审批；审批通过后可以追加持久规则。

### 工具与会话

- 工具有 trusted / external 之分，外部工具不能覆盖内置工具；MCP 工具带命名空间；
- `ToolExposure::{Direct, Deferred, DeferredModelOnly, CodeModeOnly, Hidden}`，
  大目录用 `tool_search` 延迟发现；
- `apply_patch` 有独立 DSL，先校验参数、再审批、再执行；`turn_diff_tracker`
  从已提交的 patch delta 计算净 diff，不重新读盘；
- AGENTS.md 从项目根向下拼接，限制总字节，untrusted 项目跳过；
- rollout 是追加式 JSONL + SQLite 索引，支持 resume/fork/compaction；
  **但事件没有哈希/签名，且 MCP/命令输出会被截断到 64 KiB**；
- `codex exec --json` 输出 `ThreadEvent` JSONL：thread/turn/item 事件，
  适合被外部程序消费；还有 `--output-schema`、`--output-last-message`。

## 二、不能直接搬的三件事

### 1. 沙箱拒绝后的升级重试

Codex 会在 `SandboxErr::Denied` 后申请更大权限并重跑。对 FCode 这是危险的：
命令可能已经部分执行，重试可能重复扣费/重复写入；FCode 的 `unknown` 语义明确
“不重试，只对账”。**融合时：沙箱拒绝就是该次调用的终态，记 `denied`，不自动升级。**

### 2. 会话级审批缓存

Codex 的 `ApprovedForSession` 按命令/路径复用审批，不绑定内容哈希、预算或过期时间。
FCode 的 grant 每轮从 Host 重新读取，审批是一次性的。**不要引入“本次会话都允许”
这种隐式授权；要复用就发签名、带范围、带过期时间的 grant。**

### 3. 模型 guardian 与 agent 可变规则

Codex 的 guardian 是模型审批器，execpolicy 规则也能被审批追加并热加载。
FCode 的权限、预算、规则必须由 Host 拥有、版本化、可审计；模型不能改规则，
也不能当审批权威。**规则可以提示人，但不能替人签字。**

## 三、值得借的五件事

| 借什么 | Codex 落点 | FCode 怎么用 | 风险 |
| --- | --- | --- | --- |
| execpolicy 预门控 | `execpolicy/src/parser.rs`、`core/src/exec_policy.rs` | 在 gate 前做确定性规则匹配；deny > ask > allow；规则集哈希进事件 | 低 |
| MCP 工具适配 | `codex-rs/codex-mcp`、`core/src/mcp*` | ToolSpec 适配器；固定 server/version/schema hash；每次调用走 gate/budget/receipt | 中 |
| AGENTS.md 上下文 | `core/src/agents_md.rs` | 只读项目说明，哈希进 binding；限字节；untrusted 项目跳过 | 低 |
| apply_patch 校验 | `apply-patch/src/lib.rs`、`core/src/turn_diff_tracker.rs` | 先解析/校验路径，再审批；执行后读回哈希，独立算 diff | 中 |
| typed approval/event | `protocol/src/protocol.rs`、`app-server-protocol` | 审批动作类型化；事件映射到 FCode 哈希链；UI 只读事件流 | 中 |

### execpolicy 的最小落地

FCode 不引入 Starlark 依赖也能先做一版确定性规则：

```json
{
  "version": "fcode.execpolicy/v1",
  "rules": [
    {"id": "allow-git-status", "pattern": ["git", "status"],
     "decision": "allow", "justification": "read-only"},
    {"id": "deny-rm-rf", "pattern": ["rm", "-rf"],
     "decision": "forbidden", "justification": "destructive"},
    {"id": "ask-git-push", "pattern": ["git", "push"],
     "decision": "prompt", "justification": "writes remote"}
  ]
}
```

- 只做前缀 token 匹配，不做 shell 解析；输入必须是 argv 数组，不是字符串；
- 决策聚合 `forbidden > prompt > allow`；
- 规则集规范化后算 hash，写进 gate 事件；
- `prompt` 映射到 FCode 现有行内审批，不新增“总是允许”；
- `forbidden` 直接 `denied`，不执行、不重试。

这层不依赖 Codex runtime，可以独立实现、独立测试，是最适合先做的融合点。

### AGENTS.md 的最小落地

- 从任务 workspace 根向下收集 `AGENTS.md`，限制总字节；
- 每个文件记 `{path, sha256, bytes}`，写进 binding/run manifest；
- 只作为只读上下文，不允许覆盖 Host 的权限、预算、验证规则；
- 不信任的 workspace 跳过，并显式记录“已跳过”。

## 四、路线 A：把 Codex 当受控执行器

### 适用场景

FCode 需要一个“在仓库里改代码/跑测试”的工具，但不想把 Codex 的模型当成
FCode 的决策者。Codex 只是工具，和 `read_document`、`calculate_margin` 同级。

### 契约

```text
CodexRunSpec:
  objective: str
  workspace: absolute path        # 必须在 allowed_roots 下
  writable_paths: [relative]      # 默认空，只读
  max_turns: int                  # 硬上限
  timeout_seconds: int            # 硬超时
  network: false                  # 默认关闭
  sandbox: read-only | workspace-write
  model: pinned by host           # 不允许模型自选

CodexRunResult:
  session_id, exit_status
  changed_files: [{path, before_sha256, after_sha256}]
  diff_hash
  event_count, tokens/cost (if reported)
  stdout/stderr tail (bounded)
  verified: bool                  # 独立读回后的结果
```

### 执行流程

```text
Policy 选择 codex_delegate（候选里必须有它）
  → Host 校验 workspace 根、writable paths、预算、网络、超时
  → 人类审批（一次性 grant，带范围和过期）
  → 快照 workspace：文件哈希 + git status
  → 运行 codex exec --json --ephemeral --skip-git-repo-check \
        --sandbox <profile> --ask-for-approval never <prompt>
  → 解析 JSONL；只信事件结构，不信模型自述
  → 进程结束/超时/崩溃：
       成功 → 读回所有改动文件，算 after hash 与 diff
       失败/超时/未知 → recovery_required 或 denied，不自动重试
  → 校验：只改了允许路径；没有网络；diff hash 稳定；无越权文件
  → 记录 tool_succeeded/denied + 收据；输出给模型的是 diff 摘要，不是“我完成了”
```

关键点：

- Codex 内部也有沙箱/审批；FCode **不能因此免除自己的 gate**。外层仍要有
  workspace 隔离、超时、网络关闭、路径白名单；
- `--ask-for-approval never` 只在这个一次性、人工批准的受限任务里使用；
  没有外层 grant 就不允许；
- 不把 Codex 的 `turn.completed` 当成 FCode 的成功；FCode 只认独立读回和 diff 校验；
- 超时/进程被杀 = unknown；按 FCode 规则进 `recovery_required`，不重跑；
- Codex 的模型调用是外部计费，Host 必须在发送前预留预算；本地模型不适用这条。

### 本机状态

`which codex` 为空，当前设备只有 Claude CLI；要走路 A，需要先安装 Codex CLI
（`npm i -g @openai/codex` 或官方 binary），并确认账号/条款允许这种用法。
在安装前，路线 B 的 execpolicy/MCP/AGENTS.md 可以先做。

## 五、路线 B：只借架构模式（推荐先做）

### 优先级

| 顺序 | 内容 | 工作量 | 为什么先做 |
| --- | --- | --- | --- |
| 1 | execpolicy 预门控 | M（1–2 周） | 纯确定性、低风险、直接增强可审计性 |
| 2 | AGENTS.md 哈希上下文 | S（2–3 天） | 不改信任模型，补项目上下文 |
| 3 | MCP 工具适配 | M（2–3 周） | 扩展数据源，但必须固定 server/schema |
| 4 | approval/event 协议 | M | 统一 UI 与链事件；为 codex_delegate 铺路 |
| 5 | apply_patch 校验 | M | 只有加入代码工具后才需要 |
| 6 | sandbox 后端 | L（数周） | 高风险；等确有 shell/code 工具再做 |

### MCP 适配的硬约束

- 固定 server 身份、版本、工具 schema hash，写进事件；
- MCP 返回的 title/description/annotation 一律当**不可信文本**，不能作为授权依据；
- 每次 `call_tool` 都走 FCode 的 gate/budget/receipt；
- 默认禁网；需要网络的工具单独审批，域名白名单；
- 不缓存审批，不做“本会话都允许”；
- 失败/超时按 unknown 处理，不自动重试。

### 不借的清单

- 沙箱拒绝后的自动升级重试；
- 会话级审批缓存；
- agent 可变、热加载的 execpolicy；
- 模型 guardian 作为审批权威；
- 截断持久化输出（审计会丢证据）；
- 信任 MCP 提供的元数据；
- 无哈希的 rollout 作为事实源；
- 通用 shell 逃生口绕过 Host gate。

## 六、对 FCode 框架的位置

在 `framework.md` 的三平面里，Codex 属于 **Executive 的工具/执行后端**，不属于
Policy 或 Voice：

```text
Executive（Host）
  ├─ execpolicy 预门控（规则哈希进事件）
  ├─ codex_delegate 工具（可选，受控执行器）
  ├─ MCP 工具（受控外部工具）
  └─ apply_patch / diff 校验（代码任务）
Policy：只选候选，不知道 Codex 的存在
Voice：只解释证据，不读 Codex 的原始输出
```

这保证 Codex 再强，也不能绕过 FCode 的预算、审批、复算和哈希链。

## 七、建议的下一步

1. **先做 execpolicy 预门控**：JSON 规则 + argv 前缀匹配 + 规则集哈希 + gate 事件；
   不改 runtime 信任模型，可独立测试。
2. **再做 AGENTS.md 哈希上下文**：只读、限字节、进 binding。
3. **然后评估 MCP 适配**：先接一个只读数据源，固定版本/schema，跑满 gate/budget/receipt。
4. **Codex 执行器作为 lab**：安装 Codex CLI 后，用只读/隔离 workspace 跑一个
   `codex_delegate` 原型；先只验证 diff 和超时语义，不接控制台默认路径。
5. 任何一步都不把 Codex 的审批/沙箱当成 FCode 的最终权威。
