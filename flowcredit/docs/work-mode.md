# Work 模式（Phase 1：侧栏项目 + 运行线程）

Work 的主区始终是**任务/对话线程**；项目、线程与运行状态住在**侧栏**。这是和 Codex Work 对齐的部分：侧栏回答“有哪些项目、每个项目下有哪些线程”，主区只呈现当前选中的线程。

## 项目是服务端实体

此前项目只是浏览器 `localStorage` 里的 `{id, name}`，没有目录、没有服务端身份。现在：

- `runs 目录旁 projects.sqlite` 保存 `id / name / path / verification / created / archived`；
- 目录必须是**存在且绝对**的路径（复用 `validate_project_spec` 的规范化）；
- 路径唯一；删除只有**归档**——历史运行的 `projectId` 不能被物理删除或成孤儿；
- `verification` 是项目级默认值，启动运行前必须存在。

API：

```text
GET  /api/projects              项目 + run 统计（runs/running/attention/succeeded/lastRunAt）
POST /api/projects              {name, path, verification?}
POST /api/projects/update       {id, name?, verification?, archived?}
POST /api/projects/archive      {id}
POST /api/runs                  {projectId, ...}       创建时绑定
GET  /api/runs?projectId=…      按项目过滤
POST /api/runs/{id}/project     {projectId}            事后归属（meta-only）
```

## 侧栏信息架构

```text
项目                                       ＋
  ▾ FlowCredit   ● 待处理 · 2         3
      ● 修 pricing 取整            2 分钟前
      ✓ 调研公开来源                1 小时前
  ▸ 另一个项目
  （没有项目时显示「还没有项目」）

任务                                       ＋
  今天 / 昨天 / 更早
  ✓ 没有 projectId 的运行（有项目时才只列未归属）

对话
  历史 chat 线程

治理区（Governance） 治理台 / 团队 / 工作台 / 审计台 → 审计记录
```

- **项目行**：名称、路径 tooltip、运行数，右侧小圆点表示进行中（绿）或待处理（琥珀）；点击展开/收起，并把 composer 绑定到该项目。
- **项目动作**：悬停出现「＋ 新运行」「⋯ 项目设置」；设置里可改名称/验证命令、归档。
- **运行嵌套**：项目展开后直接显示该项目的 run 行（状态点、标题、时间），点击进运行视图，不离开 Work。
- **新建项目**：项目标题的「＋」在侧栏内展开表单（名称/绝对路径/验证命令），不跳转主区。
- **任务**：没有 `projectId` 的历史 run 保留在平坦列表；已有项目的 run 不重复出现在这里。任务行只留状态点、标题与时间，模板/轮次/成本在主区线程里看。
- **空态**：没有项目时侧栏只显示一行「还没有项目」；主区仍是任务舞台，可直接描述任务（未归属运行）。

运行归属的两种事实强度：

- **创建时绑定**（推荐）：`projectId` 进 `binding.spec.project_id`，随 trace 可见、可审计；
- **事后移动**：只改 `meta.json`，与 pin/rename 同级，不重写历史链。

这个差异是刻意的：证据链记录“运行时属于哪个项目”，不记录“后来被人整理到哪个项目”。

## 主区

