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

## 情报知识库 v0（OSV 同步 + 出处快照）

网络纪律：审计离线（PoC 证据声明 network: none）；情报同步是**操作员显式
动作**，产出带出处的快照落盘，审计与探员离线读取：

- `POST /api/intel/osv/sync {audit_dir}`：读包内 REA facts 的 Go 依赖 →
  查 api.osv.dev → 快照落盘 `work/intel/osv/osv-<audit_dir>.json`
  （schema `fcode.intel-osv-snapshot/v1`：source / fetched_at / queries /
  完整 vuln records）；重同步整包替换；
- 快照与手动 `--osv-snapshot` 完全兼容（AGI-05-103 离线匹配）；
- matcher 已泛化生态：`match_dependencies(deps, records, ecosystem="go"|"npm")`，
  npm 依赖提取在后续切片接入；
- 语义候选生成器（探员）的上下文注入命中的情报记录——已知漏洞匹配靠数据，
  不靠模型记忆（EvoX 盲测实证：无情报漏报 Wails CVE，注入后命中）。

## 探员候选（语义候选生成器 · shadow）

模型挖、管线判：探员读有界证据切片，产出结构化候选假设；候选落审计链，
**永远晋升不了**（没有 replay receipt，双闸门拒绝）：

- `POST /api/gov/audit/<dir>/hunt {threadId?}`：构建有界上下文（宿主事实 /
  Go 依赖图 / 文件树摘要 / 已同步的 OSV 情报；**findings 永不在输入里**）→
  探员调用（预算显式：48k 补全顶 + low effort）→ 候选以 `candidate_proposed`
  落 session 链（actor=`model-shadow`，source=`semantic-hunter`）→ 摘要消息
  进治理对话（卡片 kind=hunt，标注 shadow · 未晋升 · 不进审计包）；
- 落链走 `AuditService.record_model_candidates`（zone isolation：HTTP 层
  不直接 import flowaudit.session）；audit-bundle.json 逐字节不变；
- 入口：工作台「探员候选 → 对话」；mock/模型缺位 fail-closed；
- 实测（EvoX）：10 条候选 C-0006…C-0015 落链（链 21 事件完整），bundle
  mtime/尺寸/发现数均未变。

## 深度审计参数

- `target_md`：已安装制品的 `target.md`（授权、范围、Declared features）。带上它以后，
  已声明的 entitlement 不再算越权；例如 EvoX 的麦克风/日历/Apple Events。
- `osv_snapshot`：离线 OSV JSON（`{"vulns":[...]}`）。flowaudit 只匹配 Go module
  依赖；模块级命中可能包含未实际链接的包，需要操作员复核。
- 对话用法：`/挖洞 /Applications/EvoX.app --target-md fixtures/evox/target.md --osv-snapshot work/audit/evox-osv.json`
- 团队派活表单同样提供两个可选输入。

## UI

- Governance 默认落**治理对话**：正常模型对话，顶层只有「对话 / 审计库」两个入口；
  团队 roster、当前工作与 pending 待办在右侧团队栏，工作台/角色详情是对话下钻。
- 治理对话线程持久化（`governance-chat.sqlite`），刷新后保留消息、提案和 run 锚点。
  模型可以解释治理快照、追问意图，但执行必须经 `gov_action` 提案卡由操作员确认。
- 团队页先展示**待办**：所有 pending 运行按时间倒序，前 3 条直接批准/拒绝，
  其余折叠；待办是 pending 的第一入口，团队频道仍保留完整时间线。
- 团队频道：待批准置顶（新的在前），其余按时间正序；每张卡带来源徽章
  （对话 / 团队 / 定时），完成态保留工作台与审计库出口。
- 审计记录行 → 对话下的审计工作台（发现/处置/修复）；工作台 header 提供
  「← 对话」与「审计库 →」出口。审计库仍是独立 `#/audit/<dir>` 深链。
- 发现卡 / 链卡的「频道 →」按 audit id 找到对应运行并定位；判断卡的「频道 →」
  打开统一运行流。定位锚点是 `data-gov-run-card`，不依赖列表顺序。

## 解读进对话（账本 × 解读层）

审计产出保持双轨：账本永远是**纯证据面**（finding_id / 指纹 / oracle / 负对照，
不渲染任何解读 UI）；人话输出进**治理对话**：

- 出口在审计工作台的发现详情卡：「AI 解读 → 对话」按钮（`data-gov-explain-finding`）；
- 生成走解释层（不裁决、不引入证据外事实、bundle 逐字节不变、模型缺位
  fail-closed），解读缓存于 `bot_explanations`（键 `audit:<dir>[:<finding_id>]`，
  重复解读按键替换）；
- 生成后宿主把解读追加进治理对话线程（`ChatService.post_explanation`，
  `chat_assistant` 事件，前缀 `【AI 解读 · <finding> · <audit_id>】`，尾注
  「非裁决 · 引用确定性证据 · 不进审计包」）；无收据时消息只显示 tokens，
  不声称"已签收据"；threadId 缺省时新建标题为 `AI 解读 · <audit_id>` 的线程；
- 接口：`POST /api/gov/audit/<dir>/explain`（`{finding_id?, threadId?}`）、
  `GET /api/gov/audit/<dir>/explanations`（缓存读取）；
- 解读是消费产物，不是证据：不进 bundle、不进链、不参与任何裁决。
