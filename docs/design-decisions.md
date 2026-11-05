# 设计决策

每一条都对应一个"如果反过来做会怎样"，以及代码里能指出来的位置。面试时按这个顺序讲，逻辑是连贯的：先讲权限，再讲钱，再讲失败，最后讲为什么不用现成框架。

---

## 1. 为什么模型不能执行工具，只能提议？

因为**执行权一旦下放，就没法回收**。

如果模型直接调用工具，那么"它能做什么"就取决于模型那一刻的输出，而输出是不可预测的。你想在调用前拦一道，就得把工具包一层——那本质上还是回到了"模型提议、别人执行"。

代码里的落点：`runtime/permissions.py`。每次调用要过三道独立检查：

```python
if tool not in self._registry:          # 注册表
    return GateDecision(False, "TOOL_NOT_REGISTERED", ...)
if tool not in self._allowed_tools:     # 任务创建时冻结的范围
    return GateDecision(False, "TOOL_NOT_IN_TASK_SCOPE", ...)
if tool not in grant_tools:             # 主机此刻的授权
    return GateDecision(False, "TOOL_NOT_IN_GRANT", ...)
```

**为什么查三次？** 第一次查"这个工具存在吗"，第二次查"这个任务被允许用吗"，第三次查"主机现在还同意吗"。第三次不是冗余——grant 在每一轮都会重新从主机读取，所以中途撤权能在下一轮立刻生效。只查任务体是做不到这件事的。

演示里 `tool-denied` 场景就靠这条：模型提议 `write_document`，名字合法、参数合法，但注册表里没有。结果 `DENIED`，而且 **reads 计数保持 0**——这不是"拒绝了但偷偷执行了"，是工具根本没跑。

---

## 2. 为什么预算要"先预留"，而不是调用完再算？

因为**最需要计费的那一刻，恰恰是拿不到结果的那一刻**。

一次调用可能：正常返回、超时、连接断开、进程被杀。后三种你都不知道供应商有没有计费。如果等结果回来再扣，那"未知"就永远是免费的——而免费的重试会一直重试到预算耗尽。

代码里的落点：`runtime/budget.py::charge_call`，注释写得很直白：

```python
"""Reserve and immediately consume one model call.
Called *before* the provider is asked, so a provider that hangs or
disappears still costs a call. That is the whole point.
"""
```

演示里 `budget` 场景：`max_calls = 4`，模型反复请求同一份资料。第 5 次调用在**发送前**被拒，`calls` 停在 4/4，失败码 `BUDGET_EXCEEDED`。已占用的 4 次不退。

**反过来的代价是什么？** 会多扣钱——预留了但实际没发出去。这个方向是可接受的：宁可少花，不能超支。金融场景里，"预算超了"是事故，"预算少用了"只是浪费。

---

## 3. 为什么超时之后不自动重试？

因为**超时不等于没执行**。

一次请求发出去之后超时，可能是：供应商没收到、收到了但慢、收到了并计费了但响应丢了。第三种情况下重试就是**重复付费**，而且可能产生两份不一致的结果。

代码里的落点：`runtime/harness.py::_unknown`：

```python
updated = task.with_model(status="unknown")
updated.failure = {
    "code": "MODEL_OUTCOME_UNKNOWN",
    "cause": error.code,
    "retryable": False,          # 注意这里
}
return self._commit(task, updated.advance("recovery_required"), "model_unknown", ...)
```

任务进入 `recovery_required`，这是**终态之一**：不调 `reconcile` 就永远停在这里。

**那怎么继续？** 只有一条路：拿一份**签名匹配**的收据来对账。`core/receipts.py::load` 会验 HMAC，`harness.reconcile` 会验 `task_id` 和 `round` 是否精确匹配。错一个字段就是 `MODEL_RECEIPT_MISMATCH`。

演示里 `unknown` 场景先故意拿一个全 0 的引用去对账，被拒（`MODEL_RECEIPT_REQUIRED`），再拿真收据，才恢复。

---

## 4. SIGKILL 之后，怎么保证不重复执行？

关键是**工作记忆不在内存里**。

