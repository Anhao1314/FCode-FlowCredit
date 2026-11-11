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

- Work 默认落地 = 任务舞台（`Build your own work.`），`chat.active=false`，提交走 `sendTask → intake → /api/runs`，不是聊天。
- 选中项目后，composer 占位符变成「在『项目名』中要完成什么？」，提交时带 `spec.project_id`。
- 运行默认模型由 `FCODE_DEFAULT_RUN_MODEL` 指定（缺省 `mock`），与设置页的聊天默认模型解耦；未手动选择时，失效的历史模型会自动回落到第一个可用 provider。
- 运行视图沿用既有任务线程 / 项目三视图（执行、变更、文件）；Work 不再有主区项目卡或看板。

## 迁移与兼容

- 旧 `fa.projects` / `fa.runProjects` **不删除、不再读取**；项目以服务端为准。
- 旧 run 没有 `projectId`：保留在「未归属运行」，可通过运行菜单的事后归属移动。
- Chat 线程模型未改：run 仍不拥有 chat thread；Work 项目是 run 的容器，不是对话容器。
- finance/SFT prompt 路径与冻结哈希不受影响。

## 非目标（后续阶段）

1. **并行 agent + worktree 全量隔离**：目前 worktree 仅 fast 顶层项目运行；普通运行仍是隔离副本。
2. **完整 review 队列 / durable approvals**：`attention` 来自 run 状态与内存 pending；重启后 pending 不恢复。
3. **项目级自动化**：`scheduler.sqlite` 仍不保存 project/spec/model，定时运行默认 mock。
4. **Work skills / 插件注入**：技能仍属治理区 Bot，插件市场是独立页面。

## 验证

```bash
python3 -m unittest tests.test_projects tests.test_server_projects -v
node --test viewer/tests/side-nav.test.js viewer/tests/sidebar.test.js
cd viewer && ./node_modules/.bin/playwright test e2e/work-sidebar.spec.js
python3 scripts/gate.py
```

手动：侧栏新建项目 → 点项目展开 → 动作「新运行」→ composer 出现项目占位符 → 提交 → 运行嵌在项目下 → 刷新仍在 → 项目设置归档。