- Work 默认落地 = 任务舞台（`Build your own work.`），`chat.active=false`。`sendTask` 先经意图三分流（`core/intent-router.js`，与 Python 端 `flowagent.intent_router` 逐条对齐）：**chat**（问候/问答/解释）进真实 Chat 回答；**clarify**（有执行倾向但缺对象/来源/交付条件，或无上下文的"继续刚才那个修改"）停在任务确认页；**work**（明确可交付）保持提交即开跑。只有低置信残余桶会请求 `/api/intake/route`，可选语义分类器默认关闭（`FCODE_INTENT_CLASSIFIER=1`），失败/缺位一律回退确定性结果，歧义输入绝不自动变成 Run；路由结果不改变工具授权。详见 [general-benchmark.md](general-benchmark.md)。舞台按 Kimi Work 的中轴排布：标题、说明、composer、快捷建议居中，不再叠轨道插图。
- **明确任务提交即开跑（Kimi 式）**：任务不再进入独立的「需求确认」页。`sendTask` 先把明确任务交给后台 clarifier 准备执行方式/预算，返回 readyTask 后立即创建运行并进入任务线程；准备期间只显示任务卡与「正在准备任务」。返回 choices 时先留在任务页，选择后再次 ready 即自动开跑；在途的重复选择被忽略，双击只创建一次运行，刷新不会重放建 run。research 缺来源或项目缺验证命令时停在任务页，保留 `+` 面板补充；open/script 无需 URL。财务模板仍走原问卷。
- 选中项目后，composer 占位符变成「在『项目名』中要完成什么？」，提交时带 `spec.project_id`。
- Composer 工具栏只保留「+ / 审批档位 / 模型」；**Fast 与电脑使用合并进「+」菜单的执行模式区**（一级菜单直接切换，「+」按钮角标只在偏离默认时点亮：Fast 关 / 电脑开）。状态随 spec 提交，语义不变。
- 运行默认模型由 `FCODE_DEFAULT_RUN_MODEL` 指定（缺省 `mock`），与设置页的聊天默认模型解耦；未手动选择时，失效的历史模型会自动回落到第一个可用 provider。
- 运行视图以任务线程为主体：执行线程常驻；「变更 / 文件」收进右侧扩展栏（rail），主区不被替换。代码变更栏显示文件列表、Patch、应用到原项目与下载；文件栏只读浏览隔离副本（运行期 `.shell-home`/缓存不列）。关闭 rail 或点「执行」回到线程。Work 不再有主区项目卡或看板。
- 最终回复里直接给交付物卡：列出本次变更文件；`gif/png/jpg/webp` 用 `<img>`、`mp4/webm` 用 `<video>` 内联预览，原始字节走 `GET /api/runs/{id}/workspace-media?path=…`（同一 WorkspaceSandbox 路径狱、20 MiB 上限、only 同源）。媒体文件不必先打开右侧 rail 才能看到。
- 成功回答不显示成本 chips、「签发报告」「复制诊断」等运行管理动作；失败卡仍保留重试/追加预算/诊断。完整证据与成本留在运行详情，报告走 `/report` 命令，诊断在设置页。
- 单文件 HTML 交付物额外给「预览动画」：点击后用 sandbox iframe（`sandbox=""`）加载 `GET /api/runs/{id}/workspace/{path}` 的内容，并注入 `default-src 'none'; script-src 'none'` 的 CSP；CSS/SVG 动画可运行，脚本、外链与网络被阻断。
- 单文件 HTML/SVG/CSS 动效任务由 intake 识别为 `motion-render`：短系统契约、6 个工具（`ls_workspace`/`read_file_raw`/`write_file`/`edit_file`/`run_command`/`motion_check`）、`max_steps=4`，要求和 `motion_check` 校验轮速/背景速度、曲柄/腿周期与脚-踏板 hook。质量结果写入 `project.json.quality`，交付物卡显示分数与 issues；它不重写功能终态。v2（仅文本质量门）原始 prompt 实测：功能通过 5 calls / 62,000 tokens；加质量修正后 6 calls / 113,179 tokens / 38,760 µUSD，质量 100/100。v3（几何门）原始 prompt 实测：5 calls / 101,113 tokens / 34,991 µUSD / 4m16s，功能与公开/隐藏验收通过，质量 100/100（脚-踏板最大 6 px，腿/轮 0 px²，4 帧采样）。v3 + stall guard + held-out 采样实测：4 calls / 66,114 tokens / 21,357 µUSD，质量 100/100（脚-踏板 0 px、腿/轮 0 px²，最终采样 150/450/750/1050 ms），公开/隐藏验收通过。v5（rig + coverage + 关键帧名）原始 prompt 实测：14 rounds / 16 calls / 509,330 tokens / 168,534 µUSD；质量 100/100，coverage 8/8，髋距 0 px、最短腿 139 px、脚-踏板 0 px，公开/隐藏验收通过。v6（edit-only repair + repair budget）原始 prompt 实测：7 rounds / 7 calls / 152,178 tokens / 51,153 µUSD；质量 100/100，coverage 8/8，髋距 0 px、最短腿 105 px、腿端点到脚 11 px、脚-踏板 6 px，公开/隐藏验收通过。v7（结构化 required_fixes + 同 hash 检查缓存）实测：7 rounds / 8 calls / 175,993 tokens / 59,868 µUSD，质量 100/100、coverage 8/8，全程用 2 次 edit_file 修复、没有整文件重写；公开/隐藏验收通过。修复预算把 14 rounds / 509k tokens 压到 7 rounds / 152–176k tokens；剩余成本主要来自每轮上下文和模型输出，而不是重复检查。
- 动效质量现在分三层。文本层由 `motion_quality.analyze_motion` 计算轮速/背景速度、曲柄/腿周期；渲染层由 `viewer/motion_frames.py` 调 Playwright：暂停所有 CSS 动画，在 0/300/600/900 ms 各截一帧，量 `.foot` 到最近支撑面（`.pedal` / `.deck` / `.support`）的矩形距离、`.leg`/`.wheel` 包围盒重叠、computed `animation-name`，以及新增的 rig 语义：每条 `.leg` 必须从 `.hip` 附近出发（≤30px）、长度 ≥60px、终点靠近 `.foot`（≤25px）。脚-支撑距离超过 25px、脚无支撑可对、腿/轮相交、腿从曲柄中心起笔、腿太短，或 `.leg` 使用了 `pedal-alt` 这类别名都判 fail。质量报告同时带 `coverage`：缺少 `.hip`、腿路径、脚或帧时返回 `unverified`，不再用 100 分冒充通过；UI 显示「质量未验证 · 覆盖 x/y」。`motion_check` 在模型循环内直接返回这些量；最终 `_publish` 用**偏移采样时刻 150/450/750/1050 ms** 再独立测一次，把 4 帧合成 `contact.png` 放到 run 目录的 `quality-frames/`，交付卡通过 `GET /api/runs/{id}/quality-frame?name=contact.png` 展示。帧与几何不进入 Git 交付物，也不改运行终态。
- 当前质量门仍只覆盖可计算的物理/时序/接触，不覆盖审美；视觉批评仍需一个能看图的 critic，但当前部署只用 `deepseek-flash`，所以 P0.2 只做确定性几何。
- 任务线程按正常对话呈现：用户任务右对齐气泡，Agent 以 32px 头像 + `FCode / 本机 Agent` 开场，状态、计划、工具与最终回复都缩进到同一 Agent 回合；最终回复按 Markdown 渲染。审批、预算与工具证据仍是原来的卡片/行，不改语义。
- 状态行只留「状态 + 耗时」：耗时取链上首末事件之差（运行中取当前时刻），轮次、事件数与工具名退到执行过程；失败码仍留在状态行。
- 产物与报告分开判：独立验证通过后，即使模型声明的某条引用在链上找不到，也不再丢弃产物、把整次运行标 failed；结果记 `source_links=unresolved`、`evidence_unresolved`，最终回复显示「已完成 · N 处引用待核实」，相关结论按未采信处理。结构性非法的 evidence 块仍在提案阶段拒绝。
- `ask_operator` 可带 2–4 个 60 字以内的建议答案（`options`，经工具门校验）。线程把它们渲染成可点选的单选项；点选只填入输入框，提交仍是同一个普通字符串回答，观察契约不增加第二种形状。
- 项目运行会读取隔离工作区里的 `AGENTS.md`（根目录与子目录，最多 8 个文件 / 单文件 4,000 字 / 合计 8,000 字）。内容作为「项目约定」注入系统提示，明确不能覆盖系统/操作员指令；每个文件的 SHA-256 与截断标记写进 `task.binding.project_instructions`，可审计模型到底看到了哪一版约定。
- 同一次运行内重复完整读取同一路径时，工具会重新读盘并核对 SHA-256；内容未变则返回 `unchanged: true` 和空 `content`，不再把重复正文送回上下文。需要正文时传 `force=true`；内容变化或带 `offset`/`limit` 的分段读取都返回全文。链上仍记录真实读取与当前哈希。
- `motion-render` 还带 stall guard：计划里已有待写的 `write_file`/`edit_file`，却连续两轮只读/跑命令时，下一轮上下文注入 `progress_guard` 要求先写；到第 4 轮仍未写入则以 `NO_PROGRESS` 结束，不再把预算烧在无效探索上。没有该标记的 profile 行为不变。
- `motion-render` 的修复是局部优先：首次创建文件用 `write_file`，之后对同一文件的修复必须用 `edit_file`；`write_file` 默认返回 `FILE_EXISTS_USE_EDIT`，只有显式 `rewrite=true` 才允许整文件替换（每条路径最多一次）。单次 `write_file` content 超过 3,000 字符时返回 `WRITE_TOO_LARGE_USE_APPEND`，必须先写骨架，再用 `append=true` 分块追加；这避免供应商在巨型工具参数上直接断连。质量失败超过 5 次或同一 failure fingerprint 出现 3 次时，loop 以 `REPAIR_BUDGET_EXCEEDED` / `NO_PROGRESS` 停止，避免 14 轮级别的重复全文生成。`motion_check` 同时返回结构化 `required_fixes`，并对同一 artifact hash 的重复检查直接返回 cached，节省浏览器渲染和重复检查。
- 项目独立验证命令的默认白名单已覆盖常见生态：`pytest`/`unittest`、`npm`/`pnpm`/`yarn`/`npx`/`bun`/`deno`、`uv`/`poetry`/`pip`/`ruff`/`mypy`、`cargo`、`go`、`make`/`cmake`/`ctest`/`ninja`、`mvn`/`gradle`/`dotnet`、`bundle`/`rake`/`ruby`、`composer`/`php`、`swift`。仍是 argv-only、无 shell；未列出的项目专用命令用 `commands.json` 追加。

