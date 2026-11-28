# General-Agent 通用任务评测 + Work 入口意图分流评测

> 状态：当前权威。本文描述 `src/flowagent/evaluation/` 的通用任务评测套件
> （`general-agent-benchmark/v1`）与 Work 入口意图分流评测（intent-router）。
> Runtime 治理机制的离线基准见 [runtime-benchmark.md](runtime-benchmark.md)；
> Work coding 专项基准见 [work-coding-benchmark.md](work-coding-benchmark.md)。
> 最后核对：2026-11-22。

## 1. 它回答什么问题

`runtime-benchmark/v1`（19 项）证明的是**治理机制**：门控、预算、审批、恢复、
审计完整性在真实 Runtime 路径上生效。它不证明、也不能被解读为模型能力。

`general-agent-benchmark/v1`（27 项）回答另一个问题：**同一套生产 Runtime
（Loop/Harness/门控/预算/审批/收据/恢复）之上，通用任务能不能被真正完成**——
读文件、改代码、跑测试、交付产物、引用来源、诚实停止。两类指标永远分开报告。

## 2. 运行

```bash
cd flowcredit
PYTHONPATH=src python -m flowagent.evaluation --list          # 任务清单 + SHA256
PYTHONPATH=src python -m flowagent.evaluation                 # 离线全量（默认）
PYTHONPATH=src python -m flowagent.evaluation --suite intent  # 入口分流评测
PYTHONPATH=src python -m flowagent.evaluation --task coding-fix-off-by-one --repeat 2
PYTHONPATH=src python -m flowagent.evaluation --online --provider deepseek \
    --confirm-paid "I authorize paid online evaluation for this run"
python -m unittest tests.test_general_eval tests.test_intent_router tests.test_router_eval -v
```

默认输出 `work/general-eval/latest`（intent 评测在 `work/intent-router-eval/latest`）。
每次生成替换目标目录；逐任务独立工作区、canary 与 SQLite 事件库。

## 3. 执行类别

| 类别 | 提议来源 | 用途 |
| --- | --- | --- |
| `OFFLINE_SCRIPTED` | 每个任务自带的**参考提议**（`model_input.actions`），经确定性 ScriptedProvider 重放 | 校验评测器自身：参考提议必须把生产 Runtime 驱动到独立 verifier 通过的终态 |
| `ONLINE_MODEL` | 真实 Provider（`--provider` 显式选择） | 真实模型任务效果；同样的门控、预算、审批、事件链与 verifier |

在线模式的 fail-closed 门（`check_online_gate`）：

1. `--online` 必须伴随 `--confirm-paid`，其值包含
   `authorize paid online evaluation`；
2. Provider key/model/价格快照（input/output µUSD/MTok + as_of）缺一即拒；
   `mock` 永远不允许作为在线模型；`local` 免价格但必须真实可达；
3. 任一条件不满足：输出 `ONLINE_MODEL=NOT_RUN`、`真实模型通过率 = NOT_MEASURED`，
   退出码 3，**不会**静默降级为离线或 mock；
4. 真实调用失败的任务按失败记录，不重试、不换模型、不伪造成功。

## 4. 任务集（27 项，`general-agent-benchmark/v1`）

| 类别 | 数量 | 覆盖 |
| --- | ---: | --- |
| coding | 6 | 单文件修复、多文件一致性、失败测试修复、项目约定、跨文件重命名、回归守护 |
| file_delivery | 5 | 汇总报告、局部编辑、JSON 产物、hash↔交付一致性、CSV 过滤排序 |
| research | 3 | 本地静态 HTTP fixture 抓取、双来源冲突仲裁、原文引用证据 |
| multi_step | 4 | 读→改→验证→交付、构建→验证、顺序依赖、瞬时失败有界重试 |
| recovery | 3 | unknown 后不重复写入、SIGKILL 后对账、提交后回放续跑（复用 benchmark 的真实 SIGKILL worker） |
| adversarial | 3 | README / .env / 工具结果中的提示词注入不得覆盖宿主规则 |
| negative | 3 | 输入缺失、证据被抹除、工具未授权——必须诚实 `report_blocked`，不得伪造产物 |

每项任务包含：唯一 ID、集合版本、类别、存在理由（rationale）、初始 fixture、
用户目标、可用工具与效果授权、预算与超时、独立 verifier 期望、禁止副作用、
离线/在线适用范围、定义 SHA-256（`digest(definition)`）。任务定义变更必须
在 `general_tasks.py::GENERAL_TASK_CHANGELOG` 记录原因，并重新生成全部产物。

研究类任务的抓取目标是评测进程内启动的 `127.0.0.1` 静态 fixture；离线网络
guard 放行该端口、拒绝其余一切连接并计数。恢复类任务通过
`benchmark.crash_worker`（`BENCHMARK_TASKSET=general`）复用真实 SIGKILL 注入。

## 5. 独立 verifier 与指标

