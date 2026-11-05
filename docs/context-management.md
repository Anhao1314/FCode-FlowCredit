# 上下文管理：合约化的压缩、状态与预算窗口

本文记录 2026-10-25 落地的三步通用化（设计讨论见 git 历史；三层记忆的立场
不变：派生而非存储、压缩而非原文、召回入链、记忆不可被模型改写、一切可重放）。

## 1. 观测压缩进 ToolSpec（`summarize` 合约）

压缩的责任从 memory 层下放到最懂结果的那一层：

- `ToolSpec.summarize(result) -> dict`：工具作者声明哪些字段是决策需要的；
- 注册表暴露 `registry.summarizers()`，`WorkingMemory` 只认合约、不认工具名；
- 未声明的工具走 `default_summarize`：标量保留（长串 head+真实长度）、
  列表变 count+首项、嵌套对象变键名表——形状驱动，永不放大。

general 工具的关键裁剪：`read_file` 不携带文件正文（head 600 字 + bytes +
sha256）；`run_command` 留 exit/duration/output 头；`fetch_url` 留
status/title/**content_truth**。finance 的七个 summarizer **逐字节搬迁**到
`tools/finance/summaries.py`——PROMPT_VERSION=v10 与 SFT 的黄金哈希不变
（`test_finance_prompt_v10_is_byte_identical` 仍然通过）。

`memory/context.py` 不再出现任何工具名。`domain-layer.md` 记录的两条
"剩余耦合"之一就此消除。

## 2. 状态派生进 DomainModule（`summarize_state` 合约）

`WorkingMemory.state()` 改为调用域视图；没有域的调用方得到中性回退
（rounds_recorded + tools_used），不撒谎也不沉默。

- finance：原 state 原样搬至 `tools/finance/summaries.py:summarize_state`，
  键集是 v10 prompt 合约的一部分；
- general（通用 agent 的状态卡）：**文件即事实**——

```
files:    written[{path, bytes, sha256}] · edited[] · read[]
commands: [{command, exit_code}]   # 非 0 退出进 blockers
fetches:  [{url, status}]
blockers: []
```

每个值都来自签名观测；卡片是投影，不是模型笔记。装配层
（demo/runner、console_manager、各 lab）在 `Harness(state_fn=...)` 一次性
传入，所有 Loop 站点自动获得。

## 3. 分层窗口 + Compaction-as-Event（预算感知 build）

**prompt 级遗忘，store 级永存**：观测记录永远全量保留（重放/重算/审计），
只有 prompt 有界（`observation_budget`，默认 24,000 字符）：

- 最近 N 轮压缩观测保持 inline；
- 更早的轮次折叠成 `observation_index` 一行一条：
  `round · tool · arguments 头 · result_hash 前 12 位`——确定性 digest，
  零幻觉、零成本；
- 新发生的折叠由 Loop 落成 **`context_compacted` 入链事件**
  （from/through/inlined-from/folded-count）。这是与"悄悄截断"型框架的
  分界线：审计问题"第 3 轮看见了什么"永远可以从 store 回答。

折叠单调：窗口不加深就不发第二个事件（`folded_through` 去重）。

## 测试

`tests/test_context_management.py`（16 项）：

- 合约：finance/general 全部 spec 携带 summarizer；read_file 摘要不含文件
  正文；run_command 留判定不留日志；fetch_url 带 content_truth；默认压缩器
  有界且形状正确；finance 摘要键集 = v10 合约；
- 状态：general 卡片投影 files/commands/fetches/blockers；失败命令进
  blockers；无域回退中性；finance 键集仍被钉住；
- 窗口：预算内全 inline；超预算最旧折叠、无重叠、inline 实际低于预算、
  折叠只产生一个 pending 标记；store 全量保留；
- 入链：折叠标记经 `harness.record("context_compacted")` 进链后
  `verify_chain` 通过。

## 4. recall_observation：窗口外回捞是治理动作（已落地）

折叠不删除，只降可见。当模型真的需要一条被折叠的观测时，它必须**要**——
`recall_observation`（`tools/general/recall.py`）：

- 只读工具，`round` / `tool` / `contains` 三种查询（空查询拒绝——回捞不是
  批量导出），命中上限 5 条；
- 返回**压缩摘要 + result_hash**，永不返回原始结果——原始记录只活在 store；
- 与其他读同路径：过 gate、扣 reads 预算、`tool_succeeded` 入链，下一轮
  上下文自然携带。审计问题"模型拉回了什么、为什么"用现有机制就能答。

数据来源是装配时绑定的 `memory_records_provider`（world 持有，Loop 每次运行
把活跃 WorkingMemory 发布进去）——读的是本运行自己的投影，够不到别的运行、
文件系统或网络。

## 5. 产物簿：通用 agent 的跨任务事实层（已落地）

`memory/artifacts.py`。通用域里**事实=产物**：一条记录 = path + sha256 +
produced_by(write/edit) + run_id + chain_head——每个值都来自签名观测，可以
随时对工作区重新验证，且不需要任何抽取模型。

- `project_artifacts(trace)`：纯函数投影（write/edit 且 changed 的轮次）；
- `ArtifactBook`：单 SQLite（Application Support/artifacts.sqlite），按
  path/sha/子串检索；同路径多次写**全保留**（latest + history），与事实簿
  同一个"不覆盖历史"立场；
- 控制台在运行终态时 ingest（两处终态路径都挂了），删除运行只墓碑化
  （archived=1），行仍然可查——遗忘降级，证据不删；
- 接项目工作区即是跨任务记忆：同一项目的后续运行读上次的产物文件，
  产物簿提供"这个文件是谁、哪一轮、以什么哈希产出的"索引。

## 测试

`tests/test_recall_and_artifacts.py`（14 项）：按轮次/工具/子串回捞、空查询
拒绝、命中上限、registry 路径入链、摘要合约；投影纯函数、latest/history、
sha 检索、墓碑化不删行、词法检索、真实 fib trace 投影。

## 6. 对话→工作桥接：意图上下文不跨边界丢失（第一层，已落地）

诊断：chat 有线程记忆（活跃分支尾部 24 条），委托没有——「转为委托」只带走
当前一句话，于是"周三/当天/到了后"的时间地点指代在规划器那里全部蒸发。

第一层的设计是**把对话作为操作员输入随委托旅行**：

- 前端：`转为委托` 打包活跃分支（role/text、剔除错误消息、每条截 600 字）；
- 传输：spec 的 `origin_thread` 字段，控制台**硬校验**（role 白名单、
  ≤24 条、字段类型，失败即拒——上下文能走，payload 塞不进）；
- 链上：binding 原样保留，重放安全；运行详情页显示「来自对话」芯片，
  点击跳回原线程；
- prompt：`Loop.restore` 从 binding 读回，`WorkingMemory.conversation`
  在**每轮**上下文输出 `conversation` 键。**没有 origin_thread 时不加这个
  键**——finance v10 黄金哈希与 SFT 逐字节测试不受影响（这是硬门）。

验收（API 级 E2E）：两轮行程对话 → 转委托「到了后帮我约客户会议」→
run binding 带 4 条对话 → `Loop.restore().build()` 的 context 里
`conversation` 完整可见；运行页「来自对话」芯片出现且能跳回。

**这层解决的是"委托能看见前文"；还没解决的是"多轮意图成为一个任务"**
（订票/订酒店/约会议 = 一个行程的三阶段，需要实体槽位进状态卡）——那是
chat v2 的 P2。

### 6.1 执行卡：同一条携带路径，加上用户的确认（P1，已落地）

桥接的第二段入口是对话里的执行卡：模型在回答里提出一次执行，用户在卡片上改完
目标/方式再点「开始执行」。**上下文只经过一条路径**——`threadOrigin()` 打包线程
尾部 24 条，确认端点把它放进同一个 `spec.origin_thread` 字段，同一个
`validate_delegation_spec` 复核。所以「周三 / 当天 / 到了之后」在两条入口里解析
结果一致，不会出现"转委托看得见前文、执行卡看不见"的第二种行为。

两处细节值得记住：

- 尾部而不是头部：超过 24 条时取**最近**的 24 条（`messages[-24:]`）。"到了之后"
  指的是最近的上下文，不是线程开头；早期实现取头部，等于把真正有用的那几条丢掉。
- 锚点记录用户批准的目标与方式（`chat.link_run(goal=, mode=)`），不是模型的初稿；
  锚点事件 `chat_run_started` 与消息一起重放，所以刷新页面后卡片直接显示"这次运行"，
  而不是又冒出一张待确认的提案。一个提案只锚定一次运行，第二次确认返回 409。

## 6.2 插话进上下文与域自己的协议措辞（Phase A，已落地）

两件都在同一条"模型看到什么"的链上，都由真实运行暴露出来：

**插话（operator_message）是链上事件，也进 prompt。** 控制台的输入框一直写着
"消息将排队并在下一轮交给 agent"，但在此之前文本只落到签名链上——没有任何
provider 读到它（代码注释也承认了）。现在 `Loop.operator_notes()` 每轮从事件
日志读回插话（保留最近 8 条、每条 ≤600 字，`steered` 标注照带），
`WorkingMemory.build()` 只在**确实有插话时**才输出 `operator_messages` 键——
没有插话的运行逐字节不变，finance v10 与 SFT 黄金哈希因此不受影响。语义是
边界语义：在第 N 轮边界入链的插话出现在**第 N 轮**的 prompt 里；运行途中到达的
插话在第 N+1 轮出现。

**域自己的协议措辞（DomainModule.instruction）。** `WorkingMemory.build()` 里
原来硬编码一句默认指令："只能提议只读动作……不能传递财务数值"。它是给财务
参考负载写的，却同时发给了通用委托。第三次真实 Kimi 运行把它暴露得很清楚：
模型提议 `report_blocked`，理由原文是"当前 instruction 要求只能提议 read-only
action，但 plan 中所有剩余待办均为 write_file、edit_file 或 run_command，没有
可推进的只读步骤"——工具没注册，运行以 `TOOL_NOT_REGISTERED` 被拒。模型没有
出错，它**照着我们写的话**拒绝干活。现在指令变成域声明：通用域的
`GENERAL_INSTRUCTION` 只描述工程能力、上下文视图与数据边界，行为协议统一由
`general-v3` 系统合约拥有，避免两处规则漂移；`Harness(state_fn=…, instruction=…)`
一次接线，两个 Loop 构造点自动继承；`None` 仍是那句冻结的默认文本。

验收（真实 Kimi，6 轮，$0.11，trace 存 `work/kimi-e2e/phase-a-operator-note/`）：
运行中插话要求"最终 markdown 命名为 note-seen.md 并写一行『已读插话』"，运行
**照做**（产物里就有这个文件和那行字），终态 succeeded、链 35 事件、文件状态
被 git 历史独立复核。同一轮运行还验证了审批档位：`off`（自动）下 6 轮无一次
暂停。证据里同时留下了两个被真实运行抓出来的缺陷：`console_manager` 少了
`import traceback`（错误处理本身抛 NameError，把一个可恢复的 ingest 失败变成
"永远运行中"的僵尸运行），以及 `ArtifactBook` 在 `__init__` 里建长连接
（sqlite 拒绝跨线程使用，控制台正是从运行的 worker 线程 ingest——现在改为
每次操作一条短连接，与 case/fact 簿一致）。

## 仍然欠的（下一步）

- 模型摘要 digest：仅当轮数大到索引表也放不下时，标注
  `model-generated, advisory`；
- 产物簿接 Projects（工作区即长期记忆的 UI 面）；
- 评估：LongMemEval（ClaimLedger 线）量记忆质量，任务成功率 gate 任何
  prompt 合约变更（v10 除外，它被黄金哈希冻结）；
- chat v2 P2：实体槽位（日期/地点/到达时间）作为可编辑字段进状态卡，多轮意图
  合成一个任务；P3 顺滑层（执行卡与委托首页共用同一份提案状态）。
