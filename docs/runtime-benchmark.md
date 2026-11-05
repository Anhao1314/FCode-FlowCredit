# Runtime 真实任务基准

本基准回答的是“正式 Runtime 的治理与恢复是否真实生效”，不是“离线脚本能否冒充模型能力”。任务复用 `Loop`、`Harness`、封闭工具注册表、Schema／权限／预算／审批门控、SQLite 事件存储和恢复链路。`ScriptedProvider` 只提供动作提议，不能直接执行工具。

## 运行

```bash
cd flowcredit
PYTHONPATH=src python -m flowagent.benchmark --list
PYTHONPATH=src python -m flowagent.benchmark --output work/runtime-benchmark/latest
python -m unittest tests.test_runtime_benchmark -v
```

默认输出为 `work/runtime-benchmark/latest`。仓库内提交的复核副本位于 `artifacts/runtime-benchmark/latest`。每次生成会替换目标目录；任务之间使用独立工作区、canary 和 SQLite 数据库，离线网络 guard 会记录并拒绝应用层联网尝试。

## 任务集与变更纪律

当前任务集为 `runtime-benchmark/v1`，共 **19 项**。完整定义、逐项 SHA256 和任务集 SHA256 见运行产物中的 `taskset.json` 与各任务 `task.json`。任何任务定义变化都必须在 `benchmark/tasks.py::TASK_CHANGELOG` 留下原因，并重新生成全部受影响产物；不得通过删除困难任务、放宽 verifier 或改写错误交付来提高通过率。

| 类别 | 数量 | 覆盖 |
| --- | ---: | --- |
| 文件与关联修改 | 4 | 单文件、多文件、脚本报错修复、中间状态依赖 |
| 权限与协议拒绝 | 4 | 非法 Schema、未知工具、越权路径、未授权网络工具 |
| 人工审批 | 2 | 批准、拒绝 |
| 预算 | 3 | 调用、Token、工具次数 |
| 真实崩溃恢复 | 3 | 执行前、副作用后未知、提交后恢复续跑 |
| 未知结果 | 1 | 副作用后显式 unknown，禁止重试 |
| 审计完整性 | 1 | 修改 SQLite 历史事件并定位篡改 |
| 独立 verifier | 1 | 模型自报成功但文件内容错误 |

每项任务包含版本、fixture、用户目标、模型输入、工具与权限、预算／超时／审批、预期状态、禁止副作用、治理事件、独立 verifier、存在理由和离线／在线适用范围。

## 四种状态不能混用

- `pipeline_completed`：Runtime 到达终态，只说明流程结束。
- `verifier_passed`：独立 verifier 对文件、命令结果、diff、事件、收据、canary 和恢复证据的检查通过。
- `policy_violation_blocked`：预置违规被阻止，且没有越权副作用。
- `recovered`：真实崩溃或 unknown 后按预期对账／重建；不等于原任务目标已经成功。

负面任务的成功标准是治理符合预置预期，不是强迫 Agent 完成原目标。`model-success-verifier-reject` 的 Runtime 状态可以是 `succeeded`（真实写入且哈希匹配），但任务 verifier 必须因内容错误返回 `passed=false`、`exit_code=1`。该失败保留在独立 verifier 通过率分母中；“符合预置负面预期”另记为 `case_expectation_met`。

## 真实 SIGKILL 与恢复边界

崩溃任务在独立进程中运行正式 `Loop`，于明确检查点发送 `SIGKILL`：

1. `tool_started` 已提交、工具 handler 未执行；
2. 文件副作用已发生、结果尚未提交；
3. `tool_succeeded` 已提交、计划尚未更新。

父进程保存 PID、返回码、stdout／stderr 和 handler 次数。前两种情况由 `runtime/recovery.py` 找到未闭合调用，依据工作区哈希记录 `recovery_reconciled`，保持结果未知并且不重发。第三种通过只读回放重建计划，再执行不同的下一动作；第一次写入不会重复。

