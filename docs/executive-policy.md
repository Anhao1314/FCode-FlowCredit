# Executive–Policy（EPA）· P1 决策包

**状态：设计 + 部分落地** — EPA P1/P2 的选择协议；运行时的选择面在 [architecture.md](architecture.md)，训练与验收实测在 `../sft/cloud/README.md`（当前结论：packet 验收 1/2，暂不默认启用）。下一版框架见 [framework.md](framework.md)。最后核对：2026-10-26。

> Host 负责“想清楚”，4B 负责“选一下”，Ledger 负责“留下证据”。

## 问题

旧接口把完整计划、16 个工具和全部历史交给模型，让它自由生成 JSON。对 4B 来说，
这一轮同时要求理解长上下文、选工具、生成参数、规划下一步、保证格式合法。

P1 把模型输入改成一张 **DecisionPacket**：当前状态、最近一张证据卡、2–5 个合法动作。
模型只回答 `{"choice": "a1"}`，不生成 tool、不生成参数、不管理记忆。

## 运行链路

```text
plan + memory + budget
  → ActionCompiler
      → 0 个候选：Host 阻塞 / 问用户
      → 1 个候选：Host 直接执行（fast path，auto_action）
      → 2–5 个候选：DecisionPacket → Policy → ActionChoice
  → Host 校验 → gate → execute → verify
  → EvidenceCard 更新状态
```

## 关键契约

| 契约 | 内容 |
| --- | --- |
| `ActionCandidate` | id / todo_id / tool / arguments / why / effect / version |
| `DecisionPacket` | objective / round / state / last_evidence / memory_cards / budget / candidates / output |
| `ActionChoice` | candidate_id / reason |
| `PacketProvider` | 把 Policy 选择适配成现有 Provider/Proposal，复用 Harness 的状态机、预算、receipt |
| `auto_step` | 单候选时由 Host 直接提交，记录 `auto_action`，不扣模型预算 |

## Trace 事件

- `policy_requested`：完整 packet + `packet_hash`；
- `policy_decided`：选中的 candidate、packet hash；
- `auto_action`：无需模型调用，记录 candidate、step、packet hash。

这样“为什么调用/没调用模型”可以回放，且不记录隐藏 chain-of-thought。

## P1 实测

```sh
cd flowcredit
PYTHONPATH=src python3 -m flowagent.demo.packet \
  --workdir /tmp/fcode-packet-full --scenario full --fresh
```

| 场景 | 状态 | 轮次 | 模型调用 | fast path | packet | 旧上下文 | 降幅 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `full` | succeeded | 9 | 8 | 1 | 1,541 B | 7,624 B | **79.8%** |
| `open-ended` | failed（如实阻塞） | 9 | 9 | 0 | 1,541 B | 7,624 B | **79.8%** |

`full` 仍然是 `-250 bp`，链验证通过；最后一步 `calculate_margin` 只有一个合法动作，
由 Host 直接执行，所以模型调用从 9 降到 8。

## 当前边界

- `MockPolicy` 是 rule-first-candidate，只用于验证结构，不代表模型能力。
- 控制台默认仍走旧的 Provider 路径；packet 模式目前只在 lab 和测试里启用。
- 候选动作来自蓝图 todo，还不是模型生成的计划。
- SFT 仍是 v10 全上下文格式；P2 才会切到“DecisionPacket → choice”。
- 发生阻塞时 packet 会保留一个 `report_blocked` 候选；模型可以诚实停下。

## P2 已落地（lab）

### LocalPolicy

`providers/local_policy.py` 调用 OpenAI-compatible 本地端点，只接受 `{"choice": "aN"}`：

- `temperature=0`，`max_tokens=64`；
- 可选 `--json-mode`（`response_format={"type":"json_object"}`），
  真正的 grammar/guided decoding 由 vLLM/Outlines/llama.cpp 侧提供；
- 输出不是合法候选时，只做**一次**修复；第二次仍失败抛
  `POLICY_INVALID_CHOICE`，不会静默回退或编造动作；
