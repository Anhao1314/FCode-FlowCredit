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
Projects                                   ＋
  ▾ FlowCredit   [1 进行中] [1 待处理]  3
      ● 修 pricing 取整            2 分钟前
      ✓ 调研公开来源                1 小时前
  ▸ 另一个项目
  （没有项目时显示「还没有项目」）

对话
  历史 chat 线程

未归属运行（仅当存在项目时）
  没有 projectId 的历史 run

治理历史（Governance 区）
```

- **项目行**：名称、路径 tooltip、运行数、进行中/待处理徽标；点击展开/收起，并把 composer 绑定到该项目。
- **项目动作**：悬停出现「＋ 新运行」「⋯ 项目设置」；设置里可改名称/验证命令、归档。
- **运行嵌套**：项目展开后直接显示该项目的 run 行（状态点、标题、时间），点击进运行视图，不离开 Work。
- **新建项目**：Projects 标题的「＋」在侧栏内展开表单（名称/绝对路径/验证命令），不跳转主区。
- **未归属运行**：没有 `projectId` 的历史 run 保留在平坦列表；已有项目的 run 不重复出现在这里。
- **空态**：没有项目时侧栏只显示一行「还没有项目」；主区仍是任务舞台，可直接描述任务（未归属运行）。

运行归属的两种事实强度：

- **创建时绑定**（推荐）：`projectId` 进 `binding.spec.project_id`，随 trace 可见、可审计；
- **事后移动**：只改 `meta.json`，与 pin/rename 同级，不重写历史链。

这个差异是刻意的：证据链记录“运行时属于哪个项目”，不记录“后来被人整理到哪个项目”。

## 主区

- Work 默认落地 = 任务舞台（`Build your own work.`），`chat.active=false`，提交走 `sendTask → 后台 clarifier → /api/runs`，不是聊天。舞台按 Kimi 2.8 的中轴排布：标题、说明、composer 居中，轨道场景退为背景水印。
- **提交即开跑（Kimi 式）**：通用委托不再进入独立的「需求确认」页。`sendTask` 先把任务交给后台 clarifier 准备执行方式/预算，返回 readyTask 后立即创建运行并进入任务线程；准备期间只显示任务卡与「正在准备任务」。返回 choices 时先留在任务页，选择后再次 ready 即自动开跑；在途的重复选择被忽略，双击只创建一次运行，刷新不会重放建 run。research 缺来源或项目缺验证命令时停在任务页，保留 `+` 面板补充；open/script 无需 URL。财务模板仍走原问卷。
- 选中项目后，composer 占位符变成「在『项目名』中要完成什么？」，提交时带 `spec.project_id`。
- 运行默认模型由 `FCODE_DEFAULT_RUN_MODEL` 指定（缺省 `mock`），与设置页的聊天默认模型解耦；未手动选择时，失效的历史模型会自动回落到第一个可用 provider。
- 运行视图以任务线程为主体：执行线程常驻；「变更 / 文件」不再替换主区，而是在右侧检查器抽屉里侧开，关闭后回到执行线程。Work 不再有主区项目卡或看板。

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
- 旧 run 没有 `projectId`：保留在「未归属运行」，可通过运行菜单的事后归属移动。
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
