# FCode-FlowCredit 仓库架构梳理

> 基线：`main` 分支（PR #4 合入后，2026-11-03），Apache-2.0。
> 文中所有行数、测试数、依赖边均为本地实测，不是照抄 README。
> 审计域 `flowaudit` 已随 PR #4 合入，本文覆盖内核与审计域两部分。

---

## 0. 一句话定位

`main` 分支上，它是：

**一个 host-governed、事件溯源、可崩溃恢复的 agent runtime（内核 `flowagent`）+ 三个可插拔领域（finance 参考负载 / general 默认入口 / audit 安全审计）+ 四个外壳（离线 demo、Work 控制台、benchmark、审计 CLI）。**

最上层是审计产品 `flowaudit`：它把"审计 agent 应用"做成一个领域，叠在同一个内核上，复用同一套门控、预算、哈希链与恢复机制。所以它既是一个可复用的治理内核，也是一个具体的安全审计产品——两者是同一棵树，不是两套实现。

整个仓库的架构主线只有一句：**模型只提议，主机来门控、执行、验证，人保留决定权。**

---

## 1. 顶层目录

| 路径 | 是什么 | 备注 |
|---|---|---|
| `flowcredit/` | **真正的项目主体** | 运行时 + 审计域 + 控制台 + 测试 + 文档，全部在这里 |
| `flowcredit/audit-rules/` | 版本化规则库（JSON 即数据） | 审计规则与证据 Schema，不经代码发布 |
| `flowcredit/fixtures/` | 审计靶子与 ground truth | `demo-target`（MiniAgent）、`fcode-self`（7 个自体缺陷） |
| `flowcredit/config/` | `online-policy.json` | 在线调用的授权范围与钱包上限，主机策略 |
| `desktop/` | PyInstaller 桌面壳（新版） | README 指向这个，`build.sh` 在这里 |
| `archive/node-prototype/` | 旧版 Node 原型，1.7 MB | 只作历史对照，不参与运行 |
| `.agents/skills/fcode-design/` | 给编码 agent 用的设计规范 | 说明开发过程本身用了 agent |
| `.github/workflows/` | CI：Ubuntu/macOS × Python 3.11/3.13 | 第一步断言 `dependencies = []` |
| `mcp.json.example` | MCP 服务器白名单示例 | 模型看不到 URL，只能选主机配置的源 |

**注意：`flowcredit/` 内部还有一个 `desktop/`，与根目录 `desktop/` 是两套实现，而且不是"新版 / 旧版"的关系。**

- 根 `desktop/`：release 打包，staging 源树 + bundle id + 每启动 token 鉴权；`app.py` 最后更新 2026-10-25
- `flowcredit/desktop/`：开发 / 冒烟打包，带 `--smoke-test` 和 provider 环境隔离；`app.py` 最后更新 2026-10-28

两份 `app.py` 有 451 行差异，功能互有对方没有的（token 鉴权 vs 环境隔离 + 冒烟测试）。**这不是删一个就能收敛的重复，而是需要一次功能合并。**

---

## 2. 分层框架（总图）

