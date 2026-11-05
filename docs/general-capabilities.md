# 受控通用能力：原生工具集与 MCP 桥接

**状态：当前权威（能力边界）** — 通用委托能做什么、不能做什么，以及 MCP 桥的忠实边界；运行时实现见 [architecture.md](architecture.md)。最后核对：2026-10-26。

> 状态：第一阶段（原生受控工具集）与第二阶段（MCP 桥接 v1）均已实现。
> 本文档是设计决策的记录，也是"独立重算的推广"的正式定义。
> 说明：任务书提到的 `docs/mcp-tradeoffs.md` 在仓库中并不存在（全库检索无命中）；
> 其权衡内容已并入本文第二节。

## 0. 主张

FCode 不自建浏览器自动化、自进化或终端执行这类重能力。通用能力靠**接入**，
FCode 提供的是接入时的治理：门控、预算、收据、unknown 语义、事件链。
本批次的目标是让治理之下的 Agent 达到通用 Agent 的能力基线（读写文件、抓取网页），
同时证明核心主张——**任何外部能力接入时都必须留下收据**。

治理不变量（红线，测试锁定）：

1. 任何新工具走既有 门控 → 预算 → 收据 → unknown 语义 路径；
2. 注册表保持封闭——MCP 工具是"导入注册表"，不是绕过注册表；
3. 模型永远只能提议；效果类授权（read/write/network）写在主机 grant 里，
   任务不声明就保持 Phase 1 的只读承诺；
4. 路径校验与 SSRF 防护在**工具实现内**强制，绝不在前端或提示词里。

## 1. 能力来源分层

```
原生受控工具（第一阶段，参考实现）
        │  同一注册表、同一门控、同一预算、同一收据
MCP 桥接（第二阶段，外部工具正式导入通道）
```

- **原生层**（`src/flowagent/tools/general/`）：`ls_workspace` / `read_file` /
  `write_file` / `fetch_url`。领域无关，注册为 `general-work` 域，与财务域并列共存。
- **MCP 层**（`src/flowagent/tools/mcp/`）：仓库根 `mcp.json`（主机审阅后生效）
  声明的 stdio server，启动时连接、`tools/list` 导入，每个工具以
  `mcp__{server}__{tool}` 命名进注册表，inputSchema 直接作为门控参数校验。

## 2. 关键设计决策

### 2.1 效果类授权：分类权在主机

`Permissions` 原本无条件拒绝 write/network。本批次把它改为：
grant 里的 `allowed_effects`（缺省 `["read"]`）决定本任务可执行的效果类。
财务域 grant 不写这个键 → 行为零变化；通用域 grant 显式写
`["read", "write", "network"]` → 三方一致（注册表 ∩ 任务范围 ∩ 主机 grant）才放行。
grant 里出现未知效果类 → `GRANT_EFFECTS_INVALID`，大声拒绝。

MCP 工具不自声明副作用：**一切导入默认 consequential（过审批）**，
只有主机在 `mcp.json` 按工具标注 `readonly: true` 才归入读取类。
"分类权在主机"与"模型不能自审"同源。

### 2.2 沙箱：路径监狱 + git 版本化 + 哈希收据

- 每个运行的沙箱锚定 `runs/{runId}/workspace/`；路径在
  `WorkspaceSandbox.resolve` 内归一化：绝对路径、`..`、空组件、反斜杠、
  NUL、`.git` 一律 `SANDBOX_PATH_DENIED`；`resolve()` 后做包含检查，
  符号链接逃逸（沙箱内链接指向外部）同样拒绝。
- 沙箱惰性初始化为 git 仓库，**每次有效写入自动 commit**（内联身份，
  不读全局 git 配置）；内容不变的重写不伪造历史（无 commit），但收据
  仍记录前后哈希，无操作可见。
- `write_file` 收据：`sha256_before`（新文件为 null）/ `sha256_after` /
  `commit` / `bytes`。审批界面展示的是**主机在执行前算好的真实 unified diff**
  （`WorkspaceSandbox.diff_preview`），与写入走同一套路径校验。
- 崩溃恢复语义照旧：write 的观察已入链，`rebuild_plan` 重放后 todo 已
  done，恢复后不重写。

### 2.3 fetch_url：解析后钉住（防 DNS 重绑定）

- 输入 url，输出正文文本；上限 1 MiB，超时 30s，至多 3 次重定向
  （每一跳从头重新校验）。
- SSRF：`getaddrinfo` 解析后**逐一**校验所有地址（环回/私网/链路本地/
  唯一本地等非全局地址一律拒绝，放行清单 `FCODE_FETCH_ALLOW_HOSTS`
  或构造参数可显式豁免主机），然后**连接钉死的已校验 IP**；
  HTTPS 用自定义 `HTTPSConnection` 保持 SNI 与证书校验在原域名上。
  DNS 重绑定（校验与连接之间换答案）因此没有可乘之机。
