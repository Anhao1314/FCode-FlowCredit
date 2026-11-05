# 用文件式 Oracle 代替 LLM：训练前的 LLM 层实验

Qwen3-4B 还在训练时，唯一能验证“模型层是否接得对”的办法，是让另一个模型（或人）
坐进 Provider 的位置。实验装置不绕过任何生产代码：它使用真实的
`render_prompt`、`validate_plan`、工具门控、预算、执行与独立验证，只把
`POST /chat/completions` 换成文件交换。

## 协议

```sh
cd flowcredit
PYTHONPATH=src python3 -m flowagent.demo.oracle \
  --workdir /tmp/fcode-oracle-full --scenario full --fresh --timeout 1800
```

进程每轮写出：

```
/tmp/fcode-oracle-full/exchange/round-01.request.json
  {"round": 1, "prompt_version": "fcode.prompt/v9",
   "system": "...", "user": "<canonical context>", "context": {...}}
```

操作者读取 `system`/`user`，写回：

```
round-01.reply.json  {"version": "flowagent.plan/v1",
                      "steps": [{"id": "t1", "tool": "list_documents", "arguments": {}}],
                      "todo_id": "t1"}
```

回复由与本地 Qwen 供应商相同的 `parse_proposal_text` 解析。缺回复是
`PROVIDER_TIMEOUT`，格式错是 `PROPOSAL_MALFORMED`；两者都是 unknown，不重试、
不回退 mock。实验产物落在独立 workdir，不污染控制台的案例簿与事实簿。

## 实验结果（2026-10-20）

| 实验 | 轮次 | 事件 | 调用 | 成本 µUSD | 终态 | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| `full`（v7） | 9 | 49 | 9 | 4,500 | succeeded | FY2024 17.50%，FY2025 15.00%，-2.50pp |
| `open-ended`（v7，修复前） | 9 | 48 | 9 | 4,500 | failed | INSUFFICIENT_EVIDENCE；模型看不到阻塞原因 |
| `open-ended`（v8，修复后） | 7 | 38 | 7 | 3,500 | failed | 第 6 轮发现桥接缺失，第 7 轮诚实停止 |
| `budget`（v8，修复前） | 1 | 8 | 1 | 500 | failed | 预算只剩 4 次，但只能误报 `insufficient_evidence` |
| `budget`（v9，修复后） | 1 | 8 | 1 | 500 | failed | BUDGET_EXHAUSTED，模型主动停止 |

`full` 的模型侧 prompt 从 7,220 B 增长到 7,751 B；工具目录 15 个，实际用到 9 个。
所有提案都通过门控，没有越权或 schema 错误。

## 实验发现与已修问题

1. **失败观测被当成成功状态。** `reconcile_basis` 返回 `reconciled=false` 时，
   `WorkingMemory.state()` 仍把 FY2025 记入 `reconciled_periods`。修复后只认
   `reconciled=true`，并把失败原因放进 `blockers`。
2. **压缩观测丢掉了失败原因。** 原摘要只保留 `period/from_basis/delta/…`，
   阻塞时全为 null。修复后摘要显式带 `reconciled` 与 `reason`，成功时才带
   `reconciled_gross_profit`。
3. **`report_blocked` 不终止运行。** 模型在计划仍有 pending todo 时报阻塞，
   Loop 会继续追问，白耗预算，甚至可能撞上轮次上限。修复后
   `report_blocked` 直接以 `INSUFFICIENT_EVIDENCE` 终止，并优先采用计划里已
   blocked todo 的领域原因。
4. **其余摘要补全。** `check_restatement` 现在带 `restated_gross_profit`；
   `calculate_margin` 现在带两期毛利率，模型不必从变化反推。
5. **预算不足没有可用的阻塞原因。** `budget` 场景只剩 4 次调用、计划还有 9 步，
   模型只能把预算问题误报成 `insufficient_evidence`。现在枚举增加
   `budget_insufficient`，终止码映射为 `BUDGET_EXHAUSTED`。

## 仍然存在的边界

- **这是“计划执行者”，不是“规划者”。** 当前 `plan` 直接把 tool 和 arguments
  填好，模型只需选第一个 pending todo。实验证明指令遵循与门控链路可用，但没有
  测到真实拆解能力。下一步需要 planner actor 分离，或让模型自己产出 todo。
- **`list_documents` 只给 doc id。** 模型看不到标题、期间、kind、unit；文档一多
  就无法选择。当前靠计划预先指定 doc_id 绕过。
- **15 个工具（实验当时）里只用了 9 个。** `search_cases`/`query_facts` 等记忆工具不在计划
  里，模型没有动机调用；这不代表它们没用，而是说明“工具可见”不等于“工具会被
  选择”。
- **没有模型驱动的计划修订。** 跳过/提前仍由操作员控制；模型只能报告阻塞。
- **Oracle 延迟没有参考价值。** `latency_ms` 包含操作者阅读时间，不能当模型性能。

## 用途

这组装置适合在训练前后回答三个问题：

1. 同一套 prompt 与工具，换模型后提案是否仍然合法、可门控、可验证；
2. 上下文是否足够让模型做出正确选择，还是计划/工具 schema 在替它思考；
3. 失败时模型能否诚实停止，而不是把“没有证据”说成结论。

等 `LocalProvider` 接入后，同一批场景和同一份 trace 可以直接做 A/B：oracle 的
决策作为参考轨迹，比较本地模型的工具选择、轮次、成本和终态。