```
┌─ 外壳 / 入口 ──────────────────────────────────────────────────────────┐
│ demo.py → demo/          8 个离线场景（CLI）                           │
│ viewer/                  Work 控制台：HTTP + SSE + 零构建前端          │
│ benchmark/               19 个版本化任务，复用生产 Loop                │
│ flowaudit/cli.py         审计命令行（audit / verify-bundle）            │
│ chat*.py                 对话分支（只读工具循环；写操作转委托）          │
│ sft/                     trace → SFT 数据 / 策略评测                   │
│ review*.py               运行后人工复核（追加签名，不改写）             │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ 组装（demo/runner.py 是唯一装配点）
┌─ 领域插件 ────────────────────┴────────────────────────────────────────┐
│ domains/base.py  DomainModule 契约：世界/工具/规划器/验证器/审批点      │
│ domains/finance  参考负载：确定性、可独立复算                          │
│ domains/general  默认入口：文件 / 命令 / 网页 / 追问                   │
│   └ tools/finance/*   tools/general/*                                  │
│ flowaudit/       审计产品域（独立包，只依赖 flowagent）                │
│   vulns 分类法 · poc 重放 · targets 适配器 · report · session          │
│   └ audit-rules/（JSON 规则）· fixtures/（靶子与 ground truth）        │
└───────────────────────────────┬────────────────────────────────────────┘
┌─ 运行时内核 runtime/ ─────────┴────────────────────────────────────────┐
│ loop.py          多轮驱动：计划 → 逐轮单动作 → 终止判定                 │
│ harness.py       单动作最小单元：propose→gate→execute→observe→verify   │
│ permissions.py   三道闸：注册表 / 任务范围 / 当前 grant                 │
│ budget.py        四维硬预算，发送前预扣                                 │
│ verification.py  host 声明的完成义务，运行时重新执行                    │
│ recovery.py      结果未知的对账      replay.py  只读重放                │
│ policy.py        Executive–Policy 决策包（弱模型选候选）                │
│ control.py       人工干预点         plan_extension.py 计划扩展审批      │
│ state_machine.py 迁移策略（表在 core/contracts.py）                     │
└───────────────────────────────┬────────────────────────────────────────┘
┌─ 能力面 tools/ ───────────────┴────────────────────────────────────────┐
│ registry.py   封闭注册表（模型可见工具的唯一来源）                      │
│ base.py       ToolSpec / Effect / Observation / 参数严格校验            │
│ artifacts.py  内容寻址工件写入（唯一写字节的地方）                      │
│ evidence.py   文件 → 带 hash 的锚定观测（只读）                         │
│ connectors.py 主机配置的网络源，模型只见 source 名                      │
│ workspace.py  ingest / search / working_paper / cases / facts 共享工具  │
│ mcp/          stdio JSON-RPC 客户端 + 导入注册表的桥                    │
└───────────────────────────────┬────────────────────────────────────────┘
┌─ 记录 / 治理原语 core/ ───────┴────────────────────────────────────────┐
│ contracts.py  Task/Plan/Step/Observation + 合法状态迁移表（事实标准）   │
│ store.py      SQLite + BEGIN IMMEDIATE + sequence CAS + 哈希链          │
│ hashing.py    canonical JSON（排序键、拒绝 float）+ SHA-256             │
│ receipts.py   HMAC-SHA256 签名收据（未知结果的唯一解）                  │
│ plan.py       Goal / Plan / Todo 的持久化形状                           │
│ trajectory/   recorder（观察者） + exporter（导出与独立重校验）         │
└───────────────────────────────┬────────────────────────────────────────┘
┌─ 模型接入 providers/ ─────────┴────────────────────────────────────────┐
│ base.py           提案源接口（只提议，不执行）                          │
│ mock.py           确定性离线模型（按计划走）                            │
│ openai_compat.py  统一传输引擎（/v1/chat/completions）                  │
│ kimi / deepseek / local / oracle / local_policy  预设与实验入口          │
│ prompt.py         唯一的提示词契约     catalog.py  服务端模型菜单       │
└────────────────────────────────────────────────────────────────────────┘
   横切：memory/（派生记忆）· pacing.py（端点级限速）· intake.py（澄清追问）
```

依赖方向的**声称**（`flowagent/__init__.py`）：

```
core  →  tools / providers  →  runtime  →  demo
```

依赖方向的**事实**（AST 实测）：`flowagent` 内部 fan-in 以 `core` 最高，`tools` / `providers` / `memory` 平级，`runtime` 在它们之上；包际方向 `flowaudit → flowagent` 共 22 条，反向 **0** 条。主干成立，`flowagent` 内部只剩 1 处反向边（`sft → demo`，见 §7）。

---

## 3. 包级职责表

