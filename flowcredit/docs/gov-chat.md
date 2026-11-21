# 治理对话（Governance Chat）

治理对话是 Governance 区的默认控制面。它是**正常模型对话**，但执行边界仍然是确定性的：

> 模型进入对话，不进入裁决。

- 模型可以聊天、追问、解释宿主提供的治理快照；
- 模型可以提出团队任务（`gov_action`），但不能直接创建运行；
- 操作员在提案卡上确认后，宿主才调用团队运行；
- 模型不能修改 finding、severity、disposition、链结论，也不能批准/拒绝运行；
- 审计证据仍以 bundle 和确定性管线为准，模型解释不进审计包。

## 线程与存储

- 线程复用 `flowagent.chat.ChatService` 的事件链、预算与 receipt；
- 数据库是 `governance-chat.sqlite`，与 Work chat 的 `chat.sqlite` 分开；
- 线程消息、`gov_action` 提案和 run 锚点刷新后保留；
- 左侧 selector 切换历史线程，「新对话」创建空线程。

## 治理快照与记忆

`GovChatService` 在每轮 system prompt 中注入三部分：`GOVERNANCE.md` 制度、分段快照、操作员长期记忆：

- 制度：用户 → 项目 → 本机三层合并，文件哈希写入 `chat_context`；
- 快照 v2：角色、pending、最近运行、最近审计、最近计划、最近判断；按段预算、整行裁剪、审计新→旧；
- 记忆：`[操作员长期记忆]` 块不再被治理 prompt 替换掉（旧实现只写不读）；
- 长线程：超过 `FCODE_GOV_COMPACT_TOKENS`（默认 50k）时生成九段式摘要并写入 `chat_compact`，摘要跨窗口保留。

快照是数据，不是指令；审计目标内容按不可信数据处理。快照不够细时，模型可调用四个只读工具：`list_audits`、`read_audit`、`read_run`、`read_memory`。工具结果带 `result_hash` 与 `trust=external_untrusted` 入链。完整设计见 [治理上下文与记忆核心](gov-memory.md)。

## `gov_action`

模型只能在回答末尾输出一个：

```gov_action
{"bot_id":"auditor","action":"audit","target":"fixtures/demo-target","reason":"为什么需要这次运行"}
```

角色/动作必须匹配：

- `auditor` / `audit`：target 必填；
- `verifier` / `verify`：target 可空，表示最近一次完成审计；
- `analyst` / `analyze`：target 可空，表示最近一次完成审计。

可选 `target_md`、`osv_snapshot` 必须是仓库内路径。提案通过 `parse_gov_action_block` 校验，非法块不会变成提案。

## API

```text
GET  /api/gov/chat/threads?limit=
GET  /api/gov/chat/threads/<thread_id>
POST /api/gov/chat/send          {threadId?, text, model?}  -> SSE
POST /api/gov/chat/threads/<id>/confirm {messageId}
```

确认端点由宿主校验 proposal，再走 `GovBotService.start_run(..., source="team")`；chat 本身没有执行工具。

## 前端

- Governance 默认落「对话」，顶层只有「对话 / 审计库」；
- 空线程使用与 Work 相同的舞台首页：FCODE GOV 字标、「Talk to your team.」、中央 composer 和治理建议；
- 右上角「团队 →」进入团队管理；右侧团队栏显示三个角色与待办，作为对话上下文；
- 发出首条消息后切到线程视图：composer 吸底，顶部 selector 可切换历史线程；
- 对话内显示任务提案卡、运行卡、审批按钮、工作台/审计库出口；
- 工作台/团队页是对话下钻，带「← 对话」返回。