- 收据记录 url、最终域名、状态码、内容 sha256、字节数、时延。
- 诚实标注：结果携带 `content_truth: "not-verified"`——运行时复核的是
  文件状态哈希、预算与链完整性，**网页内容的真实性如实声明不保证**。

### 2.4 unknown 语义的推广

工具层引入"结果未知"：AgentError 携带 `details={"outcome": "unknown"}`
（fetch 超时/断连、MCP 调用超时/进程失联）时，Harness 记
`tool_unknown` 事件并以 `TOOL_OUTCOME_UNKNOWN` 终止任务——读取已计费、
**绝不重发**、不伪装成干净失败。这与模型侧 unknown（不重发、不退费、
凭签名收据对账）同一纪律；工具侧的收据式对账（带外签名结果）刻意
不在本期发明，留待确有需求时立项。

### 2.5 提案参数上限的再定界

`validate_plan` 的参数上限从 1 KiB **再定界**为 96 KiB（不是取消）：
1 KiB 假设了只读工具的标识符级参数，`write_file` 携带的是成文内容。
工具 schema 仍以 `Param.max_length`（write_file 内容 65 536 字符）在
**门控时**拒绝超限提案—— oversized 内容在审批前就被拒绝，不会审批通过
后才发现写不下。字符串/布尔/数值/字符串数组的输入校验同步补齐
（MCP inputSchema 映射需要）。

## 3. 独立重算的推广（收据哲学，不是放弃）

财务域重算**数字**：runtime 从世界复算毛利率并比对。通用任务重算
**可复核的账**：

| 对象 | 复核方式 |
| --- | --- |
| 文件状态 | `general-work` 验证器从磁盘重读文件、重算 sha256，与 write 观察记录的 `sha256_after` 比对；不一致即 `RESULT_VERIFICATION_FAILED` |
| 预算 | 既有 calls/reads/cost/tokens 计量，逐轮入链 |
| 链完整性 | `verify_trace` 哈希链重算 |
| 网页内容真伪 | **不可复核** → 显式 `not-verified`，绝不伪装成已验证 |

"运行时独立验证结果"因此从"能重算数字"推广为"能重算的都重算，
不能重算的如实声明"。这是收据哲学的推广，不是放弃。

## 4. MCP 桥 v1 的边界

做：stdio 传输（本地进程）、`initialize`/`tools/list`/`tools/call`、
JSON-RPC 2.0（stdlib 实现，行分隔）、逐工具导入与拒收（schema 含
object/union/非字符串数组等门控无法校验的类型 → 拒收并给出理由）、
失联降级（`/api/mcp` 如实报告 per-server 可用性；调用时预检失败为
confirmed `MCP_SERVER_UNAVAILABLE`，发出后超时/失联为 unknown）。

不做（v2 及以后）：HTTP 传输、MCP resources/prompts、外部计费工具定价
（配置位 `billing` 已预留，值恒为 `not-configured`）、自进化、多
Agent 编排。

**2026-10 第二批修订**：原"shell/终端工具（单独立项）"已落地为受限
版 `run_command`（白名单 + 参数策略 + 审批 + 收据，见 4b）；原"浏览
器自动化（不自建）"已按分层决策经 MCP 导入 `playwright-mcp`（4b）。
仍未做的：系统级沙箱（容器/seccomp）、自由形态 shell（无白名单的
任意命令行）、HTTP 传输的 MCP。

密钥纪律：`mcp.json` 的 env 值支持 `"env:VAR_NAME"` 从启动进程解析，
密钥不落盘；配置内容不进 trace。

## 4b. 能力基线三大件（2026-10 第二批）

通用 Agent 的能力基线补齐为三件，全部走既有 门控→预算→执行→收据 路径：

| 能力 | 形态 | 效果类 | 审批 | 收据 | 独立重算 |
| --- | --- | --- | --- | --- | --- |
| `edit_file` | 原生工具 | write | consequential（真实 diff 预览） | before/after blob sha256 + 行数变化 + git commit | 当前盘上哈希，或收据所指 git 历史状态 |
| `run_command` | 原生工具（受限终端） | write（命令级 readonly 豁免） | consequential（完整命令行卡） | 完整 argv、退出码、耗时、截断标注的合并输出 | 输出语义不可重算 → 收据如实记录，文件效应看 git 快照 |
| 浏览器自动化 | MCP 导入 `mcp__playwright__*` | write（全部 consequential） | 审批卡显示 URL/选择器参数 | 调用参数 + 返回文本摘要 | 不可独立重算 → not-verified |

### edit_file：锚定替换，不许模糊

