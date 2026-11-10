# Prompt 资产目录

> 单一清单：每个 profile 的版本、消费者、冻结状态和关键契约。改 prompt 前先看这里；
> 改完必须同步 `tests/test_prompt_snapshots.py` 的版本/字节快照并在下方 changelog 记一笔。

## Profiles

| key | version | system prompt | 消费者 | frozen | 关键契约 |
| --- | --- | --- | --- | --- | --- |
| `finance` | `fcode.prompt/v10` | `providers/prompt.py::SYSTEM_PROMPT` | `FINANCE_DOMAIN` | **是**（SFT 金色哈希） | 单动作 JSON；数字只能来自观测；`report_blocked` 说明原因 |
| `general` | `fcode.prompt/general-v9` | `providers/prompt.py::GENERAL_SYSTEM_PROMPT` | `GENERAL_DOMAIN` | 否 | 单动作 JSON；additions 逐项校验、部分接受；恢复/证据规则分开 |
| `general-fast` | `fcode.prompt/general-fast-v1` | `providers/prompt.py::GENERAL_FAST_SYSTEM_PROMPT` | `spec.fast` 的 JSON 链路 | 否 | 一轮 1–4 动作；读可批量；写/terminal 单独；plan-first；路径纪律 |
| `general-fast-native` | `fcode.prompt/general-fast-native-v1` | `providers/prompt.py::GENERAL_FAST_NATIVE_SYSTEM_PROMPT` | `spec.fast` 的默认原生 tools 链路 | 否 | 原生 tool calling；`submit_plan`；批量/写规则同上 |
| `chat` | 无独立 profile（inline） | `chat.py::CHAT_SYSTEM_PROMPT` | `ChatService` | 否 | 只读工具直接执行；写/网络逐次审批；完整任务用执行提案转委托 |
| `audit-analyst` | 无独立 profile（inline） | `flowaudit/analyst.py` 的 objective | `flowaudit.analyst` | 否 | 只依据审计 bundle 给跟进计划；不编造未给出的信息 |

## 版本与冻结纪律

1. **frozen profile 不原地改字节**：finance v10 的 system prompt、user 渲染和版本串受 SFT 金色哈希约束；行为要变就新增 profile，不改旧字节。
2. **非 frozen profile 改文本必须 bump version**：例如 `general-v9 → general-v10`；同时更新 `tests/test_prompt_snapshots.py` 的 version 与 SHA-256。
3. **prompt 快照是评审材料**：改动的 diff 必须能回答“为什么改、影响哪些 profile、是否更新了 golden 常量”。
4. **默认 profile 是 finance**：没有任何 selector 的旧上下文/SFT 样本继续按 finance v10 渲染；不要改这个兼容策略。

## Changelog

- **2026-11-10 · general-v9**：删除过期的“不要使用 additions / 计划扩展是致命的”段；运行时已支持逐项校验、部分接受和带原因退回，prompt 改为描述真实语义。
- **2026-11-10 · chat**：`CHAT_SYSTEM_PROMPT` 与 `chat_tools.py` 对齐——写文件/运行命令/抓取网页是逐次审批后执行，不再声称“不能写”；完整多步任务仍走执行提案转委托。`docs/chat.md` 同步。

## 已知欠账（按优先级）

- **P0**：所有 profile 的快照测试（本目录配套 `tests/test_prompt_snapshots.py`）。
- **P1**：prompt 级 eval harness（真实模型、版本 A/B、成功率/轮数/tokens/费用/违规率）。
- **P1**：注入对抗集（恶意 README、工具输出伪装 system message、网页指令）。
- **P2**：按 provider 的语言变体（中文 system prompt vs 英文 tool descriptions 的 A/B）。
- **P2**：运行时事件记录 `prompt_profile` / `prompt_version` / `system_prompt_sha256`，让 trace 能直接回答“这轮用的哪版 prompt”。
- **P2**：多 Agent 角色 prompt（AgentTask/AgentResult、verifier）产品化。