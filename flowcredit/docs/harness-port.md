# Harness 移植：行为等价的 Claude Code harness 层（S1–S3）

2026-11-19 起对 Claude Code 2.1.302 做二进制级逆向，只移植**架构契约与行为
语义**，不拷源码（对方是专有 TS/Bun 产品）。实现在
`src/flowagent/harness/`，与既有 `runtime/`、`capabilities/` 完全独立；
S5 之前 Work 产品路径行为零变化。

## 分期

| 阶段 | 模块 | 状态 |
| --- | --- | --- |
| S1 循环 | `core.py` `tools.py` `provider.py` `events.py` | 完成 |
| S2 权限 + Hooks | `permissions.py` `hooks.py` `pipeline.py` `settings.py` | 完成 |
| S3 压缩 + 会话树 | `compaction.py` `session.py` | 完成 |
| S4 子代理 + 后台任务 | `subagents.py` `background.py` | 完成 |
| S5 Work 接管（flag） | `runtime/capability_core.py` + bridge 路由 | 完成（flag 默认 legacy） |
| S6 PostToolUse 挂治理审计 | — | 待做 |

## S1：一条不知道业务的主循环

- `HarnessCore.run(messages, system, tool_names, max_turns, call_deadline_ms,
  tool_deadline_ms)`：流式 propose → check → call → append。
- 工具是协议对象 `{name, description, input_schema, handler}`；schema 在
  handler 之前校验，任何失败都变成模型可读的 `tool_result`，不抛穿循环。
- 事件顺序：`tool.describe → loop.turn → assistant.message → tool.check →
  tool.call → tool.result → loop.end`；全部事件是 chain-safe JSON（整数
  `at_ms`，无浮点）。
- 每次模型调用有墙钟 deadline：provider 流由 daemon 线程泵入队列，超时置
  `cancel` 并返回 `CALL_DEADLINE`；工具用同一 join-with-timeout 模式。
- 循环只因 `max_turns`、provider 异常或 deadline 停止。

## S2：权限与生命周期 Hooks

权限（`PermissionPolicy`）：

- 三个规则集 `allow / ask / deny`，规则形如 `Bash(git:*)`、`Edit`、
  `WebFetch(domain:*.github.com)`；匹配内容按工具取（command / path / host）。
- 裁决顺序 `deny > ask > allow`，集合内 managed 规则先评估；`ask` 只经注入
  的 approver 解析，没有 approver 时明确拒绝。
- 模式：`default` / `acceptEdits` / `plan` / `bypassPermissions`；后者受
  managed 的 `disableBypassPermissionsMode` 门控。上下文压力下
  `shed_allow_rules` 只削用户/本地授权，managed 授权保留。

Hooks（`HookRunner`）：

- 10 事件：SessionStart / UserPromptSubmit / PreToolUse / PostToolUse /
  Notification / Stop / SubagentStop / PreCompact / PostCompact / SessionEnd。
- matcher 支持 `Bash`、`Edit|Write`、`*`；命令从 stdin 读 JSON 事件。
- 退出码 2 或 stdout `{"decision":"block"}` 阻断（stderr 变原因）；
  `{"updatedInput": ...}` 改写参数；超时记 `HOOK_TIMEOUT` 但不阻断；
  managed 先于 user/project/local 执行。
- 循环自己拥有 SessionStart / PostToolUse / Stop；PreToolUse 属于权限管道。
- PostToolUse 阻断时把原因追加进 `tool_result`，模型能看到并修正。

配置（`load_settings`）：

- 四层 `managed（FCODE_MANAGED_SETTINGS）→ project .fcode/settings.json →
  user ~/.fcode/settings.json → local .fcode/settings.local.json`。
- 坏 JSON、坏规则、未知事件只记 `warnings`，不阻断运行。

## S3：压缩与会话树

压缩（`Compaction`）：

- 触发：`min(0.95 × window, window - reserve)`，下限 100k tokens；窗口
  ≤100k 不自动压缩；空闲 1h 且上下文 ≥1000 tokens 也可触发。
