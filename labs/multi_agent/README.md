# FCode Multi-Agent Lab

这是一个**确定性角色编排实验**，不是生产级多 Agent，也不是多个 LLM 自主协作。
Planner / Reviewer 是规则实现，Executor 复用现有 `Loop` + `MockProvider`；每个角色
没有独立进程、没有独立模型、没有工具越权。

## 它验证什么

```text
Planner ──plan──▶ Executor ──child trace──▶ Reviewer
   │                  │                        │
   └────── manifest（SHA-256 prev_hash 链）────┘
```

- Planner 只产出 typed `Plan`，不调用工具。
- Executor 为每次尝试创建一个 child task，先写 `plan_steered`，再跑现有 `Loop`。
- Reviewer 只看序列化后的 `ChildResult`，没有 registry/harness，不写 `review_decided`。
- 重规划 = 新 child run；`denied` 是终态，不能在同一 task 内续跑。
- 每个 child 有自己的哈希链；manifest 记录父子关系、handoff、verdict 和链头。
- 预算按角色 ledger 预留；child 启动前做总预算硬检查；重复 `(tool, canonical(args))`
  在重规划中会被 `DUPLICATE_OBSERVATION` 拦住。

## 运行

```bash
cd flowcredit
python3.12 -m labs.multi_agent.cli --task full --mode multi
python3.12 -m labs.multi_agent.cli --task full --mode single
python3.12 -m labs.multi_agent.cli --ablate \
  --out labs/multi_agent/.runs/ablation.json
```

seam spike（只验证 `plan_steered` 注入是否可靠，不跑后续模块）：

```bash
cd flowcredit
PYTHONPATH=src python3.12 labs/multi_agent/seam_spike.py
```

输出目录 `.runs/` 已被忽略；每个 run 有 `manifest.jsonl`、`state.json`、
`children/<task>/trace.json`。所有数字都能从这些文件和命令输出复核。

测试：

```bash
cd flowcredit
python3.12 -m unittest discover -s tests -v
```

## 文件

| 文件 | 职责 |
| --- | --- |
| `state.py` | `MultiAgentState`、budget ledger、manifest 哈希链、原子写 |
| `handoff.py` | `Handoff` / `ReviewVerdict` schema 与白名单校验 |
| `roles.py` | 确定性 Planner / Reviewer |
| `executor.py` | child task、`plan_steered`、`Loop.run`、trace/budget 收集 |
| `orchestrator.py` | 控制流、重试、预算、duplicate/no-progress/resume |
| `fixtures.py` | 10 个消融任务 + 3 个 toy protocol 任务 |
| `cli.py` | 单任务与消融入口 |
| `seam_spike.py` | 证明 `plan_steered` 可靠的 pre-flight |

## 明确边界

- **不是 LLM 多 Agent**：MVP 的角色是确定性规则；Provider 适配器只留了接口。
- **不是跨进程 subagent**：所有角色在同一个 Python 进程里，child 只是独立 task/链。
- **不做跨 child 观测复用**：重规划必须使用不重复的工具/参数；否则直接停止。
- **T4/T5/T6 使用 toy protocol fixture**：金融 full 只有一个最终计算，严格禁止重复
  执行时无法靠“再跑一遍”完成 Reviewer 拒绝后的成功重试。toy fixture 只验证协议，
  不参与金融结论，也不计入“多 Agent 提升金融能力”的叙事。
- **T3 的第 5 个 `model_started`**：FCode 在 `charge_call` 之前写 `model_started`，
  所以第 5 次尝试会留下一个 start 事件；provider 没有被调用，预算 calls 仍是 4。
- **manifest 不是 HMAC**：只做 SHA-256 + prev_hash；签名收据仍在 child runtime 里。
- **child trace 的 provider note 仍是 mock 文案**：`TraceRecorder` 的既有字段没有改，
  多 Agent 角色信息在 manifest，不在 child chain。

## 当前结果

10 个任务、单 Agent vs 多 Agent，各跑 3 次取 wall-time 中位数：

- 单 Agent 7/10；多 Agent 10/10。
- 多 Agent 的增益集中在 `tool-denied-recoverable`、`full-noisy-plan`
  和 `full-reviewer-extra-evidence`；前两个是同一个“坏首步 → 重规划”能力，
  第三个是 toy protocol 的“Reviewer 两次拒绝”，不是金融能力提升。
- 正常任务的质量和成本相同；多 Agent 只多出毫秒级进程内编排开销。
- 重规划任务的成本从 500 µUSD/1 call 增加到 5000 µUSD/10 calls，这是额外
  child run 的真实成本。

完整表、限制和结论见 `flowcredit/docs/multi-agent-lab.md`。