- 修复调用的 tokens 计入 receipt；本地推理 cost 为 0。

运行：

```sh
PYTHONPATH=src python3 -m flowagent.demo.packet   --workdir /tmp/fcode-packet-local --scenario full --fresh   --policy local --endpoint http://127.0.0.1:11434/v1 --model qwen3-4b-fcode
```

### packet prompt

`providers/packet_prompt.py`：版本 `fcode.packet-prompt/v2`。
模型只看到 packet 和输出 schema，不再看到完整工具目录；训练和推理共用同一渲染器。
v2 增加依赖顺序规则：先读取、统一口径、核对重述或解除依赖，再计算；blocked
时优先选择能解除 blocker 的候选，只有无法解除才 `report_blocked`。
2026-10-24 之前生成的 `sft/data/choices_*` 仍是 v1 样本；本地 policy head
恢复训练前必须用 v2 renderer 重新生成，不能混用。

### choice SFT

`flowagent.sft.choices` 从 packet 轨迹生成训练样本：

```sh
PYTHONPATH=src python3 -m flowagent.sft.choices   --traces /tmp/fcode-packet-full/trace.json   --out sft/data/choices_full.jsonl   --stats-out sft/data/choices_stats.json
```

一条样本就是：

```json
{"conversations": [
  {"from": "system", "value": "..."},
  {"from": "user", "value": "{\"packet\": ...}"},
  {"from": "assistant", "value": "{\"choice\": \"a1\"}"}
]}
```

`policy_requested` 提供 packet，`policy_decided` 提供标签；`auto_action` 轮次没有
模型标签，不进入训练集。

### 评测平面

`sft/evaluate.py` 把 packet lab 变成可重复报告：

```sh
PYTHONPATH=src python3 -m flowagent.sft.evaluate \
  --workdir /tmp/fcode-eval-mock --scenarios full,open-ended --policy mock
```

MockPolicy 基线：

| 指标 | 值 |
| --- | --- |
| 场景 / 结果正确 | 2 / 2 |
| 模型调用 | 17 |
| policy 决策 | 17 |
| fast path | 1 |
| 修复次数 | 0 |
| 平均 packet | 1,590 B |
| 旧上下文 | 7,624 B |
| 平均缩减 | **79.1%** |
| 链验证 | 全部通过 |

接本地模型时把 `--policy mock` 换成 `--policy local --endpoint ... --model ...`，
报告会多出 `policy_tokens`、`policy_latency_ms`、`repaired`，并记录失败原因。

### 当前边界

- 还没有在真实 Qwen3-4B 端点上跑过；测试使用 loopback OpenAI mock。
- `LocalPolicy` 的“约束”目前是 prompt + JSON mode + 一次修复；严格 grammar
  需要服务端 guided decoding。
- 控制台默认仍走旧 Provider；packet 模式尚未成为默认运行路径。
- 旧 SFT（v10 全上下文）仍在；新 choice 数据集是独立格式，需要重新训练/评测。
- 还没有完成动作正确率、blocked 精确率、引用覆盖率的冻结评测集。

## 模型到达后的验收

```bash
ollama create fcode-qwen3-4b -f Modelfile
cd flowcredit
PYTHONPATH=src python3 -m flowagent.sft.accept \
  --workdir /tmp/fcode-accept \
  --endpoint http://127.0.0.1:11434/v1 \
  --model fcode-qwen3-4b
```

验收看运行时，不看 loss：`full` 必须 `succeeded/-250bp`，`open-ended` 必须
`failed/INSUFFICIENT_EVIDENCE`，没有 error、链验证通过；修复次数会作为 warning
列出，不静默忽略。

## 下一步

1. 用真实 Qwen3-4B 端点跑 packet lab，记录合法率、动作正确率、延迟；
2. 冻结 20–30 个任务，比较 MockPolicy / LocalPolicy / oracle；
3. 把 packet 模式接进控制台，旧 Provider 保留为故障注入与基线；
4. 再考虑 strict grammar（vLLM guided_json / Outlines）与更多领域。