| 包 | 文件数 | 行数 | 一句话职责 | 关键约束 |
|---|---:|---:|---|---|
| `core/` | 11 | 1,820 | 契约、跨层词汇、哈希、存储、收据、计划 | 不 import 包内任何其他模块 |
| `runtime/` | 12 | 2,686 | 门控与循环，系统的"法官" | 只依赖 core/tools/providers/memory |
| `tools/` | 30 | 6,495 | 模型唯一的动作面 | 封闭注册表，无动态发现 |
| `providers/` | 13 | 2,829 | 模型接入，只产出提案 | 不执行、不写库 |
| `memory/` | 6 | 1,586 | 派生记忆：上下文 / 案例 / 事实 / 工件 | 只依赖 core，与 tools 平级 |
| `domains/` | 4 | 391 | 领域插件契约与注册 | 内核从不 import 具体领域 |
| `flowaudit/` | 33 | 5,235 | 审计产品域：分类法 / PoC / 适配器 / 报告 | 只依赖 flowagent，反方向有 AST 测试 |
| `demo/` | 13 | 2,801 | 装配 + 8 场景 + 终端渲染 | **唯一装配点** |
| `benchmark/` | 8 | 1,304 | 19 任务评测 | 适配器只喂提案 |
| `sft/` | 6 | 1,173 | trace → 训练数据 / 策略评测 | 依赖 demo（违规） |
| `trajectory/` | 3 | 261 | 录制、导出、独立重校验 | 观察者，不参与决策 |
| 包根平铺 | 10 | 2,479 | chat* / intake / pacing / review* | **缺归属层**（见 §8） |
| **合计** | **~149** | **29,392** | | |

外围：`tests/` 87 个文件、18,528 行、791 个测试；`viewer/` Python 3,902 行 + JS 14,576 行（含 183 个前端测试）；`docs/` 38 篇。

---

## 4. 关键文件逐个说明

### 4.1 `core/` — 记录与治理原语

| 文件 | 行 | 职责 | 被谁用 |
|---|---:|---|---|
| `contracts.py` | 562 | `Task` / `Plan` / `Step` / `ModelState`；`TASK_TRANSITIONS`、`MODEL_TRANSITIONS` 显式状态表；`AgentError` 带稳定错误码 | 全包 |
| `store.py` | 346 | 三张表 `task` / `event` / `budget`；WAL；`BEGIN IMMEDIATE` 串行写；sequence CAS；`verify_chain()` 重算而非信 flag | runtime、viewer、benchmark |
| `hashing.py` | 77 | canonical JSON（键排序、无空白、**拒收 float**）+ SHA-256；`compare_digest` 比对 | store、receipts、trajectory |
| `receipts.py` | 120 | HMAC-SHA256 签名收据；"结果未知"时唯一可用的对账凭据；密钥随库存放（威胁模型写明了） | runtime/harness |
| `plan.py` | 198 | Goal → Plan → Todo 的持久化形状与推进语义 | loop、domains |
| `observation.py` | 54 | `Observation` / `SourceTrust` / 摘要策略；跨层词汇，打破 memory↔tools | tools、memory、runtime |
| `packets.py` | 94 | `DecisionPacket` / `ActionChoice` / `ActionCandidate` 及哈希 | runtime/policy、providers |
| `verification.py` | 193 | host 声明的完成义务与校验；从 runtime 下沉 | runtime、tools/general |
| `ports.py` | 90 | `CaseSearchPort` / `FactQueryPort` + Null 实现 | tools/workspace、domains |
| `facts.py` | 77 | `make_fact` / `logical_key` / `fact_dates` 纯函数 | memory/facts、tools/finance |

### 4.2 `runtime/` — 内核

| 文件 | 行 | 职责 |
|---|---:|---|
| `loop.py` | 1,087 | 多轮驱动。`run()` 主循环；`restore()` 从事件日志重建；`_verify_obligation()` / `_verify_result_set()` 独立复算；`_succeed/_deny/_stop` 终态判定 |
| `harness.py` | 496 | 最小动作单元。propose → gate → charge → dispatch → observe；`_unknown()` 把超时转入 `recovery_required`，**不重试**；观测落库时写入 `trust` 标记 |
| `permissions.py` | 121 | 三道独立检查：工具在注册表 / 在任务 scope / 在当前 grant。任一条不过即 deny |
| `budget.py` | 152 | 调用、读取、成本、token 四维；**发送前扣减，失败不退** |
| `verification.py` | 193 | 完成义务只允许 `run_command`；必须来自冻结的 delegation spec；超时/未知一律 fail closed |
| `recovery.py` | 125 | `tool_started` 已持久但结果未落盘时的对账流程 |
| `replay.py` | 103 | 从已提交事件序列做只读投影，可比较两次运行的等价性 |
| `policy.py` | 261 | Executive–Policy：主机编译 `DecisionPacket`，弱模型只选一个候选 id |
| `plan_extension.py` | 244 | 模型提议的新 todo 由 host 逐条校验后才落库 |
| `control.py` | 89 | 运行中的人工干预：改计划、加预算、跳过问题 |
| `state_machine.py` | 45 | 迁移策略；迁移表本身放在 `core/contracts.py`（因为 store 要用） |

