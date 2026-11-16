# Memory Policy Head（Sprint A：shadow）

> 给记忆折叠加一个**只做选择、不写内容**的决策层。第一刀只记录决策，不改变
> 行为；转正需要 shadow 数据背书。

## 为什么是「策略层」而不是「记忆模型」

本项目的记忆是证据驱动的：事实是确定性抽取、案例是签名 trace 的确定性投影、
操作员记忆只读操作者输入。让模型直接写记忆会破坏可重建性。

因此决策头的定位被收窄为：**在既有、不可变的观测记录上做闭集选择**。

- 输出只能是 `pin / keep / drop`，不产文本、不产事实、不产指令；
- `drop` 只表示「降为一行索引」，store 里证据永不删除；
- 未知 id 直接拒绝，不做模糊匹配；
- 决策本身作为 `memory_decision` 事件进哈希链，重放时应用记录值而不是重调模型。

外部依据：Jev-Mem 的 consolidation 同样坚持非破坏性（保留原始观测）；社区实测
也表明句级「压缩/删除」会破坏上下文——所以这里把 drop 设计成降级而非删除。

## 数据流

```text
第 N 轮 tool_succeeded → memory.record()
        │
        ├─ 触发：每 K 轮（interval）或观测占用 ≥ 阈值（pressure）
        ▼
memory.policy_rows()  ── 观测索引 + 摘要头（不含原始工具输出）
memory.observation_stats() ── 观测预算
plan 未完成 todos
        │
        ▼
policy head（廉价辅助调用，独立 max_tokens）
        │
        ▼
parse_policy_reply(ids 白名单) → compare_heuristic(当前折叠意图)
        │
        ▼
memory_decision 事件（决策 + 分歧 + 成本）
        │
        └─ Sprint A：build() 不读它，prompt 字节不变
```

## 契约

```json
{
  "schema": "flowagent.memory-policy/v1",
  "observations": [
    {
      "id": "a1b2c3d4e5f6",
      "action": "pin",
      "confidence": 0.82,
      "reason": "needs_for_next_step"
    }
  ]
}
```

- `id`：观测 `result_hash` 的前 12 位，与 prompt 里的索引行一致；
- `action`：`pin | keep | drop`；
- `reason`：`needs_for_next_step | superseded | duplicate | low_signal`；
- 置信度以 basis points 入链（`confidence_bp`），因为 `canonical_json` 拒绝浮点。

校验规则：未知 id / 重复 id / 非法枚举 / 越界置信度都计为无效；无效条目超过
30%（且至少 3 条）时整条决策作废（`invalid_ratio`），不部分信任。超出
`max_entries` 的条目丢弃并计数，不污染已考虑条目的判断。

## 配置

```json
{
  "spec": {
    "memory_policy": {
      "mode": "shadow",
      "every_rounds": 3,
      "max_calls": 12,
      "max_entries": 40,
      "max_tokens": 8192,
      "effort": "none",
      "trigger": "interval"
    }
  }
}
```

- 缺省 `off`；也可用环境变量 `FCODE_MEMORY_POLICY=shadow` 作为兜底。
- 仅 `general` / `general-fast` / `general-fast-native` 可启用；finance 默认
  profile 与 SFT 冻结路径强制关闭。
- Sprint A 只实现 shadow：spec 写 `active` 会被记录为 `requested_mode`，实际仍按
  shadow 执行，行为不变。
- `max_tokens` 默认 8192、`effort` 默认 `none`：实测 Kimi 的推理通道会吃掉
  800/2048 的补全预算并返回空 content；有界分类不需要思维链，覆盖值只在
  provider 本身配置了 reasoning 时才发送。
- 决策调用独立限流（`max_calls`、`max_tokens`），成本如实入事件但不占任务工具预算。

## 决策质量评测（真实 provider）

`scripts/eval_memory_policy.py` 用 3 个带 ground truth 的场景直接调用真实
policy head（不经过完整 loop），分别标注「剩余步骤需要的观测」和「被后续观测
取代的历史版本」：

```bash
python3 scripts/eval_memory_policy.py --provider kimi
```

2026-11-11 首次实测（Kimi k2.6，effort=none，3 次调用 / 2,929 tokens / 4,942 µUSD）：

| 指标 | 结果 |
| --- | --- |
| live-needed 保留率 | **9/9**（剩余步骤真正需要的观测全部保留） |
| superseded 清理率 | 4/5（历史版本 4 条被正确降级） |
| irrelevant 清理率 | 6/7（无关观测 6 条被降级） |
| 严格口径 needed 保留 | 10/14（差额 4 条均为被后续观测取代的历史版本） |

