<div align="center">
  <img src="viewer/brand/logo.svg" alt="FCode" width="72" />

  # FCode

  ## Build your own work.

  **让模型提议，让主机决定。**

  一个本地优先、可恢复、可审计的 Agent Runtime。

  `MODEL PROPOSES · HOST VERIFIES · YOU DECIDE`

  <img src="viewer/docs/desktop-home.png" alt="FCode Work 控制台" width="100%" />
</div>

---

FCode 把目标变成一段可检查的工作过程：模型负责理解与提议，Runtime 负责计划、权限、预算、执行、验证与恢复，人始终保留关键决定的控制权。

Python 包名为 `flowagent`。金融研究是第一个示例域，运行内核本身不绑定行业。

## 不是更会回答，而是更能负责

一个 Agent 说自己完成了，并不等于工作真的完成。

在这个内核之上，FCode 构建了审计域 `flowaudit`——审计其他 agent 应用的 agent，见仓库首页与 [能力图谱](docs/capability-map.md)。

FCode 更关心另一组问题：它读了哪些证据，调用了什么工具，为什么被允许执行，失败后能否恢复，结果能否由独立过程复算。可信不来自模型的自述，而来自可验证的运行记录。

### 三条约束

**模型不掌权**

工具来自封闭注册表。参数先通过 schema 校验，再同时匹配任务范围与主机授权；模型不能绕过门控，也不能自行宣布成功。

**失败不乱来**

调用在发送前计入预算。结果未知的请求不盲目重发，进程崩溃后从事件日志重建状态，已完成的轮次不重复执行。

**全程可审计**

审批、工具调用、观测、预算与人工干预进入同一条哈希链。读取时重新计算链，任何篡改都会暴露，而不是只给日志染一个颜色。

## 四个核心，一条责任链

| 核心 | FCode 的实现 | 当前边界 |
| --- | --- | --- |
| **大脑 / Brain** | Provider 抽象；支持 mock、本地与 OpenAI 兼容模型；模型输出动作提议 | 模型能力不等于任务已被验证 |
| **规划 / Planning** | 目标分解、todo、轮次循环、计划跳过与提前、失败停止 | 当前以单 Agent 的计划执行为主 |
| **记忆 / Memory** | SQLite 事件日志、上下文重建、运行历史、断点续跑 | 不是无限记忆，也不等于跨任务知识系统 |
| **工具 / Tools** | 封闭注册表、参数校验、权限门控、预算控制、签名收据 | 受限命令不是操作系统级沙箱 |

```text
Model proposal → Schema → Tool registry → Permissions / Budget / Approval
                                                              │
                                                              ↓
SQLite events ← Independent verifier ← Receipt ← Tool execution
      │                                                        │
      └──────────── Crash recovery / read-only replay ─────────┘

The model proposes. The host gates, executes and verifies.
```

Runtime 不只记录“做了什么”，还记录“为什么允许做”。独立 verifier 可以重算领域结果；验证不通过，运行就不能被伪装成成功。

## 启动 FCode

需要 **Python 3.11+**。离线演示零第三方依赖、零配置。

```bash
git clone https://github.com/Anhao1314/FCode.git
cd FCode/flowcredit
python demo.py
```

终端会显示场景菜单；无交互终端时直接运行完整故事。

### 打开 Work 控制台

在 `flowcredit` 目录执行：

```bash
python viewer/server.py --repo . --port 8642
```

打开 `http://127.0.0.1:8642/`。控制台包含 Work、Chat、任务与运行历史、审批、轨迹回放和运行控制。

### 一条线程，看清工作

Work 把执行过程组织成 **任务 → 状态／计划 → 工具与审批 → 最终回复**，不再用互相割裂的卡片拼接进度：

- **同一事实，两种视图**：实时运行与历史重开共用统一线程投影；普通项目和完整研究使用相同的阅读结构。
- **结果可以展开核对**：工具默认保持一行摘要，展开后查看参数、命令、真实 diff、原始输出与签名收据。diff 来自沙箱的实际前后内容，不由前端推测。
- **审批属于具体一轮**：提交后只禁用当前审批并显示等待状态，下一轮审批不会被旧状态卡住；`?run=` 与 `?trace=` 深链始终进入 Work。
- **项目交付保持隔离**：普通项目在独立副本中执行，交付 `FCODE_RESULT.md`，通过独立验证后可切换执行、变更和文件视图。
- **Chat 连续对话不串线**：流式回答按线程与世代隔离；完成或停止后立即发送下一条，不会被旧回读覆盖，分页与归档也不会带回失效列表。

真实模型接入与 Provider 配置见 [Kimi 接入说明](docs/kimi.md)。密钥只放在本机环境或本地配置中，不要提交到仓库。

### 构建桌面应用

```bash
bash desktop/build.sh
```

当前已实际验证 **macOS arm64** 构建。其他平台需要对应环境重新构建与验收，不能由一次 macOS 构建推定可用。

## 八个可复现场景

```bash
python demo.py full          # 完整研究：多轮取证、口径桥接、独立复算
python demo.py tool-denied   # 越权工具：主机拒绝，工具不执行
python demo.py budget        # 预算耗尽：发送前拦截
python demo.py budget-resume # 追加预算：失败后续跑
python demo.py crash         # 真实 SIGKILL：恢复后不重复轮次
python demo.py unknown       # 结果未知：不重发，凭收据对账
python demo.py open-ended    # 证据不足：诚实停止
python demo.py general-research # 通用研究：开放目标与证据整理
python demo.py all           # 顺序运行全部场景并自检轨迹
```