回放只接收事件字典，不接收 registry、world 或 provider。它比较动作顺序、结果哈希、计划和终态；预算计数仍在 SQLite 中，恢复前后单独比较，文档不宣称预算可从每个事件单独重算。

当前恢复边界是应用层工作区、事件与已有收据的对账。它不是操作系统级沙箱，不能回滚任意外部系统，也不证明所有并发写入或外部副作用都可恢复。

## 产物

每次运行保存：

```text
run_manifest.json       被测 commit、生成前 dirty、环境、Provider、任务集哈希
summary.json/.md        指标、分子分母、逐任务结果与统计边界
taskset.json            版本化任务与变更记录
tasks/<task-id>/
  task.json             任务定义与哈希
  task.sqlite           可定位的原始事件数据库
  events.json           完整事件流（篡改案例保留篡改前导出）
  receipts.json         工具调用、结果哈希与恢复收据
  initial-files.json    初始工作区快照
  final-files.json      最终工作区快照
  workspace.diff        实际前后 diff
  verifier.json         独立检查项、预期/实际、失败原因、退出码
  stdout.txt/stderr.txt 运行输出
  provider.json         OFFLINE_SCRIPTED 配置摘要
  policy.json           权限、预算、审批与超时
  recovery.json         对账与恢复结果
  process.json          SIGKILL PID、信号和返回码
  replay.json           结构化状态等价比较
```

关键案例：

- 正常完整链：`artifacts/runtime-benchmark/latest/tasks/single-file-modify/`
- 副作用后未知：`artifacts/runtime-benchmark/latest/tasks/crash-after-side-effect/`
- 提交后恢复：`artifacts/runtime-benchmark/latest/tasks/crash-state-replay-resume/`
- verifier 拒绝：`artifacts/runtime-benchmark/latest/tasks/model-success-verifier-reject/`

## 指标与统计边界

| 指标 | 分母 |
| --- | --- |
| `pipeline_completion_rate` | 全部任务 |
| `independent_verifier_pass_rate` | 全部任务；预置 verifier 失败仍计入 |
| `policy_block_accuracy` | 仅预置违规任务 |
| `approval_gate_accuracy` | 仅批准／拒绝任务 |
| `budget_enforcement_accuracy` | 仅预算任务 |
| `crash_recovery_rate` | 仅真实 SIGKILL 任务 |
| `unknown_outcome_safe_handling_rate` | 仅未知工具结果任务 |
| `replay_equivalence_rate` | 非篡改链任务 |
| `hash_chain_tamper_detection_rate` | 仅篡改任务 |

`unauthorized_side_effect_count` 与 `duplicate_side_effect_count` 是绝对计数。没有有效数据时使用 `NOT_MEASURED`，不把空分母写成 100%。

## 基线与当前证据

首次在 `34aa41d` 上运行现有命令，实际基线为 **651 项 Python 测试、166 项前端测试、8 个端到端场景**；当时根 README 的 `642 / 71` 与 Runtime README 的 `650 / 166` 均不是本次实测值。基线 Python 与前端测试全部通过，8 个场景轨迹全部通过自检。

加入本基准后最终数字以当次 `summary.md` 和完整测试输出为准。当前任务基准的真实结果是：pipeline `19/19`、独立 verifier `18/19`、policy `4/4`、approval `2/2`、budget `3/3`、crash recovery `3/3`、unknown safe handling `3/3`、replay `18/18`、tamper `1/1`、越权副作用 `0`、重复副作用 `0`。

## ONLINE_MODEL 边界

本次为 `OFFLINE_SCRIPTED`。环境中虽检测到 Kimi 凭据变量存在，但未获得本轮付费在线评测的明确授权，因此 `ONLINE_MODEL=NOT_RUN`；真实模型通过率、Token、成本、延迟、波动和 Badcase 均为 `NOT_MEASURED`，没有生成任何在线数字。离线高通过率不能解释为真实模型任务能力。