### 4.3 `tools/` — 能力面

**根级五个抽象 + 一个共享工具集：**

- `registry.py`（76）：注册表只做三件事——持有 spec、暴露 schema、按名解析。没有动态发现。
- `base.py`（278）：`Param` / `ToolSpec` / `Observation`；`validate_arguments` 对未知键**拒绝而不是纠正**。
- `artifacts.py`（92）：内容寻址，写入即收据。
- `evidence.py`（206）：磁盘文件 → 带 hash 的锚定观测，只读。
- `connectors.py`（585）：网络访问的唯一合法通道，模型只报 source 名。
- `workspace.py`（346）：跨领域共享工具（ingest / search / working_paper / cases / facts）。

**`finance/`（参考负载，7 个只读工具）：**

`fixtures.py` 造出三个真实障碍（单位、口径、重述）→ `authority.py` 三个权限恒假 → `planner.py` 给蓝图 → `tools.py` 注册工具 → `facts.py` 只从已提交观测里做确定性抽取 → `summaries.py` 压缩成决策字段。

**`general/`（默认入口，写类能力都在这里）：**

`world.py` 沙箱 + 源 → `sandbox.py` 路径 jail + git 版本 + hash 收据 → `project.py` 每次 run 拿独立副本，交付用基线 diff → `commands.py` 白名单终端（不是 shell）→ `fetch.py` SSRF 防护 → `planner.py` 三种蓝图（research / script / open）→ `recall.py` 回取被折叠的旧观测 → `ask.py` 停下来问人。

**`mcp/`：** `client.py` 是 stdlib 的 stdio JSON-RPC 客户端；`bridge.py` 把外部工具变成一等 `ToolSpec`，参数照样过闸；`config.py` 读主机白名单。

### 4.4 `providers/`

| 文件 | 行 | 职责 |
|---|---:|---|
| `base.py` | 59 | 提案源接口，刻意窄：收到上下文，返回动作提议 |
| `mock.py` | 172 | 确定性离线模型，验证的是"模型输出形状"而不是智能 |
| `openai_compat.py` | 815 | 统一传输：所有 `/v1/chat/completions` 兼容端点 |
| `kimi.py` / `deepseek.py` / `local.py` | 231/162/160 | 三家预设；DeepSeek 走"外部计费"纪律 |
| `oracle.py` | 128 | 文件驱动的"人类/另一个模型"替身，实验用 |
| `local_policy.py` + `packet_prompt.py` | 136/91 | 小模型只做候选选择（Executive–Policy 的模型侧） |
| `prompt.py` | 336 | **唯一的提示词契约**，同时供运行时、SFT 导出、评测三方复用 |
| `catalog.py` | 278 | 服务端枚举可用模型，前端不再硬编码 |
| `deepseek_config.py` | 253 | 主机侧配置解析（环境 > 用户设置 > 默认） |

### 4.5 `memory/` — 全部是投影

| 文件 | 行 | 回答的问题 |
|---|---:|---|
| `context.py` | 597 | "模型这一轮该看到什么"——派生、有 token 预算、可重建 |
| `cases.py` | 450 | "以前做过什么"——终态 trace 变成可搜索案例簿 |
| `facts.py` | 289 | "我们曾经相信什么"——双时态，更正靠追加 |
| `artifacts.py` | 226 | "现在存在什么、谁产出的"——跨任务工件事实层 |
| `summary_cache.py` | 88 | 增量摘要缓存；度量后默认关闭（点查比重算更慢） |

### 4.6 `domains/`