## unknown 结果的恢复出口

长跑在传输层 0 字节断连时会落 `recovery_required / MODEL_OUTCOME_UNKNOWN`。此前控制台没有出口：`reconcile` 要求一张匹配回执，而 `resume` 只接受 `failed + BUDGET_EXCEEDED/ROUND_LIMIT`。现在失败卡仅对这一失败码显示「确认恢复」，二次确认后走 `POST /api/runs/{id}/recover`（202；状态不对 409）。

两条路径，都不猜测旧调用成功：

| 条件 | 动作 | 账目 |
| --- | --- | --- |
| `find_intent(task_id, intent_round)` 找到验签通过的回执 | `reconcile`，不重发 | 无新增 paid call，`attempts` 不变 |
| 无回执 | `model_reissued` 事件后回到 `running` | 旧轮保持 unknown 且已计费；下一轮是全新计费，事件写明 `possible double billing` |

`model_reissued` 的 detail 固定含 `{by, unknown_round, receipt_ref: "", risk}`。状态机新增 `reissued`，只允许 `unknown → reissued → started`；`unknown → validated` 仍是回执专用路径，`unknown → started` / `→ succeeded` 都被显式拒绝。

传输层只有 `RemoteDisconnected` 与 `partial` 为空的 `IncompleteRead` 会自动重试一次，并把 `transport_retries`（整数）写进 usage、metadata 与签名回执；HTTP 4xx/5xx、超时、半包、非 JSON 一律不重试，仍走 `MODEL_CALL_FAILED` → unknown。

