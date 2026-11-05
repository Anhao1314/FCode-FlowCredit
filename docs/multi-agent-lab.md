# 多 Agent Lab：确定性角色编排

> 结论先行：这是一个**可复现的角色编排实验**，不是多个 LLM 自主协作。  
> Planner/Reviewer 是规则实现，Executor 复用现有 `Loop`；角色之间没有独立进程、
> 独立模型或工具授权。所有数字都能从 `.runs` 下的 manifest/state/child trace 重算。

## 1. 为什么做

FCode 的单 Agent 运行时已经能证明：模型只提议，Host 门控/预算/执行/复算，事件进
哈希链。多 Agent 的问题不是“再加几个 prompt”，而是：

- 角色之间如何 handoff，且不能靠自由文本扩权；
- 每次尝试如何留下独立可验证的 trace；
- Reviewer 拒绝后如何重试，而不重复执行、不重复扣费；
- 预算、父子关系、角色身份放在哪里；
- 如何诚实区分“Runtime succeeded”和“Reviewer accepted”。

本 lab 只回答这些编排问题，不训练模型，也不把结果包装成“多 Agent 比单 Agent 更聪明”。

## 2. 结构

```text
Planner ──Handoff(plan)──▶ Executor ──ChildResult──▶ Reviewer
   │                          │                         │
   │                          ├─ child task / hash chain│
   │                          └─ trace / budget         │
   └──────────── Manifest（SHA-256 prev_hash 链）────────┘
```

- **Planner**：`DeterministicPlanner`。`plan(spec)` 复用 `FinancePlanner` 蓝图；
  `revise` 只允许 `drop:<todo>`、`variant:<x>`、`add:<tool>`，不持有工具。
- **Executor**：`ChildExecutor`。每次 attempt 创建独立 child task，写
  `plan_steered`，再用现有 `Loop` + `MockProvider` 执行；不修改 runtime。
- **Reviewer**：`RuleReviewer`。只看 `ChildResult` 摘要/plan/state；没有 registry、
  harness 或工具；不写 `review_decided`。
- **Orchestrator**：拥有 manifest、attempt 上限、预算 ledger、duplicate guard、
  no-progress guard、resume。

### 共享状态

`MultiAgentState` 持久化到 `state.json`，关键字段：

`run_id, parent_run_id, objective, task_spec, plan, plan_revision, attempts,
child_runs, handoffs, verdicts, budget_ledger, status, outcome, state_hash`。

`state_hash` 只覆盖可复现字段（run/task/attempt/verdict/budget settled/status/
outcome），不覆盖 wall clock。manifest 每条事件：

```json
{"sequence": 1, "type": "handoff", "at": "...", "payload": {...},
 "prev_hash": "...", "hash": "sha256(canonical_json({sequence,type,payload,prev_hash,at}))"}
```

manifest 只做 SHA-256 + prev_hash，不做 HMAC；child runtime 的签名收据仍然是它自己的
链，两个层不混。

### Handoff

```json
{
  "message_id": "h-...", "run_id": "ma-...", "from": "planner", "to": "executor",
  "kind": "plan", "task_id": "ma-...-a1",
  "plan": {"goal": "...", "revision": 1, "todos": []},
  "allowed_tools": ["list_documents", "read_document"],
  "grant": {"grant_id": "...", "revision": 1},
  "limits": {"max_calls": 12, "max_reads": 20, "max_cost_microusd": 20000, "max_tokens": 20000},
  "constraints": {"max_rounds": 12},
  "notes": "",
  "payload_hash": "sha256:..."
}
```

白名单字段、`allowed_tools` 必须是父任务的子集、plan 里 pending/active 的 todo
工具必须在 allow-list 内、limits 不能超过父预算、notes ≤ 2 KiB。Executor→Reviewer
只允许 `kind="result"`，不带任何工具授权。

### 控制流

```text
plan = planner.plan(spec)
for attempt in 1..2:
    handoff = build_handoff(plan, remaining_limits)
    validate handoff (scope/limits/hash)
    if duplicate(plan, prior child tool signatures): stop DUPLICATE_OBSERVATION
    reserve budget; if cannot: stop BUDGET_EXHAUSTED
    child = executor.run(plan, attempt)
    settle budget; link child trace
    verdict = reviewer.review(child, plan, state)
    if accept: stop ACCEPTED
    if stop: stop FAILED / BUDGET_EXHAUSTED
    if same verdict twice: stop NO_PROGRESS
    if attempt == 2: stop REJECTED / FAILED
    plan = planner.revise(plan, verdict)
```