如果记忆是进程内的对象，重启后就是空的，模型会从第一轮重新问一遍——前面读过的资料全部重读，调用全部重发。这在演示里会直接表现为轮次从 9 变成 10 甚至更多。

代码里的落点：`runtime/loop.py::restore`：

```python
def restore(self, task: Task) -> WorkingMemory:
    """Rebuild working memory from committed observations."""
    memory = WorkingMemory(...)
    for event in self.harness.store.events(task.id):
        if event["type"] != "tool_succeeded":
            continue
        detail = event["payload"].get("detail", {})
        memory.record(int(detail.get("round", 0)), Observation(...))
    return memory
```

这要求**观测结果本身写进事件**，而不只是写哈希。第一版只存了 `result_hash`，结果崩溃后无法重建——这是个真实的 bug，在 Phase 1b 修掉的。

还有一个更隐蔽的点：金融域自己的"已读/已归一/已桥接"标志也在对象里。`FinanceWorld.apply_state()` 从同一份事件状态重建它。**世界不是唯一记得读过什么的地方**，否则新进程会从一个空世界开始。

演示里 `crash` 场景的断言不是"计数对得上"，而是**没有任何一轮被重复提议**：

```python
duplicated = sorted({r for r in rounds if rounds.count(r) > 1})
assert not duplicated
```

---

## 5. 执行与记录为什么要分开？

因为**记录失败不应该改变已经发生的事实**。

如果 Recorder 和 Runtime 是同一件事，那么磁盘满、序列化失败、回调抛异常，都会让一个已经成功执行的任务变成失败。这在审计上是错的：任务确实完成了，只是没记下来。

代码里的落点：`trajectory/recorder.py` 是个**观察者**。它只收到 `round_start / proposal / gate / observation / round_end / terminal` 六个信号，不返回值、不参与决策。`runner.py` 用 `_Tee` 把它和终端渲染器并联，两者互不知道对方存在。

`Harness.verify()` 是唯一的成功判定入口，它不经过 recorder。

---

## 6. 为什么要单独一个 Evidence Authority？

因为**"这条证据能不能用于计算"和"这条证据是不是真的"是两个问题**。

混在一起会导致两种错误：要么把不可用的数据当成可用（用被重述覆盖的旧数算毛利率），要么把可用的数据当成不可用（因为无法证明绝对真实就一律拒绝）。

代码里的落点：`tools/finance/authority.py`。它返回五个权限，其中三个**永久为假**：

```python
PERMISSION_KEYS = (
    "contextual_calculation",       # 可能为真
    "scenario_calculation",         # 可能为真
    "risk_calculation",             # 恒假
    "independent_corroboration",    # 恒假
    "integrity_veto",               # 恒假
)
```

被重述覆盖的文档直接 `RESTATED_DOCUMENT` 拒绝——只能引用重述后的数字。这是演示里最实际的一条：用 18.00% 还是 17.50% 算 FY2024，结论差 0.5 个百分点。

**为什么恒假的三项也要写出来？** 因为"我没有资格做风险计算"必须是一句能被代码证明的话，而不是一句注释。

---

## 7. 状态机为什么要把非法迁移写在存储层？

因为**内存里的对象可以随便改，落盘的那一行不行**。

如果只在业务代码里判断"这个状态不能到那个状态"，那么任何一个绕过业务代码的写入路径都能破坏它——包括未来的你自己。

代码里的落点：`core/store.py::_assert_legal_change`，在 `BEGIN IMMEDIATE` 事务内、写事件之前执行：

```python
if current["status"] not in TASK_TRANSITIONS.get(previous["status"], ()):
    raise AgentError("ILLEGAL_STATE_TRANSITION", ...)
```

同时它还会拒绝：`id / version / principal / objective / binding / allowed_tools / model_grant` 任何变更，已成功的 plan 被重写，已成功的 step 被改写。

**回滚呢？** 链头哈希写在 task 行里，`verify_chain` 会比对"事件条数 == task.sequence"和"最后一条事件的 hash == 链头"。删掉最后一条事件，`CHAIN_ROLLBACK`。

---

## 8. 工作记忆为什么是"派生"的，不是"累积"的？

因为**可变的记忆可以被改写，派生的不能**。