## 预算追加的唯一口径（S1）

模型工具 `request_budget_extension`、操作员 `/budget`、失败续跑 `/resume` 共用同一个纯策略 `flowagent.runtime.budget.plan_adjustment`，不再各自实现上限：

- `base` 是 Run 创建时、价格缩放之后写入 `task.binding.budget_base` 的不可变信封。续跑只读它，不重算价格、不重置 base，因此不能靠 Resume 获得新的 2× 额度。
- 单次申请任一维度最多 `+50% base`；累计最多 `+100% base`（2×）。超出部分按维度截断并记 `capped`；没有剩余 headroom 时拒绝，不产生负额度。
- `extraRounds` 仍是独立 Driver 语义，不与 calls 合并：一次 Loop Round 可以包含两次 Provider 尝试（首次失败、一次重试），轮次预算用 `rounds_base` 与历次 `task_resumed.rounds.granted` 独立累计，单次最多 `+50%`、累计最多 `+100%`；它不写入 `BudgetLimits`。
- `request_budget_extension` 永不允许「本次运行内记住」：预算决定必须逐次审批。其他 Work 工具按参数范围记忆：`run_command` 全参数哈希、`fetch_url` 主机、`write_file`/`edit_file` 路径、`browser_act` action+target；`terminal`、`computer_request`、`apply_child_changes`、`spawn_subagents` 与未分类 `mcp__*` 永不记住。
- `budget_adjusted` 与其对应的 `tool_succeeded` 在同一 SQLite 事务提交；内存 `limits` 只在提交成功后更新，apply 失败不会留下「已加预算但工具未成功」的半状态。
- `request_id` 幂等：同一 id 且同一申请（包括当时被截断的原申请）重放为空操作；同一 id 不同申请拒绝；不同 id 是新的独立申请。操作员路径在计算新 delta 之前先查链上同 id 事件，因此重试不会二次消耗累计额度；续跑请求的 `task_resumed.rounds` 也带同一个 `request_id`，重放不会二次发放 `extraRounds`。

