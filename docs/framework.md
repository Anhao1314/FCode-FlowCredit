# 框架 v3：Executive / Policy / Voice

**状态：下一版（未实现）** — 设计文档，不在当前运行时里。当前权威是 [architecture.md](architecture.md)；语言规范见 [voice.md](voice.md)；策略层现状见 [executive-policy.md](executive-policy.md) 与 `../sft/cloud/README.md`。最后核对：2026-10-26。

> Host 负责事实与权力，Policy 负责选择，Voice 负责表达。
> 三者通过结构化契约连接，任何一层都不能越过证据链。

现有实现见 `architecture.md`（运行时）与 `executive-policy.md`（EPA P1/P2）；
本文定义下一版框架，重点补上语言层、领域模块和学习闭环。

**当前状态（2026-10-26 核对，实测见 [`../sft/cloud/README.md`](../sft/cloud/README.md) 的
「2026-10-21 实测结果」）：**

- **policy head 已训完**（Qwen3-4B LoRA，558 步 / 2 epoch，`train_loss=0.1862`、
  `eval_loss=0.0995`），**但结论是否定的**：packet 验收 base 与 `fcode-qwen3-4b` 都是
  **1/2**（`open-ended` 正确 `INSUFFICIENT_EVIDENCE`，`full` 过早阻塞），因此
  **控制台默认不切 packet 模式**。
- **voice 只有规范**（`voice.md`），尚未接入运行时；`--structured` 只约束格式，不解决策略。
- **下一步**：补 choice SFT（每个决策点的候选与正确 choice），或在 host loop 加
  「同一动作无状态变化即要求 `report_blocked`」的进度守卫。

## 一、为什么需要第三层

旧结构是 Host + 一个自由生成的模型。EPA 把模型收窄成“从 2–5 个候选里选一个”，
解决了动作正确性和格式稳定性。但如果让同一个 policy head 同时写解释，会出现三个问题：

1. **输出契约冲突**：一个是 `{"choice":"aN"}`，一个是多段中文；混训会互相污染。
2. **权威边界模糊**：解释里的数字可能被当成事实；policy 的职责只是选择，不是陈述。
3. **评测不可分**：动作错和语气差混在同一个 loss 里，无法定位。

所以语言层必须是**独立的表达平面**，只做“把证据写成可读的句子”，不做选择、
不产生数字、不改变结论。

## 二、三平面

```text
┌────────────────────────────────────────────────────────────┐
│ Executive（Host）                                          │
│ goal → plan → packet → gate → budget → execute → verify    │
│ → evidence → judgment → review                             │
└──────────────┬──────────────────────────┬──────────────────┘
               │ DecisionPacket           │ ExplanationRequest
               ▼                          ▼
        Policy head                  Voice head
        {"choice":"aN"}              {headline, evidence,
                                      caveats, next_step}
               │                          │
               └────────────┬─────────────┘
                            ▼
                   Host validation
             candidate legal / numbers traceable
                            │
                            ▼
                 Evidence plane（底层）
        trace · hash chain · receipts · facts · cases · judgment
```

- **Executive** 拥有计划、预算、权限、执行、复算和终止判定。
- **Policy** 只从候选菜单里选一个 id；不生成 tool、参数、计划或解释。
- **Voice** 只把结构化证据写成结构化文本；不选动作、不加数字、不改结论。
- **Evidence plane** 是唯一事实源；Policy 和 Voice 都只是它的读者。

学习闭环在系统外侧：

```text
trace → choice dataset → policy SFT → packet acceptance
trace + evidence cards → voice dataset → voice SFT → blind judge
reviewer decisions → future preference data（暂不训练）
```

## 三、四个契约

| 契约 | 生产者 | 消费者 | 权威性 | 失败语义 |
| --- | --- | --- | --- | --- |
| `TaskSpec` | intake / operator | Host | 写入 `binding.spec` | 字段白名单拒绝 |
| `DecisionPacket` | Host compiler | Policy | 只读快照 | 无候选则阻塞/问用户 |
| `ActionChoice` | Policy | Host | 提议，不是决定 | 一次修复，仍失败 `POLICY_INVALID_CHOICE` |
| `ExplanationRequest` | Host | Voice | 只读证据 | 无证据则回退模板 |
| `ExplanationDraft` | Voice | Host | **非权威**，只用于展示 | 校验失败回退模板 |

