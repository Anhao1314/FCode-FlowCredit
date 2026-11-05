# Runtime 证据演示与录屏脚本

本脚本用于约 5 分钟录屏。所有展示数据必须由当次命令生成；故障注入是受控测试，不是线上事故。

## 准备

```bash
cd flowcredit
PYTHONPATH=src python -m flowagent.benchmark --output work/runtime-benchmark/demo
```

终端最后应显示 19 个任务、所有预置案例符合预期、`OFFLINE_SCRIPTED` 和 `ONLINE_MODEL: NOT_RUN`。打开 `work/runtime-benchmark/demo/summary.md` 作为目录。

## 镜头一：正常完整调用链（60 秒）

打开 `tasks/single-file-modify/events.json`，按顺序定位：

1. `task_created`：用户目标与主机授权已冻结；
2. `model_started` 与 `plan_validated`：模型只提出动作；
3. `execution_started` 与 `tool_started`：Schema、注册表、权限与预算检查后才 dispatch；
4. `tool_succeeded`：结果与哈希成为副作用收据；
5. `verification_started` 与 `task_succeeded`：Runtime 重读文件并独立复核。

再打开同目录 `verifier.json` 与 `workspace.diff`，说明 verifier 直接检查磁盘内容，而不是复述模型结论。

## 镜头二：审批拒绝（45 秒）

打开 `tasks/approval-denied-write/events.json`，定位 `approval_requested`、`approval_resolved(decision=denied)`、`tool_denied`。指出事件中没有 `tool_started`，`rejected.txt` 不存在；审批发生在工具执行前，不是事后解释。

## 镜头三：真实 SIGKILL 与未知结果（90 秒）

打开 `tasks/crash-after-side-effect/process.json`，展示实际 PID、`returncode=-9` 和受控故障标记。随后依次展示：

- `worker.stdout.txt`：崩溃检查点；
- `events.json`：`tool_started` 存在，但没有对应的 `tool_succeeded`；
- `final-files.json`：文件副作用已经发生；
- `recovery.json`：依据磁盘证据标记 `side_effect=observed`、`retry_dispatched=false`；
- `tool-executions.json`：`write_file=1`，没有重复副作用；
- `replay.json`：持久化状态与只读回放结构化一致；
- `verifier.json`：独立检查通过。

口播边界：“这里恢复的是对账和安全停止，不是假装知道外部结果，也不承诺回滚任意外部系统。”

## 镜头四：模型自报成功，verifier 拒绝（45 秒）

打开 `tasks/model-success-verifier-reject/verifier.json`。先指出 `model_claimed_done=true` 和 Runtime `succeeded`，再指出 `file:answer.txt` 的预期为 `release=verified`、实际为 `release=wrong`，最终 `passed=false`、`exit_code=1`。

这说明文件哈希验证只能证明写入真实发生，任务内容是否正确仍由独立 verifier 决定。该失败保留在总指标 `18/19` 中，不能删除。

## 收尾（30 秒）

回到 `summary.md`，展示各指标分子／分母和 `0` 越权副作用、`0` 重复副作用。明确说明：离线 scripted 结果验证 Runtime 治理行为，不代表真实模型任务成功率；本轮没有获授权运行付费在线评测，因此没有在线指标或真实模型 Badcase。