- 摘要请求是纯文本调用（无工具），先写 `<analysis>` 草稿，再输出固定九段
  `<summary>`：Primary Request / Key Technical Concepts / Files and Code /
  Errors and fixes / Problem Solving / All user messages / Pending Tasks /
  Current Work / Optional Next Step。
- 二次压缩时，旧摘要的 “All user messages” 必须逐字保留（keepEarlierAsks）：
  先注入 prompt，模型漏写时由 `ensure_earlier_asks` 合并回第六段。
- 压缩后历史替换为一条 continuation user 消息；`compact_boundary` 事件带
  `before_tokens / after_tokens / summary_sha256 / trigger / sections`。
- 压缩失败是 best-effort：发 `compact.error`，本轮禁用压缩并继续跑。

会话树（`SessionStore` / `FileHistory`）：

- `<root>/projects/<project-slug>/<session-id>.jsonl`，追加写；每行
  `{uuid, parentUuid, type, atMs, sessionId, cwd, ...}`。
- `rewind(uuid)` 只移动写游标：后续事件从该节点分叉，旧分支保留（fork 语义，
  非破坏性）；`chain()` / `messages()` 按 parentUuid 回溯。
- `compact_boundary` 作为事件入树，不改写历史。
- `FileHistory` 以 sha256 去重存快照，`manifest.jsonl` 记录 messageUuid →
  path/sha；支持 `restore`，缺失文件记 tombstone。

## S4：子代理与后台任务

子代理（`AgentRegistry` / `SubagentRunner`）：

- 定义 `AgentDefinition{agent_type, when_to_use, tools, model, max_turns,
  system}`；`allowed_agent_types` 过滤可见类型，`max_depth` 限制嵌套。
- 父代理只看到一个编排工具 `task`，`subagent_type` 是允许类型的 enum，
  描述里带每类的 `when_to_use`。
- 子代理用同一个 `HarnessCore` 跑，但工具集是定义声明的子集；结果以
  `SubagentResult{status, text, turns, usage, code, error, model}` 回传，
  `tool_result.data` 带 typed 载荷（provider 只把 `content` 发给模型）。
- 到达 `max_depth` 时，子代理的工具表里不再注册编排工具——嵌套在结构上
  被移除，不靠模型自觉。
- `provider_factory(model)` 可按定义切换 provider；事件
  `subagent.start` / `subagent.end` 带深度、状态、用量。

后台任务（`BackgroundTasks`）：

- `start(command)` 用独立会话启动 shell，stdout/stderr 直接写入
  `<root>/background/<task_id>.log`；`max_running` 限制并发。
- `output(task_id, offset, limit, block, timeout_ms)` 返回
  `{output, next_offset, status, eof, exit_code, remaining_bytes}`，按字节
  偏移增量读，不重放全量。
- `kill(task_id)` 先 terminate、超时再 kill；已结束任务重复 kill 是 no-op。
- 工具：`run_background` / `task_output` / `kill_shell`；事件
  `background.start` / `background.output` / `background.kill`。
- 注册表是进程内状态；日志文件在重启后仍在，但活进程句柄不持久化。

## S5：Work capability 路径接管（flagged）

`HarnessCapabilityBridge` 的 `propose()` 现在有两条路：

- legacy：`harness.propose()` → `OpenAICompatProvider.propose`（原有路径）；
- core：`harness.propose_with(task, context, CoreBackedProvider)`，只替换
  模型调用的引擎，任务状态机、预算 reserve/settle、plan 校验、重试与
  action gate 全部保持原实现。

`CoreBackedProvider`（`src/flowagent/runtime/capability_core.py`）：

- 用同一个 `render_prompt(context)` 生成 system/user，prompt 字节不变；
- 把该 stage 的 native tool schemas 转成 capture-only `ToolSpec`，跑一次
  `HarnessCore.run(max_turns=1)`；工具调用只被记录，不会执行；
- 只取第一个 tool call（其余计入 `dropped_tool_calls`），构造与旧 provider
  相同的 `Proposal`（tokens/cost/receipt/raw_sink）；
- 传输仍走 `CompatClient._post_stream`：SSE、零字节重发、部分流审计不变；
- 旧路径的所有重试/预算语义因此继续生效，失败码保持
  `MODEL_CALL_FAILED` / `EMPTY_CONTENT` / `PROPOSAL_MALFORMED`。

