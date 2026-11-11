# Work 模式（Phase 1：项目 + 运行工作台）

Work 现在是一个**默认落地的工作台**，不是“新对话”的别名：先回答“有哪些项目、哪些运行在跑、哪些需要人处理”，再从项目进入运行。

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

## 运行归属的两种事实强度

- **创建时绑定**（推荐）：`projectId` 进 `binding.spec.project_id`，随 trace 可见、可审计；
- **事后移动**：只改 `meta.json`，与 pin/rename 同级，不重写历史链。

这个差异是刻意的：证据链记录“运行时属于哪个项目”，不记录“后来被人整理到哪个项目”。

## 工作台信息架构

- **项目卡**：名称、绝对路径、验证命令、运行/进行中/待处理/完成计数；动作是「新运行」「设置」。
- **运行看板**：三列
  - 进行中：`status=running`
  - 待处理：`attention`（等待审批 / 待恢复 / 失败 / 拒绝）
  - 最近完成：其余终态，按时间倒序，最多 20 条
- **过滤**：全部项目 / 单个项目 / 未归属（历史 run 没有 `projectId`）。
- **新运行**：把项目上下文交给现有 composer，仍走同一套 intake / 门控 / 预算 / 回执；不复制第二套运行表单。
- 列表接口新增紧凑字段 `projectId` 与 `attention`；完整 `pending`（含命令预览）仍只在 detail 返回。

## 迁移与兼容

- 旧 `fa.projects` / `fa.runProjects` **不删除、不再写入**；Work 首页检测到旧标签时给一次性说明，引导创建带目录的项目。
- 旧 run 没有 `projectId`：看板「全部」中仍可见，归入「未归属」，可通过运行菜单事后移动。
- Chat 线程模型未改：run 仍不拥有 chat thread；Work 项目是 run 的容器，不是对话容器。
- finance/SFT prompt 路径与冻结哈希不受影响。

## 非目标（后续阶段）

1. **并行 agent + worktree 全量隔离**：目前 worktree 仅 fast 顶层项目运行；普通运行仍是隔离副本。
2. **完整 review 队列 / durable approvals**：当前「待处理」列来自 run 状态与内存 pending；重启后 pending 不恢复。
3. **项目级自动化**：`scheduler.sqlite` 仍不保存 project/spec/model，定时运行默认 mock。
4. **Work skills / 插件注入**：技能仍属治理区 Bot，插件市场是独立页面。

## 验证

```bash
python3 -m unittest tests.test_projects tests.test_server_projects -v
node --test viewer/tests/work-home.test.js
cd viewer && ./node_modules/.bin/playwright test e2e/work-home.spec.js
python3 scripts/gate.py
```

手动：Work 首页新建项目 → 新运行 → 看板出现 → 刷新仍在 → 设置里归档。