事件只记录整数，`before`/`after` 是完整四维信封：

```json
{"type":"budget_adjusted","payload":{"detail":{
  "policy_version":2,"request_id":"op-1","by":"operator",
  "before":{"max_calls":12,"max_reads":30,"max_cost_microusd":500000,"max_tokens":120000},
  "after":{"max_calls":18,"max_reads":30,"max_cost_microusd":500000,"max_tokens":120000},
  "requested":{"calls":20,"reads":0,"cost_microusd":0,"tokens":0},
  "granted":{"calls":6,"reads":0,"cost_microusd":0,"tokens":0},
  "capped":{"calls":true,"reads":false,"cost_microusd":false,"tokens":false}
}}}
```

恢复时 `replay_limits` 从 base 按事件序号严格重建：v2 事件要求 `before == effective`、`after == before + granted`、任何维度 `<= 2× base`；同一 `request_id` 的 after 必须一致。旧格式事件（无 `policy_version`）只在没有 `budget_base` 的旧运行上按 raise-only/2× 兼容一次，并写诊断；它不会成为第二套预算事实来源。恢复永远不以 `max(effective, usage)` 抬限。发送前被预算拒绝时另记 `budget_reservation_rejected`（含 `blocked_dimensions` 与 `provider_request_sent=false`），与消耗口径分开看。

## 人工等待的持久恢复

审批与提问的 pending 会随 run meta 落盘。Console 重启后，只有同时具备 `taskId`、pending id 且能识别为审批或提问的中断运行会启动恢复 worker；普通中断运行仍标为 `stopped`，不会自动重放模型或工具。

- 审批：恢复载荷必须与最新 `approval_requested` 和已验证 proposal 的工具及参数完全一致。批准后仅为这个已由 `approval_granted` 证明的步骤跳过重复门控，原模型轮不重放、工具只执行一次；拒绝或超时走正常 denied 收尾，后续动作仍按原策略审批。
- 提问：恢复时最新事件必须是完全匹配的 `ask_operator tool_started`。人工作答经 `ToolRegistry.observe()` 校验后补写原工具结果，不再次调用 handler、不再次收费；下一轮模型从链上读取答案。
- 同一 pending 的重复决策或回答返回 409；meta 与哈希链不匹配时恢复失败，不执行原工具。

定向验证：`python3 -m unittest tests.test_pending_recovery tests.test_ask_operator -v`。前两条测试硬杀并重建真实服务进程，覆盖批准、拒绝、重复决策与一次执行；提问测试以新 SQLite 连接和全新 Harness/Loop 实例覆盖答案续接与单次计费。

## Coding Context Pack（W1）

通用 Work 的 coding 任务现在有第一轮宿主预取的 **代码上下文包**：

- 根据 objective 用确定性 BM25 选出最多 5 个候选实现文件；
- 为候选文件匹配相关测试（文件名 / tests 目录 / 候选模块名）；
- 解析 Python AST 与 JS/TS 常见 symbol/import 形状，给出有界符号边；
- 若 spec 声明了 verification 命令，同时注入模型可运行的公共检查；
- block 上限 3,200 字符，截断时必须标 `truncated`；
- 每个候选/测试文件带 SHA-256、行号和有界 snippet；
- `block_sha256` 与完整 citations 写入 task binding，审计可见模型第一轮看到了哪些文件；
- 没有 objective、没有可检索词或没有命中时不注入空块。

这不是权限放大，也不是最终事实：候选文件和 snippet 只是导航，模型编辑前仍须
`read_file / read_file_raw` 打开原文件；执行仍走既有 gate、approval、budget 和 receipt。

定向验证：`python3 -m unittest tests.test_code_context -v`。

## Coding Task Contract（W2）

一句 objective 不是契约。操作员可以通过 `spec.codingContract` 把 coding 任务
升级为**显式、可强制执行的交付契约**：

```json
{
  "codingContract": {
    "deliverables": ["src/fix.py", "REPORT.md"],
    "forbiddenPaths": ["src/legacy/**"],
    "noNewDependencies": true,
    "allowTestEdits": false,
    "requireVerification": true,
    "definitionOfDone": ["声明的验证命令必须通过"]
  }
}
```

**运行时强制，不是 prompt 提醒**：