`base.py` 的 `DomainModule` 是整个仓库最像"框架"的地方：一个领域要提供 `create_world` / `build_tools` / `make_planner` / `verify` / `result_tools` / `approval_tools` / `summarize_state` / `prompt_profile` / `plan_extension` / `make_verification`。**内核从不 import 具体领域**，新增业务 = 新增模块 + 注册。

### 4.7 `flowaudit/` — 审计产品域

| 文件 / 目录 | 职责 |
|---|---|
| `vulns/engine.py` | AGI 九类漏洞分类法 + 规则引擎；规则是数据（`audit-rules/*.json`），引擎只解释 |
| `poc/replay.py` + `replay_worker.py` | PoC 在受限子进程重放：剥离凭据、`HOME`/`TMPDIR` 指向审计目录、`sys.addaudithook` 拦 socket/subprocess |
| `poc/scenarios/*` | 内置场景（含 fcode-self 的真实缺陷与负对照） |
| `targets/repo.py` + `targets/rea.py` | 已实现的两个目标适配器：源码树（repo）与交付物（REA artifact）；desktop-app / http / mcp 仍在 `__init__.py` 里标注为后续计划 |
| `rea/` | REA CLI 适配与 allow-list 事实投影；原始 Evidence 与 facts 按 SHA-256 固定，供规则与宿主 oracle 使用 |
| `analyst.py` / `online.py` | 模型只做候选提议与分诊；在线调用受 `config/online-policy.json` 额度约束 |
| `report/` + `bundle.py` | 发现 + 审计自身的哈希链；审计包可独立 `verify-bundle` |
| `session.py` / `episodes.py` | 静态 / 动态分会话，共享 `audit_id` 与不可变目标清单 |
| `spec_gap.py` | 规格缺口报告——缺失的证据本身也是发现 |
| `jsonschema_lite.py` | 零依赖 schema 校验器 |

边界：`flowagent` 从不 import `flowaudit`，由 `tests/test_module_boundary.py` 的 AST 测试强制；反向 22 条 import 全部是 `flowaudit → flowagent`。

### 4.8 外壳

- `demo/runner.py`（670）：**全仓唯一装配点**。选择 provider、domain、policy、store、recorder，然后交给 `Loop`。往下所有模块都只接受注入。
- `demo/fixtures.py`（244）：8 个场景 = 同一内核 + 不同 host 策略 + 不同故障注入。
- `demo/__main__.py`（265）：CLI；每个 flag 要么工作，要么明确说明为什么不能。
- `demo/render.py`（238）：ANSI 渲染，本身也是一个 observer，**只打印不决定**。
- `demo/{local,deepseek,oracle,api}.py`：真实模型实验入口，与 demo 共用内核。
- `benchmark/tasks.py`（293）：19 个任务，动作是**模型提案**而不是直接工具调用。
- `benchmark/verifier.py`（184）：从文件、进程、事件、收据独立判分，不信任被测运行的自述。
- `chat*.py`（1,809）：对话面；可选只读工具循环（ls_workspace/read_file，经同一权限门与预算），写操作仍走提案转委托；全部计预算、进事件链。

### 4.9 `viewer/`

**后端（3,852 行，按职责拆得比较干净）：**

| 文件 | 行 | 职责 |
|---|---:|---|
| `server.py` | 802 | HTTP + SSE + 认证/同源/mutation guard；`do_POST` 只剩 12 行分发；新增只读审计 API 与 CSP |
| `console_manager.py` | 743 | 运行历史、创建、恢复、复核、`runs/` 目录 |
| `console_runtime.py` | 972 | 运行时宿主：导入 flowagent、表示一次 run、挂 observer、检查崩溃 |
| `console_run_driver.py` | 393 | 单次运行的装配 → 执行 → 发布 → 清理 |
| `console_post*.py` | 586 | 路由表 + chat 处理器 + run 处理器 |
| `console_common.py` | 243 | 路径、静态守卫、模型目录（可单测） |

**前端（14,392 行）：**

`app.js` 5,903 行仍是主入口（作者自己的重构文档承认"部分解决"）；`app/*.js` 是拆出来的视图模块（chat-ui / task-thread / review / run-evidence 等），`core/*.js` 是 15 个零依赖组件（SSE 状态、键盘、run-card、侧栏等），`audit/audit-view.js` 承载审计台，`tests/` 183 项。