`evaluation/verifier.py` 的 `verify_general_task` 只看宿主可复核的事实：
文件逐字内容 / 包含 / 不包含、JSON 解析与字段、`sha256_manifest` 重算、
独立命令退出码与 stdout、治理事件、动作顺序、exit-code 序列、恢复对账、
canary、网络尝试计数、forbidden 动作扫描（注入规则）、副作用与重复副作用计数。
**模型自报永远不是指标。**

指标（`evaluation/metrics.py`，分母显式，空分母 = `NOT_MEASURED`）：

| 指标 | 分母 |
| --- | --- |
| `goal_success_rate` | 全部 run |
| `independent_verifier_pass_rate` | 全部 run |
| `pipeline_completion_rate` | 全部 run |
| `invalid_action_rate` | 全部 run（发生 forbidden/被拒动作的 run 占比） |
| `recovery_success_rate` | 仅注入了恢复场景的 run |
| `unauthorized_side_effect_count` / `duplicate_side_effect_count` / `human_intervention_count` | 绝对计数 |

另报告：calls/reads/tokens/cost 合计、P50/P95 延迟（整数毫秒，最近邻秩）、
按任务/类别/Provider/模型分组的 badcase、失败类型分布。`--repeat N` 保留每次
原始 run（`tasks/<id>/repeat-<n>/`），单次运行不构成统计结论。

## 6. 报告与脱敏

`summary.json` / `summary.md` 记录：被测 commit、dirty 状态、任务集版本与
SHA-256、Provider、模型、价格快照、时间、环境、逐任务结果与 artifacts 指针
（events/receipts/diff/verifier/policy/budget）。API key（`*_API_KEY`、`sk-*`）
在写入报告前统一替换为 `[REDACTED]`。

离线报告固定包含：

```text
ONLINE_MODEL=NOT_RUN
真实模型通过率 = NOT_MEASURED
```

## 7. Work 入口意图分流（Task B）

入口三分流（chat / clarify / work）的规则在两个实现里逐条对齐：

- Python（评测与 API 的单一事实源）：`src/flowagent/intent_router.py`；
- 前端零延迟镜像：`viewer/core/intent-router.js`；
- 共同的版本化 fixture：`fixtures/intent-router/cases.json`（41 例），
  Python unittest、Node test 与评测器三方钉死，防漂移。

决策契约（JSON 稳定形状）：

```json
{"route": "chat|clarify|work", "via": "deterministic|model|fallback",
 "reason": "...", "certainty": "high|low|model",
 "model": null, "model_confidence": null, "classifier_error": null}
```

分流语义：

- **chat**：问候、知识问答、解释、讨论——进真实 Chat，不建 Run；
- **clarify**：有执行倾向但缺关键对象/路径/来源/交付条件（"解释完后直接修复"、
  无上下文的"继续刚才那个修改"）——停在任务确认页，不建 Run；
- **work**：明确、可执行、可交付——保持提交即开跑的既有体验。

护栏（全部测试锁定）：

1. 高置信输入走确定性规则，问候零模型调用；
2. 仅 `certainty=="low"` 的残余桶会（由前端）请求 `POST /api/intake/route`；
3. 语义分类器默认关闭：需要 `FCODE_INTENT_CLASSIFIER=1` 且请求带
   `classify:true`；它使用默认对话模型（`ChatService.router_provider`，
   **mock 永不伪造分类**），不走其他模型，不静默切换；
4. 分类器失败/缺位/输出不合契约 → `via:"fallback"`，落到确定性结果
   （chat/clarify），歧义输入绝不因故障自动变成 Run；
5. 路由结果不携带、也不改变任何工具授权；
6. 服务不可用时前端退回 clarify。

旧端点 `/api/intake/classify` 之前 import 了不存在的
`classify_composer_text`（请求即 500），现改为由 intent router 回答，兼容形状
`{"intent": "chat"|"task"}` 保留。

### 分流评测

```bash
PYTHONPATH=src python -m flowagent.evaluation --suite intent
```

指标：`accuracy`、`chat_misrouted_to_run`（chat 误创建 Run）、
`work_missed_into_chat`（work 误落入 Chat）、`clarify_expected_recall/share/precision`、
`safe_fallback_rate`（低置信/回退永不变成 Run）、分类器额外延迟与调用数
（确定性路由为 0）。当前基线：41/41 正确，chat→run 0，work→chat 0，
safe fallback 100%，分类器额外调用 0。

## 8. 边界

- 离线通过率证明评测器自洽，**不是**模型能力；两者不得互相解释。
- 在线评测必须在操作员显式付费授权下进行；无授权时的数字只有 NOT_RUN。
- 恢复任务的边界与 [runtime-benchmark.md](runtime-benchmark.md) 相同：应用层
  工作区/事件对账，不是操作系统级沙箱或任意外部系统回滚。
- 分流规则是启发式，不是语义保证；clarify 桶宁可多问，不冒进建 Run。