如果记忆是一个不断被 append 和 mutate 的对象，那么一次崩溃后它就和日志不一致了——而且你无法判断哪边是对的。

代码里的落点：`memory/context.py::state()`，每轮从 `records` 重新计算：

```python
def state(self) -> dict:
    """Derive what has been established so far, from observations alone."""
```

它还把每条观测**压缩**成决策需要的字段（`_summarise`），而不是把完整工具输出一路传下去。两个理由：上下文窗口会被没用的文本塞满；更重要的是，让下一步决策依赖**事实**而不是**措辞**。

**冲突怎么处理？** 目前是"后到的覆盖先到的"——`calculate_margin` 只读世界的最新状态。真正的多源冲突（两份资料给出不同数字）在 v1 的 T2-B 里测过，结论是当前设计不够：需要显式的 `stop_warrant`，而不是静默取一个。这一条在 v1 的报告里标为 `INCONCLUSIVE`，v2 没有假装解决。

---

## 9. Token 不可信的情况下，怎么算钱？

**先按上界预留，再按实际结算，差额不退。**

上界怎么来：`input_bytes + 512` 作为输入 token 的保守估计（UTF-8 字节数通常大于 token 数），输出用 `max_tokens` 硬上限。这两个数相乘得到最坏情况花费，作为预留。

代码里的落点：`runtime/harness.py` 的 `RESERVED_TOKENS = 1_000` / `RESERVED_COST_MICROUSD = 500`，在 `provider.propose()` **之前**扣。

真实供应商的收据里，`external_billing` 恒为 `not-verified`，`actual_cost_microusd` 恒为 `null`。这不是偷懒——本地没有任何办法验证供应商的账单，写一个数字上去才是撒谎。

**这意味着什么？** 预算是一个**硬上界**，不是精确账单。演示里 9 轮花了 4500 µUSD，这是预留总和，不是"真实花费"。

---

## 10. 为什么不用 LangGraph / AutoGen / CrewAI？

因为它们优化的是**能力**，我优化的是**可证明性**。

具体三条：

**状态在哪。** LangGraph 的 checkpoint 是框架内部状态，序列化格式由框架决定；这里的每一步是一个哈希链事件，你可以脱离代码用 `sha256` 自己验。

**失败怎么办。** 多数框架的重试策略是"失败就再来一次"，有的带指数退避。这里没有重试：超时就是 `unknown`，只能对账。这是一个**故意更差**的选择——可用性换正确性。

**谁说了算。** 多数框架里 agent 决定何时结束。这里 `terminal` 由 Runtime 判定：`calculate_margin` 重算通过才算成功，模型提议 `report_blocked` 也要 Runtime 复核世界是否真的无解。

**那什么时候该用框架？** 当你要快速做一个能跑的东西、失败可以重来、成本不敏感的时候。反过来，当"这次调用到底发没发出去"是一个必须回答的问题时，框架帮不上忙。

---

## 11. 为什么 canonical JSON 要禁止 float？

因为**浮点数没有稳定的文本形式**。

`0.1 + 0.2` 在不同运行时、不同优化下可能序列化成 `0.30000000000000004` 或 `0.3`。哈希一旦依赖这个，跨机器就验不过。

代码里的落点：`core/hashing.py::_reject_float`，遇到 float 直接抛 `TypeError`。金额用整数（元），比率用基点（万分之一）。

这不是洁癖：演示里 `margin_bp_2024 = 1750` 表示 17.50%，`change_bp = -250` 表示 -2.50pp。整数加减永远不会出现 `-2.4999999999999996`。

---

## 12. 为什么用 SQLite，而不是 v1 的目录树？

v1 每个任务一个目录，历史条目一个文件一个，配 HMAC 签名与独立 witness。那套东西能工作，但有两个代价：每次 `load` 要重放全部历史（O(N²)），以及"写历史 → 写 witness → 写 checkpoint"之间存在一个崩溃窗口，窗口里崩了任务就永久不可读。

SQLite 版把三件事压进一个事务：写事件、写任务体、更新链头。要么全成，要么全不成，没有中间态。代价是失去了 witness 的独立性——所以 `verify_chain` 在每次读取时全量重算，用计算换结构。