- **文件工具层**：`write_file` / `edit_file` 直接拒绝测试路径（`tests/**`、
  `test_*`、`*_test.*`、`*.test.*`、`*.spec.*`，`allowTestEdits=true` 可豁免）、
  声明的禁区路径、以及 `noNewDependencies=true` 时任意深度的依赖清单
  （`package.json`、lockfiles、`requirements*.txt`、`pyproject.toml` 等），
  错误码 `CODING_CONTRACT_PATH_DENIED`。
- **终态门**：`Loop._succeed` 在落 `succeeded` 前向 world 查询完成检查——
  交付文件缺失 → `CODING_CONTRACT_INCOMPLETE`；禁区/测试/依赖面相对
  **构建时基线哈希**有任何漂移（包括 terminal 命令绕过文件工具的写入）→
  `CODING_CONTRACT_VIOLATED`。基线在世界构建时（工作区已播种、模型未运行）
  采集，测试面用与工具层同一套 glob 模式，保证"工具拒绝的路径也是基线盯住的路径"。
- **创建时校验**：`validate_delegation_spec` 拒绝非法形状（未知字段、绝对路径、
  `..`、非字符串项）；`requireVerification=true` 必须配套 `spec.verification`
  命令，否则显式声明 `requireVerification=false`；motion capability 运行时
  不经过该门，拒绝与 `fastProfile=motion-render` 组合；非 general 模块拒绝携带契约。
- **审计链**：契约以 `coding_contract_binding` 写入 task binding；禁区基线
  （路径 → SHA-256）以 `coding_contract_baseline` 入链；注入 prompt 的契约
  说明只复述同一份 normalized 策略——判定来自门，不来自模型读了多少遍文字。
- **无契约不受影响**：没有 `codingContract` 时工具不拒绝、门直通、binding
  不新增字段，既有行为零变化。

拒绝自然语言契约解析：操作员没声明的，runtime 不假装约定过；`definitionOfDone`
只是给模型和审计者的说明文字，永远不是判定依据。

定向验证：`python3 -m unittest tests.test_coding_contract -v`。

## 验证修复回路（W3）

以前 Work 的宿主独立验证一旦失败，run 当场判死——模型写完产物、测试挂了，
它永远不知道自己错在哪。现在**验证失败是反馈，不是死刑**：

- **失败回喂**：宿主独立重跑声明的验证命令失败时，loop 从链上取回这次运行的
  命令、退出码和**有界输出尾**（≤800 字符，链上保留全文），写入工作记忆的
  `verification_failures`；下一轮模型上下文直接看到真实的 `AssertionError`，
  而不是一句"验证未通过"。
- **有界修复回合**：每个 run 最多 **2 次**修复机会（与 proposal 形状修复同预算），
  每次以链事件 `verification_repair_requested` 记录 attempt 序号——计数存在链上，
  跨进程恢复不重置。耗尽后仍以**原始验证失败代码**（如 `RESULT_VERIFICATION_FAILED`）终止。
- **复用既有扩展路径**：修复回合靠 `plan_extension`（open/project 模式本来就有）
  让模型用 additions 声明修复步骤；验证通过前不许成功，第二次完成检查会重新独立验证。
- **治理类拒绝不重试**：`VERIFICATION_NOT_AUTHORIZED`（授权撤回）、
  `VERIFICATION_DECLARATION_INVALID`、`VERIFICATION_UNAVAILABLE` 不进修复环——
  权限结论不因重试而改变，反复重提的模型是在试图绕门，不是在修复产物。
- **蓝图域冻结**：`plan_extension=False` 的域保持原语义：第一次失败立即 abort。
- **prompt 同步**：general/general-fast/general-fast-native 的恢复规则新增
  第 3 条（按 code/exit_code/output 定位、修复后继续提交、禁止重复无效修改），
  prompt 版本 bump 至 v12 / fast-v4 / fast-native-v4。

负例仍然成立：假装修好、跑无关命令替代声明验证、耗尽修复预算，最终都是
failed 且 `task_succeeded` 不出现——只是多了一次诚实的机会，不是一个豁免。

定向验证：`python3 -m unittest tests.test_verification -v`。

## agent-browser 后端（可选）