---

## 5. 四条执行路径（同一个内核）

```
A. 离线演示
   demo.py → demo/__main__ → provider_factory → demo/runner
     → Loop → Harness → PermissionGate → Budget → Tool → Store
     → Recorder(observer) → work/demo/<scenario>.trace.json

B. Work 控制台
   viewer/server.py → console_post → console_manager
     → console_run_driver → console_runtime（import flowagent）
     → 同一个 Loop；事件经 SSE 推给前端；前端用 crypto.subtle 重算哈希

C. 评测
   benchmark/__main__ → runner → benchmark/runtime（适配器只喂提案）
     → 同一个 Loop → verifier（从文件/进程/事件独立判分）

D. 审计
   python audit.py audit <target>          （等价于已安装后的 python -m flowaudit.cli）
     → targets/ 生成目标清单 → vulns/ 规则引擎出候选
     → poc/ 生成 PoC → replay_worker（受限子进程）→ 宿主 oracle 判定
     → report/bundle：发现 + 审计事件链 → work/audit/<audit_id>/
     → flowaudit.cli verify-bundle：第三方独立复核
```

**这是整个仓库最好的架构决策：四个外壳共用同一个 `Loop`，没有为 demo、UI、benchmark、审计各写一套执行器。** 判断一个 agent 项目是否"真框架"，看这一点通常就够了。

---

## 6. 数据结构与事件模型

核心对象（`core/contracts.py`）：

```
Task ──< Plan ──< Step/Todo
  │
  ├── ModelState（模型可见的派生状态）
  ├── sequence / body_hash / head_hash（CAS + 链头）
  └── binding（冻结的 delegation spec，模型不可写）
```

事件链的每条记录：

```json
{"sequence": 1, "type": "task_created", "at": "...",
 "payload": {...}, "prev_hash": null, "hash": "e142..."}
```

事件类型覆盖：`task_created` / `round_*` / `proposal` / `gate` / `tool_started` / `tool_succeeded` / `tool_failed` / `observation` / `budget` / `verification` / `review` / 终态迁移。

由此**同一份日志**免费得到四件事：审计、重放、崩溃恢复、篡改检测。它们不是四套机制，而是四个投影——这是理解 `runtime/` 的钥匙。

---

## 7. 依赖实测：声明 vs 事实

声明是单向：`core → tools / providers / memory → runtime → surfaces`。

当前只剩 1 处反向边：

| 违规 | 位置 | 问题 | 建议 |
|---|---|---|---|
| 训练管线 → 演示层 | `sft/export.py:35`、`sft/evaluate.py:17-18` | 依赖 `demo.fixtures`、`demo.packet` | 场景定义移到 `domains/` 或独立 `scenarios/` |

原本的 `memory/ ↔ tools/` 循环、`providers → runtime`、`tools → runtime` 已消掉，做法是把跨层类型下沉到 `core`：

| 下沉到 core | 新位置 | 消除了哪条反向边 |
|---|---|---|
| `Observation` / `SourceTrust` / `VALID_SUMMARY_POLICIES` | `core/observation.py` | `memory → tools` |
| `ActionCandidate` / `ActionChoice` / `DecisionPacket` | `core/packets.py` | `providers → runtime` |
| `VerificationObligation` / `build_obligation` | `core/verification.py` | `tools/general → runtime` |
| `CaseSearchPort` / `FactQueryPort` / Null 实现 | `core/ports.py` | `tools/workspace → memory` |
| `make_fact` / `logical_key` / `fact_dates` | `core/facts.py` | `tools/finance → memory` |

`tools/finance/facts.py` 的 CLI `main()` 仍有一个函数内 `FactStore` import（只用于把结果写库），模块导入期不构成边。

包际方向是干净的：`flowaudit → flowagent` 共 22 条，`flowagent → flowaudit` **0 条**，由 `tests/test_module_boundary.py` 的 AST 扫描强制。也就是说审计域没有污染内核——它只是把内核当平台用。

---

