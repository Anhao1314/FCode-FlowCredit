# 审计员受控 triage（shadow / active）

模型只提议**跟进优先级**，不做裁决、不改扫描覆盖。这是 `flowaudit` 里
`triage_model: jev-shadow (not wired)` 那次占位的落地方式：triage 的产物只落在
`bot_runs`，不进审计包、不进哈希链。

## 边界（由测试钉死）

- 输入是确定性审计包的**有界事实**：findings（最多 40 条）、uncovered、计数；
  上下文按不可信数据处理——其中的指令式文字不是指令。
- 输出只能是优先级提议：每条必须引用真实 `finding_id`；唯一可执行动作是
  `reaudit`（同一 target 的**完整**确定性复扫）。不得建议改规则、放宽门控、
  跳过检查。
- 模型缺位 / 输出非法 / provider 失败全部 **fail-closed**：审计照常完成，
  该运行的 triage 标记 `error`，证据不受任何影响。

## 三档

| 档位 | 行为 |
| --- | --- |
| `off`（默认） | 不调用模型，与旧行为一致 |
| `shadow` | 审计完成后调用一次模型，计划只落 `bot_runs.triage_json`，供操作员对照；不执行任何动作 |
| `active` | 同上；若计划非空，额外生成**一条待操作员批准**的完整复扫运行（`parent_run_id` 指回源运行）。批准后才执行；拒绝则不计预算 |

active 的复扫运行由现有审批机制接管（待批准 → 批准/拒绝），triage 自己发起的
复扫不会再触发 triage，避免递归。

## 产物与 UI

- `bot_runs`：`triage_mode / triage_status(pending|done|error) / triage_json /
  triage_error / triage_run_id`；子运行用 `parent_run_id` 关联。
- 团队频道运行卡：`TRIAGE` 块显示档位、置信、关注类别、优先级列表（finding_id +
  理由 + 动作）、模型/ tokens / µUSD，以及（active 时）待批准复扫编号。
- Bot 详情设置（仅审计员）：跟进策略三档选择，保存即生效。

## 与 `flowaudit --analyst` 的区别

`--analyst` 是 CLI 内部的跟进计划，读环境变量里的 DeepSeek 配置、带 wallet
guard，产物写进审计目录与 bundle toolchain。Bot triage 走**共享 provider 目录**
（设置页模型、`FCODE_DEFAULT_RUN_MODEL` 优先级），产物只进服务端运行日志，
不触碰审计包；两者互不替代。

## 验证

```bash
python3 -m unittest tests.test_gov_triage -v
node --test viewer/tests/gov-bots.test.js
```

真实模型冒烟会产生一次付费调用，需操作员明确同意；测试全部使用 fake provider。
