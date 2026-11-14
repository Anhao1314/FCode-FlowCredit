# 治理运行流（P1）

治理区只有一个运行对象：`bot_runs`。对话 `/挖洞`、团队派活、定时排程都写同一张表，
右栏「团队频道」是唯一时间线；团队页只负责常驻配置。这样同一个目标无论从哪个入口
发起，都能在同一个地方看到、追踪和复核。

## 来源与语义

| 来源 `source` | 入口 | 预算 | 审批 | triage |
| --- | --- | --- | --- | --- |
| `console` | 治理台 `/挖洞` | 不计 Bot 预算 | 立即执行 | 不调用（避免隐式模型调用） |
| `team` | 团队派活 | 计每日上限 | 手动派活不审批；排程按 `routine_approval` | 按审计员设置 |
| `team` + `trigger=routine` | 定时排程 | 计每日上限 | 可生成待批准运行 | 按审计员设置 |

- 手动运行也持久化：刷新、重启后仍在频道与运行记录里（重启会把未完成的
  `queued/running` 标记为失败，与团队运行同一语义）。
- `source=console` 的运行不进入 `_runs_today`，所以 Bot 详情的“今日已用”不会
  把操作员的即时调查算进常驻预算。
- 手动运行不触发 triage；triage 只服务常驻审计员的跟进提议。
- 复核员目标留空 = **最近一次完成的审计员运行**（以运行流为准，不再按目录名
  猜同一天的“最新”）。

## 角色

| 角色 | 目标 | 权限 | 产物 |
| --- | --- | --- | --- |
| 审计员 | 仓库内源码或放行的制品 | `audit.run` · `bundle.read` | findings + 证据 + 审计链 |
| 复核员 | 审计 id（留空 = 最近一次） | `chain.read` · `regression.read` · `bundle.read` | 链完整性 + 回归 buckets |
| 分析员 | 审计 id（留空 = 最近一次） | `bundle.read` · `triage.propose` | 优先级提议（`triage_json`），不产生 finding |

分析员是只读身份：使用同一套 `GovTriage`，`mode=manual`，只提议不裁决；不创建
技能、不拥有排程、不生成复扫。模型不可用时运行失败并显示 `triage_error`，
审计证据不受影响。

## 接口

- `POST /api/gov/runs`：统一创建入口。
  - `{target}` → 手动运行（source=console）；
  - `{bot_id, target, source:"team"}` → 团队运行（预算/审批语义不变）；
  - 可选 `target_md` / `osv_snapshot`：仓库内的描述文件与 OSV 快照，服务端校验
    路径不得逃出仓库；非法路径返回 400。
  - 目标校验同步完成，非法目标返回 400，不留下失败运行。
- `GET /api/gov/runs?limit=`：运行流（含手动/团队/定时），新的在前。
- `POST /api/gov/bots/<id>/run`：旧入口保留兼容，行为与统一入口的团队分支一致。

## 深度审计参数

- `target_md`：已安装制品的 `target.md`（授权、范围、Declared features）。带上它以后，
  已声明的 entitlement 不再算越权；例如 EvoX 的麦克风/日历/Apple Events。
- `osv_snapshot`：离线 OSV JSON（`{"vulns":[...]}`）。flowaudit 只匹配 Go module
  依赖；模块级命中可能包含未实际链接的包，需要操作员复核。
- 对话用法：`/挖洞 /Applications/EvoX.app --target-md fixtures/evox/target.md --osv-snapshot work/audit/evox-osv.json`
- 团队派活表单同样提供两个可选输入。

## UI

- 治理台空态展示**最近运行**（来源、目标、状态、发现数），点行直接定位到频道同一张卡；
  `/挖洞` 作业卡带「对话」来源、`查看结果 →`（工作台）、`频道 →`（同卡定位）。
- 团队频道：待批准置顶（新的在前），其余按时间正序；每张卡带来源徽章
  （对话 / 团队 / 定时），完成态保留工作台与审计台出口。
- 发现卡 / 链卡的「频道 →」按 audit id 找到对应运行并定位；判断卡的「频道 →」
  打开统一运行流。定位锚点是 `data-gov-run-card`，不依赖列表顺序。