- `max_attempts = 2`；每次重规划都是新 child run。
- 预算 ledger 在 child 启动前用剩余额度做硬检查；child 内部仍由 `Harness` 扣 task 预算。
- duplicate guard 在 attempt≥2 时比较新 plan 的 `(tool, canonical(args))` 与已执行
  child 的签名；重叠即停止。
- Reviewer 两次返回同一 `(kind, required_changes)` 时停止 `no_progress`。
- manifest resume：读取 state，校验 manifest，逐个校验 child trace/chain head；
  已完成的 child 不重跑。

## 3. 8 个测试

| 测试 | 覆盖 | 关键断言 |
| --- | --- | --- |
| T1 正常 | full → 成功 | child `succeeded`；链验证；manifest `accepted`；1 child；2 handoffs（plan + result）；预算不超 |
| T2 工具失败 → 重规划 | 坏首步 `write_document` → denied | child1 `denied`；child2 `succeeded`；`replan=1`；无重复好工具；总预算不超 |
| T3 预算耗尽 | `budget` fixture，calls=4 | child `failed`；calls=4/reads=4；无第 5 次工具执行；manifest `budget_exhausted` |
| T4 Reviewer 拒绝 | toy protocol，两次 reject | attempts=2；无第 3 child；manifest `rejected`；两个 child trace 仍 `succeeded`；`review_status=reject` |
| T5 No-progress | toy protocol，连续 replan | 第 2 次后 `no_progress`；无第 3 child |
| T6 权限不可扩张 | reviewer 要求 `write_document` | handoff 校验失败；child2 不启动；manifest `invalid_handoff` |
| T7 Manifest resume | 重规划任务在 attempt1 后崩溃 | child1 task/events 不新增；resume 后 attempt2 正常；manifest 链完整 |
| T8 Reviewer 无工具 | T1 的 Reviewer 阶段 | Reviewer 无 registry/harness；verdict payload 无 tool 字段；child 的 tools 属于 Executor |

测试命令：

```bash
cd flowcredit
python3.12 -m unittest discover -s tests -v
```

结果：**192 tests OK**（184 existing + 8 lab）。

### 两个必须说明的实现事实

1. **T3 的第 5 个 `model_started`**：`Harness._propose` 先写 `model_started`，再
   `charge_call`；第 5 次尝试会留下 start 事件，但 provider 没有被调用，`calls` 仍是
   4。测试断言 `model_started=5 / tool_started=4 / calls=4`，没有伪造成 4。
2. **T4/T5/T6 用 toy protocol fixture**：金融 full 只有一个最终 `calculate_margin`，
   在“跨 child 禁止重复执行”的前提下，Reviewer 拒绝后无法靠再跑一遍金融工具成功。
   toy fixture 用 `calculate_margin(variant=a/b/c)` 验证协议，不参与金融能力结论。

## 4. 10 任务消融

任务集：`full`, `tool-denied`, `budget`, `open-ended`, `unknown`, `full-strict`,
`tool-denied-recoverable`, `open-ended-alt`, `full-reviewer-extra-evidence`,
`full-noisy-plan`。`crash` 不进消融，由 T7 resume 覆盖；`budget-resume` 留给单 Agent
回归。

每项单 Agent / 多 Agent 各跑 3 次，wall time 取中位数；其余指标确定性稳定。

