<div align="center">
  <img src="flowcredit/viewer/brand/logo.svg" alt="FCode" width="72" />

  # FCode

  ## Audit the agents.

  **审计 agent 应用的 agent:每条发现附带可重放的 PoC,审计过程自身进哈希链。**

  `MODEL PROPOSES · HOST VERIFIES · YOU DECIDE`

  [![CI](https://github.com/Anhao1314/FCode-FlowCredit/actions/workflows/python.yml/badge.svg)](https://github.com/Anhao1314/FCode-FlowCredit/actions/workflows/python.yml)
  [![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

  <img src="flowcredit/viewer/docs/desktop-home.png" alt="FCode Work 控制台" width="100%" />
</div>

---

## 30 秒版本

- **问题**:agent 应用爆发式增长,而它们有一个传统软件没有的攻击面——**不可信内容会变成指令**。现有评测工具给出分数,却不给出能被第三方重新执行的证据。
- **做法**:一个治理级 Runtime 之上(哈希链事件日志、权限门控、预算、SIGKILL 崩溃恢复,全部 stdlib-only),构建审计域 `flowaudit`:AGI 九类漏洞分类法、规则即数据、PoC 在受限子进程中重放、**宿主侧 oracle 判定**、负对照必带、审计包可独立复核。
- **证据**:它审计了自己的仓库——端到端挖出 1 个真实缺陷(审批记忆参数无关免审批,带正反对照重放),7 个自体缺陷归档为 ground truth;一份 49 条意见的外部对抗评审被逐条裁决并合入契约(`docs/design-reviews/`)。
- **状态**:Sprint 2 + REA 集成 MVP(2026-11)。**791 项 Python 测试 + 183 项前端测试全绿**(CI 在 Ubuntu/macOS × Python 3.11/3.13 上重新推导)。

## 它和评分工具的区别

| | 评分型评测工具 | FCode |
| --- | --- | --- |
| 输出 | 分数、通过率 | **发现**:分类号 + 证据指针 + 可重放 PoC |
| 判定 | 模型说通过就算通过 | **宿主 oracle 读事件链**,PoC 自报无效 |
| 重放失败 | 常被记为通过或忽略 | 三值如实区分:未复现 / harness 缺陷 / 范围内不成立 |
| 审计自身 | 无 | 审计事件入哈希链,`verify` 可回放 |
| 依赖 | 通常重依赖 | **stdlib-only**,CI 断言 `dependencies = []` |

*(对比以公开了解为限,欢迎指正;竞品能力矩阵在持续核对。)*

## 快速开始

需要 Python 3.11+。离线演示零第三方依赖、零配置。

```bash
git clone https://github.com/Anhao1314/FCode-FlowCredit.git
cd FCode-FlowCredit/flowcredit

python audit.py audit fixtures/demo-target
# → 30 秒看到审计器工作:内置漏洞样例应用 MiniAgent,
#   2 条发现(审批记忆绕过 AGI-02 / 明文密钥 AGI-03),每条附可重放证据

python demo.py all                      # 8 个治理场景,全部离线
python audit.py audit .                 # 进阶:让审计器审计本仓库自己

# 没有源码的交付物(REA 制品证据)
python audit.py audit /Applications/EvoX.app \
  --adapter rea --declared-features "microphone"
```

打开 Work / Chat / 审计台:

```bash
python viewer/server.py --repo . --port 8642
# Work / Chat: http://127.0.0.1:8642/
# 审计台:      http://127.0.0.1:8642/audit.html
```

真实模型接入见 [Kimi 配置](flowcredit/docs/kimi.md);密钥只存本机环境,不进仓库。

## 工作原理

```text
Rule (AGI taxonomy) → Candidate finding → PoC → Constrained replay
                                                        │
                                                        ↓
Audit event chain ← Receipt ← Gated execution (budget / approval)
      │
      └──── "Who audits the auditor" = verify_chain, replayable ────┘
```

三层结构:**内核**(flowagent:哈希链、门控、预算、恢复)、**审计域**(flowaudit:分类法、PoC、目标适配器、报告,单向依赖由 AST 测试强制)、**数据**(JSON 规则库、已知缺陷靶子)。静态与动态分会话,共享 `audit_id` 与不可变目标清单。

四条不变量,每条都有测试背书:

1. **发现 = PoC 重放通过**,宿主 oracle 执行,模型与 operator 的意见都只是输入;
2. **FAIL 不是"不存在"**——`not_reproduced / invalid_poc / not_vulnerable_under_scope` 三值如实区分;
3. **目标内容永远不可信**——包括 Analyst 读完它之后写下的每一行 PoC(能力清单 + 宿主校验,越界即中止);
4. **审计器每天用自己的 19 任务基准自检**——标尺必须先证明自己没坏。

## 审计产品状态(诚实版)

- **已交付**:AGI 分类法 v1、审计方法论、被测试钉死的证据 Schema、垂直切片(见"证据")、外部评审裁决记录、审计台 UI(`viewer/audit.html` + 只读 `/api/audit/*`)、REA 制品证据适配器(`--adapter rea`:Go 构建信息 / bundle 解剖 / entitlements 对照 / sidecar 源码事实 / Mach-O 加载链 / 离线 OSV)、扁平 schema MCP wrapper、真实 REA CI lane。
- **Ground truth**:`fixtures/fcode-self/` 记录本仓库 7 个已确认缺陷(含 file:line 证据),划分为 dev 集(01/02/06)与**盲测集**(04/07,不进任何规则与示例)。**用自己当第一个靶子是刻意的。**
- **尚未实现**:动态探针全量、Jev 决策模型 shadow 接线、容器级沙箱、REA sidecar JS 图、真实干净对照、CVE/OSV 情报。审计台 UI 已交付,不再是设计稿;本文档不提前声称未落地的能力存在。

## 诚实的边界

- 治理较强的**单 Agent Runtime**,不是多 Agent 集群(多 agent 在 labs/)。
- 本地工具约束是**应用层**的;重放隔离是"审计钩子 + env 剥离 + 临时副本"强度,不是 OS 级沙箱,容器层在路线图。
- 审计分类法覆盖**已知模式**,报告会明示未覆盖面;发现面向修复,不面向武器化。
- 在线模型评测保持 `NOT_RUN` 直至操作者明确授权;双 API 已冒烟(deepseek-flash / jev-1.14.0)。
- Chat 回答有事件链与收据,不代表每个回答都经过领域 verifier 验证。

知道何时不能完成,和完成工作同样重要。

## 文档地图

| 方向 | 文档 |
| --- | --- |
| Runtime 完整说明 | [flowcredit/README.md](flowcredit/README.md) |
| 系统架构 | [architecture](flowcredit/docs/architecture.md) |
| 仓库全貌：分层、包职责、逐文件地图与结构债 | [architecture-map](flowcredit/docs/architecture-map.md) |
| **Agent 应用威胁模型(AGI 分类法)** | [threat-model](flowcredit/docs/threat-model.md) |
| **审计方法论与交互模型** | [audit-methodology](flowcredit/docs/audit-methodology.md) |
| **规则编写指南** | [rule-authoring](flowcredit/docs/rule-authoring.md) |
| **REA 制品证据融合** | [integration-rea](flowcredit/docs/integration-rea.md) |
| **能力图谱(逐条可复现)** | [capability-map](flowcredit/docs/capability-map.md) |
| **外部设计评审裁决记录** | [design-reviews](flowcredit/docs/design-reviews/) |
| 任务基准、指标与证据 | [runtime-benchmark](flowcredit/docs/runtime-benchmark.md) |
| 上下文与记忆 | [context-management](flowcredit/docs/context-management.md) |
| 技术债与重构记录 | [refactor-evaluation](flowcredit/docs/refactor-evaluation.md) |

### 文档同步约定

文档是产品状态的一部分，不做事后补丁。每次改代码后按这张表同步：

| 改动 | 必须同步 |
| --- | --- |
| 新增/修改目标适配器、CLI 参数 | 根 README、`flowcredit/README.md`、`architecture-map.md`、`integration-rea.md` 的适配器矩阵与命令 |
| 新增/修改规则 | `audit-rules/index.json` 的 `version`、规则表、`fixtures/` 靶子、`rule-authoring.md` |
| 修改测试数量或前端测试 | 根 README、`flowcredit/README.md`、`capability-map.md`、`architecture-map.md` 的数字 |
| 修改 viewer/审计台 | `viewer/README.md`、截图、API 表 |
| 新增“已交付/未交付”能力 | 根 README 的“审计产品状态”和 `capability-map.md` 的诚实清单 |

CI 会运行 `python flowcredit/scripts/check_docs.py`，检查相对链接、过时声明
（如旧测试数、已落地却仍写“未实现”的界面）和当前必须出现的入口
（`--adapter rea`、`audit.html`）。它不替代人工判断，但能阻止文档再次漂移。

## 项目结构

```text
flowcredit/
  src/flowagent/   Agent Runtime 内核:哈希链、门控、预算、恢复(域无关)
  src/flowaudit/   审计域:分类法、PoC 受限重放、目标适配器、报告
  src/flowaudit/rea/  REA CLI 适配 + allow-list 事实投影
  audit-rules/     版本化规则库 + 证据 Schema(数据,非代码)
  fixtures/        已知缺陷靶子(fcode-self = 自审计 ground truth)
  viewer/          Work / Chat / Audit 控制台、SSE、Trace 回放
  desktop/         桌面应用入口与构建脚本(macOS arm64 已验证)
  tests/           791 项 Python 测试;viewer/tests/ 183 项前端测试
archive/           v1 Node 原型(NO_GO 负结果,不参与 CI)
```

---

<div align="center">

**If it cannot be verified, it is not done.**
**An audit that cannot be replayed is not an audit.**

Apache License 2.0 · [LICENSE](LICENSE)

FCode 是一个独立开源项目,不是 Moonshot AI 官方产品。

</div>