## 8. 结构债清单（按严重度）

| 级别 | 问题 | 证据 | 影响 |
|---|---|---|---|
| P1 | **包根平铺 10 个模块**（chat* 5 个、intake、pacing、review* 2 个），不属于任何层 | `src/flowagent/*.py` 共 2,482 行 | 新人无法从目录判断职责；依赖方向声明也没覆盖它们 |
| P1 | **两套 `desktop/`，功能互有缺失** | 根：release 打包 + token 鉴权；`flowcredit/desktop/`：冒烟测试 + provider 环境隔离。451 行差异 | 用户不知道该改哪套；两份 README 各自指向自己那份 |
| P2 | **`sft/ → demo/` 反向边** | `sft/export.py:35`、`sft/evaluate.py:17-18` | 训练管线无法脱离演示层单独复用 |
| P2 | **`tools/workspace.py` 与 `tools/general/` 职责重叠** | 共享工具 vs 通用域工具，靠约定区分 | 新增领域时不知道工具该放哪 |
| P2 | **`app.js` 仍 5,903 行** | 作者自己的 `docs/refactor-evaluation.md` 写"部分解决" | CSS 已收敛（9→6 文件），JS 拆分待做 |
| P2 | **审计目标适配器覆盖仍不完整** | 已落地 `repo` 与 `rea`（artifact）；desktop-app / http / mcp 仍在 `targets/__init__.py` 标注为计划 | 对外表述要与适配器矩阵一致；REA 只覆盖静态制品事实，动态/远程目标仍缺 |
| P3 | **生成物入库** | `flowcredit/artifacts/` 388 个文件 | clone 体积；但作为 benchmark 证据可以接受 |
| P3 | **`archive/` 1.7 MB** | 旧 Node 原型 | 历史价值 vs 仓库重量 |

---

## 9. 建议的目标结构

不改任何行为，只调整归属（括号内为现有文件）：

```
src/
├── flowagent/       内核包（域无关）
│   ├── core/        contracts(hashing/store/receipts/plan) + observation/decision 契约
│   ├── kernel/      runtime/*                        ← 现 runtime/
│   ├── capabilities/ registry/base/artifacts/evidence/connectors/workspace/mcp
│   ├── domains/     base + finance/ + general/
│   ├── providers/   base + mock + openai_compat + presets
│   ├── memory/      context/cases/facts/artifacts（只依赖 core）
│   └── surfaces/
│       ├── demo/    runner/fixtures/render/cli
│       ├── chat/    chat/chat_answer/chat_config/chat_provider/chat_usage
│       ├── console/ viewer/*.py（后端）
│       ├── benchmark/
│       └── sft/
└── flowaudit/       审计域（只依赖 flowagent，反向有 AST 测试）
    ├── vulns/ · poc/ · targets/ · report/
    └── session / episodes / spec_gap / bundle
audit-rules/         JSON 规则库
fixtures/            demo-target / fcode-self
scenarios/           8 个 demo 场景 + 19 个 benchmark 任务的共享定义
```

三条硬规则：

1. `core/` 不 import 包内任何东西；`Observation`、`DecisionPacket` 这类跨层类型必须住在 `core/`。
2. 只有 `surfaces/*` 可以 import `kernel`；`kernel` 不能 import 任何 surface。
3. 装配只允许发生在 `surfaces/*/runner.py` 和 `domains/register.py`。

---

## 10. 对照"完整 agent 框架"的差距