这些场景不是八套硬编码流程。它们复用同一个 Runtime，通过不同主机策略与故障注入，检验权限、预算、恢复、验证与停止纪律。

每次运行都会生成 `work/demo/<scenario>.trace.json`。可以阅读原始事件，也可以在控制台回放轨迹并验证哈希链。

## 人如何介入

控制发生在执行链中，而不是运行结束后的解释里。

- **审批**：通过门控后、工具执行前暂停；拒绝终止运行，超时自动拒绝。提交状态绑定审批 id，不会泄漏到下一轮。
- **计划修订**：跳过或提前计划项，在下一轮边界生效。
- **预算调整**：失败后追加预算并续跑，已支付的读取不重复发送。
- **运行中消息**：在轮次边界进入上下文，并写入同一条事件链。
- **判断记录**：将授权、阻塞、恢复与关键决定呈现为可检查的工作历史。

## 验证，而不是感觉

```bash
python -m unittest discover -s tests -q
node --test viewer/tests/*.test.js
python demo.py all
PYTHONPATH=src python -m flowagent.benchmark --output work/runtime-benchmark/latest
```

当前完整回归：**679 项 Python 测试（含审计域 flowaudit 的契约与端到端测试）、166 项前端测试、8 个端到端场景、19 个版本化 Runtime 任务**。数字口径与逐条复现命令见 [能力图谱](docs/capability-map.md)。原生桌面验收另覆盖真实项目交付、连续审批、工具 diff／命令输出，以及实时与历史线程一致性。

任务基准的离线实测为：独立 verifier `18/19`（保留一个预置错误交付）、崩溃恢复 `3/3`、未知结果安全处理 `3/3`、回放等价 `18/18`、篡改检测 `1/1`，越权与重复副作用均为 `0`。正常链和 SIGKILL 证据见 [任务基准](docs/runtime-benchmark.md)；这不是实际模型成功率，本轮 `ONLINE_MODEL=NOT_RUN`。

测试覆盖权限与参数门控、预算、状态机、并发 CAS、事件篡改、收据伪造、独立重算、审批与计划控制、崩溃恢复、Provider 纪律，以及真实 HTTP 服务边界。CI 使用 Ubuntu／macOS × Python 3.11／3.13 矩阵。

## 诚实的边界

- FCode 当前是治理较强的**单 Agent Runtime**，不是产品级多 Agent 集群。
- 本地工具与命令有应用层约束，但不构成操作系统级安全沙箱。
- Chat 附件目前以文本注入为主，不等于完整多模态理解管线。
- Chat 回答拥有事件链与收据，不代表每个回答都经过领域 verifier 验证。
- 恢复只覆盖当前工作区、事件与收据对账，不能恢复任意外部副作用；通用任务仍受模型能力与可验证性限制。

这些边界不是附注，而是产品设计的一部分：知道何时不能完成，和完成工作同样重要。

## 继续深入

| 文档 | 内容 |
| --- | --- |
| [架构](docs/architecture.md) | Runtime、状态、存储与执行链 |
| [任务基准](docs/runtime-benchmark.md) | 19 项任务、独立 verifier、指标与证据路径 |
| [演示脚本](docs/demo-script.md) | 正常链、审批拒绝、SIGKILL 与 verifier 拒绝 |
| [环境配置](docs/environment.md) | 离线运行、可选 Provider 与命令白名单示例 |
| [设计哲学](docs/philosophy.md) | 为什么把控制权交给主机与人 |
| [通用能力](docs/general-capabilities.md) | 从金融示例到领域无关内核 |
| [上下文管理](docs/context-management.md) | 记忆、压缩与事件重建 |
| [多 Agent 实验](docs/multi-agent-lab.md) | 多 Agent 方向的边界与探索 |
| [视觉系统](docs/design-system.md) | 黑白、留白、轨道与证据语言 |
| [桌面设计](docs/desktop-design.md) | Work 控制台的结构与交互 |
| [重构评估](docs/refactor-evaluation.md) | 技术债、模块边界与演进记录 |

## 项目结构

```text
src/flowagent/
  core/        合约、计划、哈希、SQLite 事件存储、签名收据
  runtime/     状态机、权限、预算、人工控制、循环与恢复
  providers/   mock、本地、Kimi、DeepSeek 与 oracle
  memory/      从事件日志重建模型可见上下文
  tools/       工具注册表与金融示例工具
  trajectory/  轨迹记录、导出与独立重算
  demo/        CLI、终端渲染与八个可复现场景
  benchmark/   版本化任务、独立 verifier、指标与真实运行证据
viewer/        Work / Chat 控制台、SSE、Trace 回放
  core/        前端纯函数与交互状态
  tests/       前端回归测试
desktop/       桌面应用入口与构建脚本
tests/         Python 单元、集成与端到端测试
docs/          架构、设计、能力与评估文档
```

---

<div align="center">

**If it cannot be verified, it is not done.**

FCode 是一个独立开源项目，不是 Moonshot AI 官方产品。

</div>