含义：在这个小样本上，决策头没有丢过「后续还需要」的观测；它把「被取代的
历史状态」识别为可降级，这正是 FIFO 启发式做不到的部分。样本量小（3 场景、
20 条观测、单模型单次），只能作为方向性证据，不能当作转正依据。

## 真实运行验证（2026-11-11，Qwen3.8-Max）

浮点边界修复后，用真实 provider 跑了两条 general 任务（`approval=off`，
shadow 开启）：

| run | 轮数 | tokens | 成本 | 真实缓存命中 | 平均前缀 | 终态 |
| --- | --- | --- | --- | --- | --- | --- |
| `…-074824-6056` | 9 | 55,317 | $0.1332 | **69.65%** | 76.82% | BUDGET_EXCEEDED（8/8） |
| `…-075837-12e9` | 6 | 44,728 | $0.1258 | **80.54%** | 75.85% | 模型自报完成，plan 未完 → 拒绝判成功 |

结论：

- **浮点边界修复有效**：15 次真实模型调用、11 条 `memory_decision` 事件全部
  正常入链，没有一次 `VIEWER_RUNNER_ERROR`。
- **缓存命中率**：69.65% / 80.54%（历史 Kimi 样本为 9.34%）。这两个数同时受
  provider 自带 Context Cache 和 cache-friendly 排序影响，不能全部归因于排序；
  但 76% 的前缀相似度是直接可观测的输入形状。
- **shadow 决策**：11 次决策、42 条对比；`decision_right` 12 / `decision_wrong`
  3 / `decision_conservative` 12。真实数据比合成评测更保守，3 次误降也说明
  转正不能急。
- **成本问题**：决策头复用主模型时，11 次调用花掉 74,454 µUSD，约占两条 run
  总成本（$0.259）的 **29%**。转正前必须给决策头配独立廉价模型。
- **触发问题**：两条 run 的观测都没到折叠预算（baseline `folded_then_reread`
  为 0/0），启发式根本不会折叠，但决策头仍按每 1–2 轮触发。active 模式必须
  改为压力触发，否则是纯开销。

## Shadow 等价性

`WorkingMemory.build()` 不读取任何决策。测试
`test_shadow_decision_is_recorded_and_context_is_unchanged` 用同一组观测构造两份
memory，一份经过决策头、一份不经过，断言 `build()` 产物 canonical JSON 逐字节相等。
这正是 Sprint 2 Jev 验收要求里的「移除 Jev 后结果不变」模式。

## 分歧报告

```bash
python3 scripts/memory_shadow_report.py --run work/runs/run-xxx
```

报告把决策与当时的确定性折叠意图逐条对比，并用**决策之后的同工具同参数调用**
作为「是否真的需要」的证据：

| 决策 | 启发式 | 后续重读 | verdict |
| --- | --- | --- | --- |
| keep/pin | folded | 是 | `decision_right` |
| keep/pin | folded | 否 | `decision_conservative` |
| drop | inline | 是 | `decision_wrong` |
| drop | inline | 否 | `decision_right` |

报告的 baseline 段不涉及模型：它统计「启发式已经折叠、之后又被重读」的观测
占比。这正是 [loop.py](../src/flowagent/runtime/loop.py) 注释里记录的重读痛点，
现在有了可量化基线。

**置信度目前只记录、不当权重**：在 shadow 数据证明校准度之前，它没有资格影响
任何排序或阈值（Kimi 评审原则：类型化输出 + 校准置信度 ≠ 正确分诊）。

## 转正门槛（预注册，先于数据）

Sprint B 允许决策生效前，必须同时满足：

1. `decision_right` 数量显著高于 `decision_wrong`；
2. 置信度分桶后的准确率单调（高置信桶不能比低置信桶更差）；
3. 决策成本 < 被消除的重读轮成本；
4. 同一批场景上，启用决策后的验收通过率不低于启发式基线。

任一条不满足，保持 shadow。

## 事件与回放

- 事件类型：`memory_decision`，payload 包含 `round / trigger / mode / decision /
decision_sha256 / comparison / cost`。
- `Loop.restore()` 重放该事件以恢复调用次数与最后决策轮次，保证 resume 后不会
  超出 `max_calls` 或同一轮重复调用；Sprint A 不回放决策内容（因为不生效）。
- 失败（传输错误、坏 JSON、不支持的 provider）记 `fallback` / `skipped`，主循环
  无感继续。

## Sprint B 待办

1. 依据 shadow 报告决定哪类决策先转正（独立开关）。
2. `active` 模式把决策映射到折叠游标，保持观测顺序不重排（缓存前缀稳定）。
3. 可选的注入/检索决策（Jev 社区证据里体感最好的用法）。