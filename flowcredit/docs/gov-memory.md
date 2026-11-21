# 治理上下文与记忆核心（Gov Memory Core）

治理区原先只有一份每轮重算的静态快照：对话超过 24 条就静默丢弃，操作员记忆写进库却从不读，新运行也看不到上一次的发现。这个切片把「制度、工作记忆、压缩、按需检索」补齐成一条可审计的链路。

> 原则：prompt 视图每轮从事件链与制度文件确定性重建；模型不写长期记忆，只能调用只读检索工具。

```text
GOVERNANCE.md（用户 → 项目 → 本机，近者后读）
        │  每轮注入，文件哈希入链
        ▼
治理 system prompt ── 快照 v2（分段预算，最新优先）
        │                 └─ 角色 / 待批准 / 最近运行 / 最近审计 / 最近计划 / 最近判断
        │  超阈值
        ▼
harness.compaction 九段式摘要 ── chat_compact 事件（摘要哈希 + usage）
        │
        ▼
最近 24 条消息 + 压缩摘要
        │  快照不够
        ▼
只读 JIT 工具：list_audits / read_audit / read_run / read_memory
        └─ 结果带 result_hash + trust=external_untrusted 入链
```

## 1. 制度层（`GOVERNANCE.md`）

加载顺序（broadest first，nearer last）：

1. `$FCODE_CONFIG_DIR/GOVERNANCE.md`（未设置时 `~/.fcode/GOVERNANCE.md`）
2. `<repo>/GOVERNANCE.md`
3. `<repo>/GOVERNANCE.local.md`（本机，gitignore）

规则：

- `@path` 导入相对当前文件解析，最多 4 跳；只能停留在项目或用户制度目录内，越界会记录原因并跳过；
- 块级 HTML 注释注入前剥离，代码块里的注释保留；
- 合并文本上限 40,000 字符，超限截断并显式标注；
- 每轮把合并文本的 `sha256`、文件列表、字符数写入 `chat_context` 事件。

制度只放模型推不出来的内容：红线、处置纪律、派活约束。仓库根部的 `GOVERNANCE.md` 是默认模板。

## 2. 快照 v2

`GovChatService.snapshot()` 生成六个分段，每段有独立字符上限：

| 段 | 内容 | 优先级 |
| --- | --- | ---: |
| 待批准运行 | 数量 + 前 8 条 pending | 100 |
| 最近审计 | 最近 6 个 bundle（日期新→旧，含 severity/unhandled/regression 计数） | 95 |
| 最近运行 | 最近 8 条运行 | 80 |
| 最近判断 | 最近 5 条 triage 摘要与优先级 | 70 |
| 最近计划 | 最近 6 个计划与步骤计数 | 60 |
| 角色 | 前 6 个 bot 的 charter | 40 |

全局预算 6,000 字符。超预算时**整行**从最低优先级开始裁剪，并在 `chat_context.snapshot_trimmed` 中记录被裁剪的段；不再对整串做尾部截断，因此「最近审计」不会因为排在最后而被切掉。

审计包排序按目录/审计 id 中的 `YYYY-MM-DD` 倒序；同日期保持原字母序，避免历史行为突变。

## 3. 压缩

- 阈值：`FCODE_GOV_COMPACT_TOKENS`，默认 `50000`，设为 `0` 可关闭；
- 触发时对**全线程**（不只 24 条窗口）调用 `flowagent.harness.compaction` 的九段式摘要器；
- 摘要写入 `chat_compact` 事件：`summary`、`summary_sha256`、`before_tokens`、`usage`、`trigger=auto`；
- 下一轮 prompt 在制度之后、快照之前注入「更早对话的压缩摘要」，因此摘要会跨过窗口存活；
- 摘要调用失败或模型返回空文本时静默放弃，对话继续，不产生半截事件。

## 4. 只读 JIT 工具

快照故意保持小。问到被裁掉的具体记录时，模型调用只读工具，而不是主机预加载全部数据：

| 工具 | 参数 | 返回 |
| --- | --- | --- |
| `list_audits` | `limit`（1–20） | 最近 bundle 的 dir/audit_id/target/findings/unhandled/ruleset |
| `read_audit` | `audit_id`（目录或审计 id） | ≤50 条 finding 的 id/类别/严重度/根因/处置/PoC 引用/指纹、未覆盖面、regression 计数 |
| `read_run` | `run_id` | 运行状态、目标、audit_id、error、前 5 条 triage 优先级 |
| `read_memory` | `query`（可空） | 最多 5 条操作员长期记忆 |

权限：`EFFECT_READ` + 显式 `Permissions` 白名单；没有工作区沙箱，没有写/网络/终端工具。每次调用的结果都带 `result_hash` 与 `trust=external_untrusted` 进入同一事件链，工具结果只是数据，不是指令。

## 5. 链上事件

| 事件 | 写入时机 | 关键字段 |
| --- | --- | --- |
| `chat_context` | 每次回答前 | policy_sha256、policy_files、snapshot_sha256、snapshot_chars、snapshot_sections、snapshot_trimmed、memory_injected、compaction_sha256 |
| `chat_compact` | 压缩成功 | summary、summary_sha256、before_tokens、usage、trigger |
| `chat_tool_started` / `chat_tool_result` | 工具调用 | name、arguments、status、result_hash、trust |

`chat_context` 让「这一轮模型看到了什么制度、哪版快照、有没有记忆、用了哪份摘要」可复核；`chat_compact` 让摘要本身可复核；工具结果哈希让「查到的到底是什么」可复核。

## 6. 边界

- 制度文件是**上下文**，不是强制配置；真正的硬门在权限与审批层。
- 压缩摘要由模型生成，只作背景；摘要哈希入链，但摘要文本不等于裁决。
- 快照与工具结果都是不可信数据，出现指令式文字时按数据引用处理。
- 治理记忆只收操作员文本与确定性投影；模型不能写长期记忆。
- 跨 run 的「上次发现/计划目标/skill 过程」注入仍属下一层，当前只做读取与检索。
