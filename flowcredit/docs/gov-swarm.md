# 目标级蜂群（P2）

蜂群不是“多个 agent 互相聊天”，而是**目标级 fan-out**：宿主把多个目标排成队列，
按并发上限启动独立的审计 run。子 run 仍然写 `bot_runs`（`source=swarm`），所以
预算、频道、审计台、回归、签名链全部复用现有事实；蜂群只负责调度，不产生新的裁决。

## 数据模型

- `bot_swarms`：蜂群本身（名称、状态、并发、target.md/OSV 可选参数、时间）。
- `bot_runs.swarm_id`：每条子 run 归属一个蜂群。
- 子 run 仍是审计员 run：目标独立、失败独立、证据独立；一个目标失败不会污染其他目标。

限制：最多 20 个目标，并发 1–4；每个目标计入审计员每日预算，创建前先检查剩余额度。

## 生命周期

| 状态 | 含义 |
| --- | --- |
| `running` | 宿主正在按并发启动/等待子 run |
| `done` | 所有目标成功 |
| `partial` | 部分成功、部分失败 |
| `failed` | 全部失败 |
| `cancelled` | 操作员取消；排队目标标记 rejected，已开始的 run 跑完 |

- **取消**只停止排队，不 kill 已在执行的审计子进程。
- **重试**把 failed/rejected 的子 run 重新置为 queued，并重新按并发调度；已成功的
  目标不会重跑。
- 服务重启时，遗留的 queued/running 子 run 按现有语义标记失败；蜂群会收敛为
  `partial`/`failed`，不会永远显示“运行中”。

## API

- `POST /api/gov/swarms` `{targets:[...], max_concurrency?, name?, target_md?, osv_snapshot?}`
- `GET /api/gov/swarms?limit=`：蜂群列表，每条带 `counts` 与子 `runs`。
- `POST /api/gov/swarms/<id>/cancel`
- `POST /api/gov/swarms/<id>/retry`

## UI

团队页新增 **SWARM / 目标蜂群**：

- 多行目标 + 并发选择 + 发起；
- 蜂群卡显示进度条、完成/执行/排队/失败计数、每个目标的运行图；
- 完成目标可直接进工作台；频道里的子运行来源徽章为「蜂群」；
- 取消排队、重试失败目标都在卡内完成。

## 边界

- 当前只支持审计员（auditor）的蜂群；复核/分析不参与 fan-out。
- 预算是每日总量，不是每目标独立预算；后续可在 swarm 行上增加 per-target cap。
- 不做嵌套蜂群；不引入 agent 间对话或自由委派。