**这是降级吗？** 是。v1 的 witness 能防"整个目录被回滚"，SQLite 版防不了。但 v1 的代价是一个已知的永久阻塞 bug，而 v2 的取舍是明确的、可测的。文档里写清楚比假装没有更强。

---

## 13. 为什么把执行顺序放在「计划」里，而不是放在策略代码里？

因为**散落在 `if` 里的顺序无法被审计、无法被修订，也无法被恢复**。

把三步换成计划之后，变化是具体的：

- 崩溃后，`Loop.rebuild_plan()` 重放观测就能恢复「进行到哪了」。顺序如果是策略里的硬编码，恢复只能靠数外部变量。
- `Plan.revise()` 递增 `revision` 并把新版本写进事件链，于是「agent 什么时候改变主意」变成可查的一条记录。
- `Todo.evidence` 记录完成所依据的观测哈希，于是「done」不是一句自我声明。
- 前端能显示 todo 状态，评审时不再需要读代码推断进度。

代价是明确的：多了一层状态要维护，计划本身会成为 bug 的来源。所以规划器被刻意做成域代码（`tools/finance/planner.py`），内核只提供状态机（`core/plan.py`），并且计划每次从日志重放而不信任快照——这两个决定限制了出错半径。

`open-ended` 场景验证的是这条设计的边界：桥接材料不存在时，`t6` 被 `blocked`，agent 把剩余计划继续跑完，然后以 `INSUFFICIENT_EVIDENCE` 停止，报告里带的是领域原因「该公司没有公布该期间的桥接说明」而不是一句通用的失败。**它没有给出一个无法核对的数字，这是这个Demo真正想证明的事。**

诚实的边界：`mock.py` 现在只是「取第一个未完成的 todo」。真正做分解和修订的仍然是规则式的规划器，不是模型。把顺序搬进计划换来的是可控与可审计，不是智能。

---

## 14. 长期记忆为什么是「案例簿」，而不是向量库或抽取式记忆？

调研过的路线各有代价：MemGPT 用虚拟上下文管理长对话，Mem0 让模型抽取事实再合并，Generative Agents 用重要性打分加反思，HippoRAG/Graphiti 建知识图谱，A-MEM 建 Zettelkasten 卡片。它们共同的假设是「原始经验是非结构化文本，所以需要模型再加工」。

FCode 的前提不同：每条经验已经是**签名过、哈希链锚定、可重算的结构化轨迹**。再让模型抽取一遍，只会引入幻觉和第二套事实来源。所以长期记忆被设计成**确定性投影**：

- 一个终态 trace 投影成一行案例：目标、模块、模板、结局、轮数、成本、人工决定数、关键引用、工件、标签、中文摘要、链头。
- 同一份 trace 重建两次，字节相同；`tests/test_case_memory.py` 直接比对 SQLite 全表。
- 检索分数是可解释的整数基点：`0.5 × 相关性 + 0.3 × 重要性 + 0.2 × 时效`，相关性是关键词/标签重叠，重要性来自「成功 + 独立验证 + 有人工参与」，时效按 90 天半衰期衰减。分数不让模型生成，也不进入训练标签。
- 删除运行记录只写墓碑（`archived=1`），默认检索隐藏，`read_case` 仍可按 run_id 读到链头——证据不被删除，只退出排序。

事实层把「我们曾相信什么」与「什么时候相信」分开：`valid_from/to` 描述财务期间，`recorded_at` 描述观测时刻。修正不会覆盖旧行，而是追加一行并用 `supersedes`/`superseded_by` 连起来，所以 `known_at` 能复现重述前的答案；同一主张被多次观测到则合并，避免事实簿被重复读取撑大。

召回不是每轮重新查库，而是在委托创建时查一次、把 top-3 写进 `memory_recalled` 事件。事件里保存命中及其分数，首轮上下文从这里重建，重放和 SFT 导出也读同一条事件——否则恢复后的运行会看到另一套排序，训练与推理就会悄悄漂移。`tests/test_sft_export.py` 对带召回的轨迹做逐字节比对。实体邻接用已入簿的文档、期间和工件做 Jaccard，不引入向量或概率图；目前只有金融域有确定性抽取器。没做的部分不假装有。