| 层 | 本项目 | 说明 |
|---|---|---|
| 模型层 | ✅ 完整 | Provider 抽象 + 4 类预设 + 统一传输 + 提示词契约 |
| 能力层 | ✅ 完整 | 封闭注册表、schema、MCP、工件、证据、沙箱 |
| 运行时 | ✅ 强 | 门控、预算、收据、恢复、重放、独立验证——**全仓最有价值的部分** |
| 状态层 | ✅ 完整 | 事件溯源 + SQLite + 哈希链 + CAS |
| 观测层 | 🟡 偏弱 | 有 trace/replay，但缺真实模型的指标与回归基线 |
| 治理层 | 🟡 部分 | 权限/预算/审批/收据齐；**无 OS 级沙箱**、无多租户 |
| 领域插件 | ✅ 有契约 | `DomainModule` 是真正的扩展点；`flowaudit` 用独立包验证了它 |
| 安全审计域 | ✅ 已合入 | AGI 九分类法 + 可重放 PoC + 宿主 oracle + 可复核审计包；目标适配器目前只有 repo |
| 编排 | ❌ 缺 | 多 agent 只在 `labs/multi_agent/`，未进主路径 |
| 评测 | ❌ 缺关键环 | 19 任务全离线脚本；`ONLINE_MODEL: NOT_RUN` |
| 部署 | ❌ 缺 | 单机 SQLite、无队列、无多副本 |
| 上下文工程 | 🟡 有思路 | 派生上下文 + 折叠 + recall，但没有检索 |

**结论：这是一个 8/10 的 runtime，4/10 的 framework。审计域证明了插件契约真的能用，但没有补上编排、真实模型评测和部署；目录结构比大多数同类项目清楚，主要问题仍是"层名对了但边界没焊死"。**

---

## 附录 A：审计域 `flowaudit`（已随 PR #4 合入）

PR #4：34 commits，+9,120 行，CI（Ubuntu/macOS × 3.11/3.13）四组全绿后合入。

它**不改变内核分层**，而是在领域层加了第三个域，而且是一个独立包：

```
flowaudit/
  vulns/      AGI 九类漏洞分类法 + 规则引擎
  poc/        detect → generate → 独立重放 → finding
              （replay / replay_worker / 场景，含 rea_fact_evidence）
  rea/        REA CLI 适配 + allow-list 事实投影（原始 Evidence 固定）
  targets/    repo 源码树 + rea 交付物（desktop-app / http / mcp 为计划）
  report/     发现 + 审计自身的哈希链
  session.py  静态 / 动态分会话，共享 audit_id
  spec_gap.py 规格缺口报告
  bundle.py   审计包导出与第三方复核
  online.py   在线策略
  jsonschema_lite.py  零依赖 schema 校验
audit-rules/  JSON 规则即数据（index + schema + examples）
fixtures/     demo-target（MiniAgent 靶子）/ fcode-self（7 个自体缺陷 ground truth）
viewer/       audit/audit-view.js(统一壳 #/audit surface) / audit_jobs.py
docs/         audit-methodology · capability-map · threat-model
              · rule-authoring · integration-rea · sprint2-acceptance · tech-debt
```

目标依赖方向（PR 描述称由 AST 测试强制）：

```
flowagent（内核） ← flowaudit（产品域） ← audit-rules / fixtures（数据）
```

四条不变量：发现 = PoC 重放通过（宿主 oracle 判定）；FAIL 按三值区分（未复现 / harness 缺陷 / 范围内不成立）；目标内容不可信（PoC 能力清单 + 宿主校验，越界即中止）；审计事件自身进哈希链。

**对内核的影响**：PR 同时改动了 `tools/workspace.py`、`tools/registry.py`、`memory/context.py`、`runtime/harness.py`、`runtime/loop.py`，都是加法且 fail-closed（观测加 `trust` 标记、旧事件缺省 `EXTERNAL_UNTRUSTED`、摘要策略非法则拒绝注册）。§7 的依赖边与 §8 的条目已按合入后的工作树重跑。从分层角度看，审计域没有破坏内核，反而验证了 `DomainModule` 这个扩展点是够用的。

**它自己承认的边界**（`docs/sprint2-acceptance.md` 是历史验收记录）：Sprint 2 已收尾并进入 REA 集成 MVP；目标适配器为 `repo` + `rea`（artifact），desktop-app / http / mcp 仍计划；审计台 UI 已落地；在线模型只在审计域内、受 `online-policy.json` 额度约束。

---

## 附：一分钟自查

```bash
cd flowcredit
python3.12 -m unittest discover -s tests -q     # 791 tests, OK
node --test viewer/tests/*.test.js              # 183 pass
python3.12 demo.py all                          # 8 场景全过
python3.12 audit.py audit fixtures/demo-target
PYTHONPATH=src python3.12 -m flowagent.benchmark --output /tmp/bench
```