### DecisionPacket（已有）

`providers/packet_prompt.py` 与 `runtime/policy.py` 已定义。保持：
objective / round / state / last_evidence / memory_cards / budget / candidates / output。
**不要**把完整工具目录、全部历史或计划全文塞回 packet。

### ExplanationRequest（新增）

```json
{
  "surface": "judgment | result | blocker | intake | review",
  "objective": "判断两期毛利率是否可比",
  "result": {"status": "succeeded", "change_bp": -250},
  "evidence_cards": [
    {"fact_id": "fact:FY2024:gross_profit", "period": "FY2024",
     "value": 151200000, "unit": "CNY", "basis": "IFRS",
     "source": "doc_fy2024_restated", "event_hash": "..."}
  ],
  "blockers": ["missing:doc_fy2025_bridge"],
  "audience": "partner | credit_committee",
  "constraints": {"max_sentences": 2, "banned": ["可能", "作为一个 AI"]}
}
```

Host 只把**已经进链的事实**放进 `evidence_cards`；不把原始客户资料、工具目录或
未验证的候选数字交给 Voice。

### ExplanationDraft（新增）

```json
{
  "headline": "两期合并毛利率可比：FY2024 17.50%，FY2025 15.00%，下降 2.50 个百分点。",
  "evidence": ["口径 IFRS、entity-total；FY2024 已应用重述。"],
  "caveats": ["分产品毛利缺失，无法区分价格与结构。"],
  "next_step": null,
  "citations": ["fact:FY2024:gross_profit", "fact:FY2025:gross_profit"]
}
```

Host 校验：

1. 正文中每个金额/比例/期间都能在 `evidence_cards` 找到；
2. `citations` 都是真实 fact id；
3. 不含身份冒充、emoji、感叹号；
4. 长度符合 surface；
5. 不新增结论、不改变 status。

任何一条失败：回退到确定性模板，记录 `voice_fallback`，不阻断任务。

## 四、领域模块

运行时不应该知道“金融”“审计”“量化”。当前 `tools/finance/` 已经是域代码，
但装配仍写在 `demo/` 里。下一版把它收成一个显式契约：

```python
class DomainModule(Protocol):
    key: str
    title: str

    def decompose(self, objective: str, spec: dict) -> Plan: ...
    def tools(self) -> list[ToolSpec]: ...
    def verify(self, observation, world) -> Verification: ...
    def authority(self, document, use) -> AuthorityDecision: ...
    def packet_hints(self, plan, memory) -> list[ActionCandidate]: ...
    def voice_surfaces(self) -> list[str]: ...
```

- `TaskSpec.binding.module` 选择模块；`ModuleRegistry` 在装配时解析。
- `runtime/` 只依赖 `DomainModule` 协议，不 import 任何具体域。
- 现有 `FinancePlanner`、`build_finance_tools`、`verify_observation`、`authority`
  收进 `FinanceModule`；行为不变，只是边界显式化。
- 新域 = 新模块 + 自己的工具/验证/权限；runtime 一行不动。

这是 `architecture.md` 里“新领域不改内核”从口号变成编译期约束的关键一步。

## 五、事件与不变量

### 事件

已有：

- `policy_requested` / `policy_decided` / `auto_action`
- `review_decided`

新增（只记录派生文本的元信息，不把全文塞进链）：

- `voice_generated`：`draft_hash`、`model`、`prompt_version`、`surface`、
  `validated`、`citations`、`tokens`、`latency_ms`
- `voice_fallback`：`surface`、`reason`、`draft_hash`（如果有）

完整 draft 存到 `artifacts/voice/`，像报告 HTML 一样可重放、可审计，但不参与
任务状态机。

### 不变量

1. 只有 Host 能 gate / execute / verify；Policy 和 Voice 都不能调用工具。
2. Policy 的输出必须是候选 id；自由文本一律拒绝。
3. Voice 的输出是非权威的；没有 `citations` 或数字不可追溯，就回退模板。
4. 每次状态变化进链；Voice 文本不是状态，只是带哈希的派生工件。
5. `unknown` 不自动重试；Policy 非法最多一次修复；Voice 非法直接回退。
6. 模型、prompt、packet、module 版本必须写进 binding 或事件。
7. 验收只看运行时结果和链验证，不看 loss、不看“像不像”。

## 六、失败语义