| task | mode | status | success | attempts | calls | reads | cost µUSD | tokens | wall ms | replans | rejects | dup | verified |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| full | single | succeeded | yes | 1 | 9 | 9 | 4500 | 9000 | 17 | 0 | 0 | 0 | yes |
| full | multi | accepted | yes | 1 | 9 | 9 | 4500 | 9000 | 20 | 0 | 0 | 0 | yes |
| tool-denied | single | denied | yes | 1 | 1 | 0 | 500 | 1000 | 2 | 0 | 0 | 0 | yes |
| tool-denied | multi | accepted | yes | 1 | 1 | 0 | 500 | 1000 | 5 | 0 | 0 | 0 | yes |
| budget | single | failed | yes | 1 | 4 | 4 | 2000 | 4000 | 9 | 0 | 0 | 0 | yes |
| budget | multi | budget_exhausted | yes | 1 | 4 | 4 | 2000 | 4000 | 11 | 0 | 0 | 0 | yes |
| open-ended | single | failed | yes | 1 | 9 | 9 | 4500 | 9000 | 16 | 0 | 0 | 0 | yes |
| open-ended | multi | accepted | yes | 1 | 9 | 9 | 4500 | 9000 | 19 | 0 | 0 | 0 | yes |
| unknown | single | recovery_required | yes | 1 | 3 | 2 | 1500 | 3000 | 5 | 0 | 0 | 0 | yes |
| unknown | multi | failed | yes | 1 | 3 | 2 | 1500 | 3000 | 7 | 0 | 0 | 0 | yes |
| full-strict | single | failed | yes | 1 | 6 | 6 | 3000 | 6000 | 12 | 0 | 0 | 0 | yes |
| full-strict | multi | accepted | yes | 1 | 6 | 6 | 3000 | 6000 | 14 | 0 | 0 | 0 | yes |
| tool-denied-recoverable | single | denied | no | 1 | 1 | 0 | 500 | 1000 | 2 | 0 | 0 | 0 | yes |
| tool-denied-recoverable | multi | accepted | yes | 2 | 10 | 9 | 5000 | 10000 | 27 | 1 | 0 | 0 | yes |
| open-ended-alt | single | failed | yes | 1 | 9 | 9 | 4500 | 9000 | 16 | 0 | 0 | 0 | yes |
| open-ended-alt | multi | accepted | yes | 1 | 9 | 9 | 4500 | 9000 | 19 | 0 | 0 | 0 | yes |
| full-reviewer-extra-evidence | single | succeeded | no | 1 | 1 | 1 | 500 | 1000 | 3 | 0 | 0 | 0 | yes |
| full-reviewer-extra-evidence | multi | rejected | yes | 2 | 2 | 2 | 1000 | 2000 | 9 | 0 | 2 | 0 | yes |
| full-noisy-plan | single | denied | no | 1 | 1 | 0 | 500 | 1000 | 2 | 0 | 0 | 0 | yes |
| full-noisy-plan | multi | accepted | yes | 2 | 10 | 9 | 5000 | 10000 | 26 | 1 | 0 | 0 | yes |

### 怎么读这张表

- **单 Agent 7/10，多 Agent 10/10**，但差距只来自三个任务：
  - `tool-denied-recoverable`：坏首步 → Reviewer replan → 成功；单 Agent 直接 denied。
  - `full-noisy-plan`：同上，另一个坏首步。
  - `full-reviewer-extra-evidence`：这是 **toy protocol**；单 Agent 没有 Reviewer，
    所以无法满足“预期拒绝”；这不是多 Agent 金融能力提升。
- 其余 7 个任务，单/多 Agent 的结果和成本相同；多 Agent 只多出毫秒级编排开销。
- 重规划任务的成本从 `500 µUSD / 1 call` 增加到 `5000 µUSD / 10 calls`，这是额外
  child run 的真实代价。
- `unknown` 两边的 child 都停在 `recovery_required`；多 Agent 的编排 `status` 记为
  `failed`。没有做跨 child 收据对账；产品里的 `run_scenario unknown` 会 reconcile，
  但不能把那条结果直接算到本 lab 的单一 child 基线上。
- `budget`/`full-strict`/`open-ended` 的“success=yes”是“预期失败/阻塞被正确接受”，
  不是成功率提升。

### 限制

- n=10，合成任务，**没有统计显著性**；
- Planner/Reviewer 是确定性规则，不是 LLM；
- child 在同一进程内，不是独立 subagent/容器；
- 不做跨 child 观测复用；重规划必须用不重复的工具/参数；
- manifest 只做 SHA-256，不做 HMAC/时间戳；
- child trace 的 `model.note` 仍是既有 mock 文案，角色信息只在 manifest；
- 所有结论不能外推到真实财报、真实模型或其他领域。

## 5. 下一步

1. 给 Planner/Reviewer 加 `Provider` 适配器，但保持“无工具”约束；
2. 设计显式的跨 child 观测复用/幂等键，才能让金融任务在 Reviewer 拒绝后不重复读；
3. 把 per-role budget 从 lab ledger 提升为 runtime 的 `budget_scope`；
4. 单链父任务 + `agent_role` 事件（可选，需核心改动）；
5. 更多 finance 任务和 Reviewer 规则，再谈统计意义。