`browser_inspect` / `browser_act` 的浏览器后端可切换为
[vercel-labs/agent-browser](https://github.com/vercel-labs/agent-browser)
（54k★，Apache-2.0）：页面读成**紧凑无障碍树文本 + 元素引用**（`@e1`），
动作按引用执行，被遮挡/失效的点击**如实失败**不假成功；每工作区一个
`--session` 守护进程跨调用保活。

```bash
npm install -g agent-browser        # CLI
FCODE_BROWSER_BACKEND=agent-browser # 部署开关（默认仍是 Playwright sidecar）
```

- 快照以 `snapshot -i` 读取（token 效率远高于 DOM dump），解析为证据
  `elements`（role/name/ref），`text` 即快照原文（有界截断如实标注）；
- 动作支持 ref（`@eN`）与 CSS/文本选择器；点击前后双截图 + 像素 diff 证据不变；
- 同页复访不重开：先 `get url` 比对，保住会话状态与 refs 新鲜度；
  open 后的快照竞态自动重试一次；"selector not found" 重拍快照重试一次；
- **安全边界不变**：loopback URL 门禁在宿主侧（`validate_browser_url`），
  agent-browser 本身不设限，策略必须由 FCode 持有；审批/预算/证据链全部照旧；
- CLI 缺席时工具如实返回 `available:false` + 安装提示，不炸 run。

定向验证：`python3 -m unittest tests.test_agent_browser -v`
（含真 CLI 用例，未安装时自动跳过）。

## 知识库检索（R1）

操作员可以通过环境变量给部署配置**本地知识库**（政策、规范、runbook）：

```bash
FCODE_KNOWLEDGE_ROOTS="policy=/path/to/policy-docs:ops=/path/to/runbooks"
```

- 来源只由**宿主**配置，模型和 run spec 都不能指定路径；没有配置时 `search_knowledge` 工具完全不注册，不会出现“看起来能搜但其实为空”的工具。
- 检索是确定性 BM25：markdown 按标题分块（长章节退化为行窗口），命中返回 `source / path / title / headings / 行号 / 有界摘要`，模型引用文档而不是凭空编造。
- 相关性只作为内部排序信号，观察结果里的 `score_bp` 是整数基点；FCode 证据链拒绝浮点。
- 敏感文件（`secrets.md`、`id_rsa*`、`*.pem` 等）不索引；符号链接不跟随；单文件/总量有上限，超限如实标 `truncated`。
- 工具是 read effect，不写工作区、不联网；run 的 binding 记录启用的 source/root，审计可见模型看到了哪套知识库。

### R1.5：目标相关上下文预取

Work 启动时还会按 objective 做一次有界知识库预取：

- 最多取 3 条命中，block 上限 2,400 字符，超出如实标 `truncated`；
- 注入内容只是“候选证据”，并明确要求模型引用前尽量打开原文件确认；
- 没有来源、没有 query 或没有命中时不注入空块；
- 精确注入的 block 会计算 `block_sha256`，与 citations、截断标记一起写入 task binding；
- `search_knowledge` 仍是模型主动检索工具；预取只是让 Work 第一轮就知道“有这套知识库存在”。

定向验证：`python3 -m unittest tests.test_knowledge_index -v`。

## 迁移与兼容

- 旧 `fa.projects` / `fa.runProjects` **不删除、不再读取**；项目以服务端为准。
- 旧 run 没有 `projectId`：保留在「任务」，可通过运行菜单的事后归属移动。
- Chat 线程模型未改：run 仍不拥有 chat thread；Work 项目是 run 的容器，不是对话容器。
- finance/SFT prompt 路径与冻结哈希不受影响。

## 非目标（后续阶段）

1. **并行 agent + worktree 全量隔离**：目前 worktree 仅 fast 顶层项目运行；普通运行仍是隔离副本。
2. **完整 review 队列**：审批与提问已支持 durable pending；跨运行的人工复核队列仍不在当前范围。
3. **项目级自动化**：`scheduler.sqlite` 仍不保存 project/spec/model，定时运行默认 mock。
4. **Work skills / 插件注入**：技能仍属治理区 Bot，插件市场是独立页面。

## 验证

```bash
python3 -m unittest tests.test_pending_recovery tests.test_ask_operator -v
python3 -m unittest tests.test_projects tests.test_server_projects -v
node --test viewer/tests/side-nav.test.js viewer/tests/sidebar.test.js
cd viewer && ./node_modules/.bin/playwright test e2e/work-sidebar.spec.js
python3 scripts/gate.py
```

手动：侧栏新建项目 → 点项目展开 → 动作「新运行」→ composer 出现项目占位符 → 提交 → 运行嵌在项目下 → 刷新仍在 → 项目设置归档。