`old_text` 必须在目标文件中**唯一**匹配，否则拒绝并返回候选行号（模型
重试时能看到锚定歧义在哪）；文件不存在拒绝——创建是 `write_file` 的
语义，编辑器不许混淆两者偷运新文件。每次编辑的改动行数（+/− 合计）
上限 200，超限拒绝并提示拆分。修改照常自动 commit，因此审批界面天然
展示这次编辑的真实 diff（与 write_file 同一展示路径）。

### run_command：白名单注册表，不是 shell

- **白名单**：代码默认集（python/python3/node/git/ls/cat/grep/find/
  wc/diff/pytest/unittest）+ `commands.json`（与 mcp.json 同级，主机
  审阅；只可加条目，不可弱化内置规则）。
- **参数策略**：解释器只跑 workspace 内脚本文件（`-c`/`-e`/`-m` 一律
  拒绝——内联代码是文件监狱看不见的逻辑逃逸）；git 只放行
  status/diff/log/add/commit（无网络、无改史、无 `-C/--git-dir` 改址）；
  只读工具的路径参数全部过沙箱监狱；任何参数含 shell 语法
  （`;` `&&` `||` `|` `<` `>` 反引号 `$(`）直接拒绝——执行用
  `subprocess` 数组 + `shell=False`，本来就没有 shell 可注入，拒绝是
  政策声明：终端没有 shell，用 shell 语法书写的提议本就不合法。
  （代价：grep 交替用多个 `-e`，不写 `a|b`。）
- **执行**：cwd=workspace/{runId}/，截止默认 30s（参数上限 120s）；
  stdout/stderr 合并捕获，64KB 截断并如实标注；退出码与耗时入收据
  （非零退出码是**确定结果**，不是失败）；子进程环境剥离全部 provider
  凭据，并注入与沙箱一致的 git 身份（无全局 git config 的主机也能
  经终端 commit）。
- **分类**：默认 consequential；命令注册表标 readonly 的纯读命令
  （ls/cat/grep/find/wc/diff/git status|diff|log）免审批——与
  read_document 同等待遇。分类权在主机注册表，不在模型提议。
- **超时 = unknown（本批与治理叙事最强的连接点）**：命令已发出而未
  归——杀整个进程组（不让任何残留继续改工作区），记 `COMMAND_TIMEOUT`
  `outcome: unknown`，读取已计费，**绝不自动重跑**。对账凭据就是工作
  区本身的 git 快照：命令实际改了什么，`git log` / `git diff` 一目
  了然。这与模型侧 unknown（不重发、不退费、凭签名收据对账）同一
  纪律——工具侧的对账通道不是签名收据，而是沙箱的版本化历史。

### 浏览器（playwright-mcp）：导入而非自建

配置步骤（主机操作）：

1. `cp mcp.json.example mcp.json`，保留/裁剪 `playwright` 条目
   （`npx @playwright/mcp@latest --stdio`）；
2. 主机审阅清单：确认信任 `@playwright/mcp` 的包与版本（npx 首次会
   下载包与浏览器内核——这是外部代码进入主机的过程，审阅发生在
   这里）；确认要暴露给模型的 browser_* 工具面；`readonlyTools`
   仅在主机确信某工具只读时标注（默认不标——浏览器动作天然有外部
   副作用）；
3. 重启 console，`/api/mcp` 如实报告导入结果与拒收理由。

诚实边界：浏览器动作的**结果正确性不可独立重算**——runtime 复核的是
调用是否过门控/预算/审批、收据是否完整（参数 + 返回文本摘要 +
时延）；页面真的长什么样、点击真的发生了吗，不可复核，收据
not-verified。这是治理框架对不可验证动作的既定答案。mock stdio
冒烟（`tests/fixtures/mcp_playwright_mock.py`）锁定导入契约：
命名 `mcp__playwright__browser_*`、全部 consequential（server 自述
"read-only" 不被信任）、URL/选择器参数经门控 schema 校验、调用经
桥接往返。

### 诚实边界：受限终端不是系统级沙箱

run_command 的"受限"是**命令级**的：白名单、参数策略、审批、收据、
git 快照。操作者批准的脚本能做其解释器能做的任何事（读工作区外文件、
开连接）——审批界面显示完整命令行、收据记录退出码与输出、工作区
git 快照记录文件效应，这些让"批准什么"可审阅，但不构成 OS 级隔离。
容器/seccomp 级隔离是另一个立项。

## 4c. 运行提问：`ask_operator`（2026-10 第三批）

不会提问的通用 agent 只能猜。这个工具把"停下来问人"做成了一次受治理调用：

- **它是真工具**：进注册表、过权限门控、计一次读预算、和别的调用一样写进签名链。
  它**不**过审批——问一句不是有后果的动作（`before_tool` 只对 `write_file`/
  `edit_file`/`run_command` 生效）。