| 失败 | 处理 | 用户看到 |
| --- | --- | --- |
| Policy 返回非法 choice | 一次修复 | 仍失败：`POLICY_INVALID_CHOICE`，任务停在可解释的失败态 |
| Policy 选择合法但被 gate 拒绝 | 记录 denial | 具体拒绝原因，不重试 |
| Voice 数字不可追溯 | 回退模板 | 正常内容，附“模板回退”元信息 |
| Voice 超时/端点不可用 | 回退模板 | 不阻断任务 |
| 外部调用 timeout | `recovery_required` | 对账入口，不自动重试 |
| 单候选 | Host 直接执行 | `auto_action`，不扣模型预算 |

Voice 的失败**不能**让任务失败；Policy 的失败**必须**让任务停在可解释状态。
这是两层最重要的区别。

## 七、评测平面

冻结一套 20–30 个任务，跨 full / budget / crash / open-ended / unknown；
每个任务记录期望状态和关键结果。评测分三组：

### Policy

- 结果正确率（status + 关键数字）；
- 合法 choice 率、repair 率、invalid 率；
- `report_blocked` precision / recall；
- 候选位置打乱后的准确率（检测位置偏置）；
- packet bytes、model calls、tokens、latency。

### Voice

- 数字可追溯率（必须 100%）；
- 盲评 4/5 以上比例（目标 ≥ 80%）；
- 禁用词、身份冒充、空泛下一步；
- fallback 率（目标 < 10%）；
- 人工“敢不敢直接转发”评分（30 条盲评）。

### End-to-end

- 链验证通过；
- 预算内完成；
- 失败码正确；
- 报告可离线打开、可独立重算。

评测脚本仍走 `sft.evaluate` / `sft.accept` / `scripts/voice_judge.py`；
不引入重型评测框架。

## 八、学习闭环

```text
运行轨迹（哈希链）
   ├─ choice_lab → packet choice SFT → policy adapter → packet acceptance
   ├─ evidence cards → voice SFT → voice adapter → blind judge + human
   └─ review_decided → 偏好数据（后续 DPO/ORPO，不先做）
```

规则：

- 训练数据只来自自己的轨迹、模板和人工改写；不抓第三方对话。
- choice 和 voice **分开训练、分开验收、分开部署**。
- 每次训练记录：数据版本、prompt 版本、packet 版本、base 模型、adapter、
  评测报告；没有评测报告的 adapter 不进控制台。
- reviewer 的 `accept / needs_evidence / qualified / redo` 是未来偏好数据的来源，
  但当前先做监督学习，不急着上 RL。

## 九、部署拓扑

Mac 16GB，一次只常驻一个 4B Q4 模型：

| 模型 | 用途 | 何时加载 |
| --- | --- | --- |
| `fcode-qwen3-4b` | full-proposal baseline / 回退 | 实验或对照 |
| `fcode-qwen3-4b-policy` | packet choice | 默认运行路径（2/2 验收后） |
| `fcode-qwen3-4b-voice` | 解释/摘要（可选） | 打开判断面/报告时按需加载 |

Host 配置按 surface 选择模型；Voice 不可用时回退模板；Policy 不可用时回退
MockPolicy 只用于演示，生产必须显式失败。

## 十、路线图

| 阶段 | 内容 | 是否需要 GPU |
| --- | --- | --- |
| P3（进行中） | choice SFT、导出 policy、packet 2/2 | 是（已完成训练中） |
| P4 | `DomainModule` 协议 + 冻结评测集 | 否 |
| P5 | voice prompt baseline + Claude 盲评；按需 voice LoRA | 否 / 少量 |
| P6 | 控制台接入 packet 默认路径，保留回退；判断面接 voice | 否 |
| P7 | 第二个领域（审计或量化）验证模块边界 | 否 |

## 十一、明确不做

- 不让 Policy 自由生成 tool / 参数 / 解释；
- 不让 Voice 产生数字、结论或身份；
- 不把 Claude 或其他产品的文本直接放进训练集；
- 不用客户资料做训练或评测；
- 不在没有冻结评测集的情况下把模型设成默认；
- 不用“像不像”代替“能不能签字”。

## 参考

- Codex CLI 对照与融合：`codex-integration.md`
- 运行时现状：`architecture.md`
- EPA 契约：`executive-policy.md`
- 语言层：`voice.md`
- DeepSeek 外部 Provider：`deepseek.md`
