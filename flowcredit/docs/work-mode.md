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

- Work 默认落地 = 任务舞台（`Build your own work.`），`chat.active=false`。`sendTask` 先用保守的确定性规则分流：只有明确的执行/交付意图进入 `后台 clarifier → /api/runs`；问候、知识问答、解释和歧义输入进入真实 Chat 回答，不创建 Run。它不是语义模型分类器；Chat 中仍可确认执行提案后进入治理运行。舞台按 Kimi Work 的中轴排布：标题、说明、composer、快捷建议居中，不再叠轨道插图。
- **明确任务提交即开跑（Kimi 式）**：任务不再进入独立的「需求确认」页。`sendTask` 先把明确任务交给后台 clarifier 准备执行方式/预算，返回 readyTask 后立即创建运行并进入任务线程；准备期间只显示任务卡与「正在准备任务」。返回 choices 时先留在任务页，选择后再次 ready 即自动开跑；在途的重复选择被忽略，双击只创建一次运行，刷新不会重放建 run。research 缺来源或项目缺验证命令时停在任务页，保留 `+` 面板补充；open/script 无需 URL。财务模板仍走原问卷。
- 选中项目后，composer 占位符变成「在『项目名』中要完成什么？」，提交时带 `spec.project_id`。
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

## 人工等待的持久恢复

审批与提问的 pending 会随 run meta 落盘。Console 重启后，只有同时具备 `taskId`、pending id 且能识别为审批或提问的中断运行会启动恢复 worker；普通中断运行仍标为 `stopped`，不会自动重放模型或工具。

- 审批：恢复载荷必须与最新 `approval_requested` 和已验证 proposal 的工具及参数完全一致。批准后仅为这个已由 `approval_granted` 证明的步骤跳过重复门控，原模型轮不重放、工具只执行一次；拒绝或超时走正常 denied 收尾，后续动作仍按原策略审批。
- 提问：恢复时最新事件必须是完全匹配的 `ask_operator tool_started`。人工作答经 `ToolRegistry.observe()` 校验后补写原工具结果，不再次调用 handler、不再次收费；下一轮模型从链上读取答案。
- 同一 pending 的重复决策或回答返回 409；meta 与哈希链不匹配时恢复失败，不执行原工具。

定向验证：`python3 -m unittest tests.test_pending_recovery tests.test_ask_operator -v`。前两条测试硬杀并重建真实服务进程，覆盖批准、拒绝、重复决策与一次执行；提问测试以新 SQLite 连接和全新 Harness/Loop 实例覆盖答案续接与单次计费。

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