- **链条自己就是审计**：`tool_started` 的 arguments 是问题，`tool_succeeded` 的
  result 是回答（`{"asked", "answered", "answer"}`）或如实标注的未回答
  （`{"answered": false, "timed_out": true, "reason": …}`）。没有额外事件类型：
  从工具体内部再写事件会破坏 store 的乐观序号（循环持有的是任务快照），而且
  一次工具调用本来就是一个审计单元。
- **人的通道是控制台的，不是内核的**：世界携带一个 `ask` 回调，控制台把它接到
  运行自己的 control 上（与审批等待同一处：同一个 pending 槽、同一个 `_wake`、
  同一个 300 秒超时）。等待中停止运行 = 抛出 `StopRequested`，与审批被停止一致。
- **没有通道就如实说**：CLI demo、离线测试这类没有操作员的部署，工具返回
  `"this deployment has no operator channel"`，模型据此自行判断，而不是挂住。
- **答案回到下一轮**：结果就是工具的 observation，`WorkingMemory` 的摘要器
  **完整保留**回答（唯一不截断的字段——丢了它模型会把同一个问题问第二遍）。
  单元测试直接在 provider 收到的第二个 context 里断言回答可见。
- **提问不是逃生门**：通用域指令同时写明"问了没人答而仍然无法完成时，把已知
  信息与缺口如实写进产出文件"，避免模型转头去提议未注册的工具（那会被门控
  拒绝并终止整个运行——真实运行里发生过两次）。

端点：`POST /api/runs/{id}/answer {questionId, text}`（或 `{questionId, skip: true}`），
与 `/decision` 同形；前端把问题渲染成与审批卡同一位置的卡片（线程尾部=实时边沿），
回答后在同一轮次里留下 Q&A 块。

验收：`tests/test_ask_operator.py`（12 项：工具形状、无通道、超时、跳过、预算、
计划步骤、答案进下一轮 context）；一次脚本化 provider 的活运行
（`work/kimi-e2e/ask-operator-live/`）：问题与回答是一次链上工具调用，浏览器里
的卡片提交后运行继续到 6 轮，产物文件里的内容证明回答进入了模型下一轮的上下文。

## 5. 验收与证据

- 第二批离线测试：`tests/test_capability_baseline.py`（edit_file 唯一
  匹配/歧义行号/锚不存在/不存在的文件/行数上限/删除语义/预览不落盘；
  run_command 白名单外拒绝、python -c/-e/-m 拒绝、注入姿势拒绝、
  路径参数监狱、git 子命令与全局 flag 拒绝、readonly 分类、真实执行
  与退出码、非零退出为确定结果、超时→unknown 且进程组被杀、输出
  截断、timeout 参数边界、经终端的 git commit 落进沙箱仓库；脚本
  委托全流程 Loop 成功 + 双验证（当前盘上/git 历史）、超时路径
  tool_unknown 只派发一次；commands.json 只加不减；playwright mock
  stdio 冒烟：命名空间导入、全部 consequential、server 自述不被信任、
  URL 参数门控校验、调用往返、主机 readonly 标注生效）。当前全量
  307 项测试通过。
- 离线测试：`tests/test_general_capabilities.py`（沙箱逃逸矩阵、SSRF
  各地址段、DNS 重绑定、钉住连接、超时→unknown、前置拒绝为 confirmed、
  1 MiB 上限、重定向再校验、收据字段与 not-verified、门控效果类、
  超限内容门控拒绝、planner、文件状态验证、全流程 Loop 成功、篡改
  失败、tool_unknown 只执行一次）、`tests/test_mcp_bridge.py`（真实
  子进程 stdio 全链路：握手、导入命名、schema 校验、主机分类、调用、
  isError confirmed、超时 unknown、进程死亡降级、坏 server 不遮蔽好
  server）。当前全量 395 项测试通过。
- 里程碑演示（录屏/截图）：viewer 创建通用委托"调研公开主题，写
  research.md"→ fetch_url 真实抓取收据 → write_file 真实 diff 审批
  （键盘放行）→ 预算走表 → `verify_trace` 通过 → 工作区文件存在且
  哈希与收据一致。见仓库 `flowcredit/viewer/docs/` 下演示截图。

## 6. 遗留风险

- fetch_url 的正文抽取是 stdlib HTML 解析（与 connectors 共用），
  对重度脚本渲染的页面只能拿到静态文本；这是诚实边界，不是缺陷。
- MCP 工具在本期不进入通用计划的默认蓝图（模型可提议、门控可放行，
  但 mock 蓝图只含 fetch/write）；真实 provider 提议 MCP 工具的端到端
  验证留给有真实模型的批次。
- 工具侧 unknown 目前是终态（不重发、等操作者重启新委托）；带签名
  收据的工具级对账通道未发明。
- `runs/{runId}/workspace` 与运行目录同生命周期；工作区导出/归档策略
  未定。