开关（默认 legacy）：

- 环境变量 `FCODE_WORK_HARNESS=core`；
- 运行 spec `capabilityHarness: "core"`（优先级高于环境变量）；
- 评测脚本 `scripts/evaluate_work_pelican.py --harness core`。

### 鹈鹕 R7 对账（deepseek-flash，低推理）

两次真实样本（同一 prompt/profile，同一场景包）：

| 路径 | 结果 | 阶段 | 质量 | calls | tokens | 成本 µUSD |
| --- | --- | --- | --- | --- | --- | --- |
| core #1 | failed | draft | unverified | 3 | 31,462 | 13,876 |
| core #2 | failed | motion | fail / 70 | 10 | 132,451 | 54,067 |
| legacy 对照 | failed | motion | fail / 70 | 9 | 100,212 | 37,769 |

- core #1 是模型采样失败：连续三次 draft edit 超过 3000 字符，修复票据被
  无视；同轮 legacy 只写了 39 字符，说明首个 prompt 完全相同
  （2,336 tokens），差异来自采样而非适配层。
- core #2 与 legacy 都进入 motion，且卡在同一个已知几何问题
  （leg/wheel 包围盒重叠：4,548px² vs 99px²），质量分同为 70；没有出现
  任何 harness 专属失败（schema、deadline、tool 协议）。
- core 路径本轮多一次 proposal retry，成本高约 43%、耗时 437s vs 343s；
  样本量只有 1+1，不足以判断系统性差异。

结论：S5 的行为等价性成立到“同一质量墙”；质量瓶颈仍是 rig/几何，不是
harness。默认保持 legacy，等 rig 修复后再翻默认。

## M1：证据运行时（evidence-by-construction）

护城河的运行时不变量：**每次派发必然产出链式证据回执，"执行了但没有回执"
在这一层不可表示。**

- `harness/evidence.py::EvidenceChain`：每次 `_dispatch`（执行/拒绝/参数
  非法/未知工具/执行异常，五种结果全覆盖）产出 `EvidenceReceipt`——
  `tool / tool_use_id / args_sha256 / result_sha256 / ok / code /
  duration_ms / world / prev_sha256 / sha256`；canonical JSON、整数毫秒、
  内容从不内联（只有哈希）。
- 链式绑定：每条回执带前一条的 sha256；`verify_evidence_chain` 重算全部
  哈希与 prev 链接，篡改/删除/乱序任意一条即被证伪。
- `world` 是工具**自己声明**的文件事实（`ToolResult.data["evidence"]`，
  path+sha256，≤16 条）；内核不发明它没观测到的世界状态。
- `HarnessResult` 携带 `evidence_head` + 全量回执；总线事件 `tool.evidence`
  同步入事件树；子代理 `SubagentResult.evidence_head` 与 S5 proposal 元
  数据 `evidence_head` 自动继承——**runner 零改动**。
- 纯文本轮次没有工具派发，head 为 ZERO_HASH（64 个 0），不伪造事实。

## 不变量

- 新包不依赖 `runtime/` / `capabilities/`；S5 前不改 Work 行为。
- 金额与计数是整数（µUSD / tokens），事件里不出现浮点。
- 权限、hooks、配置错误都不能把运行搞崩；只有 provider / deadline /
  max_turns 能终止循环。
- 压缩是可选装饰：`compaction=None` / `session=None` 时 S1 行为完全不变。

## 测试

```bash
python3 -m unittest \
  tests.test_harness_core tests.test_harness_permissions \
  tests.test_harness_hooks tests.test_harness_settings \
  tests.test_harness_compaction tests.test_harness_session \
  tests.test_harness_subagents tests.test_harness_background \
  tests.test_harness_work_adapter
```

当前 106 个 harness/适配测试；全量 Python 套件 1,599 tests OK（skipped=11）。
S1–S4 全部使用脚本化 provider，无网络、无真实模型；后台任务测试使用本地
shell 与临时目录；S5 另有两次真实 DeepSeek 评测（见上）。
