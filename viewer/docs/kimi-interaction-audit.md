# Kimi 官方开源交互审计 → FCode viewer 映射

来源：MoonshotAI/kimi-code（官方 TypeScript monorepo，8k stars）。Kimi 的
Web 产品本体（kimi.com / code-app）不开源；本审计取自其官方开源的
Agent 交互实现——`apps/vscode/webview-ui`（官方 Web 技术 Agent UI）、
`packages/pi-tui`（终端组件库）、`packages/transcript`（回放数据层）、
仓库级 `AGENTS.md`（架构约定）。学交互思想与架构分层，不抄代码。

## 一、架构启示：transcript 四层订阅（对 FCode 最有价值）

官方把"会话回放"做成独立数据层 `packages/transcript`，与渲染框架解耦：

| 层 | 职责 | FCode 对应现状 |
| --- | --- | --- |
| L1 | agent 粒度存储 | trace JSON + task.sqlite（已有，且更强：哈希链） |
| L2 | 幂等操作批 | SSE 快照全量推送（弱于：无操作语义） |
| L3 | `off / turn / block / delta` 四档订阅粒度 | 单一 SSE 全量流 |
| L4 | 无框架视图注册表 + turn-cursor 分页 | 直接重渲整列 |

**结论**：FCode 的 L1 比官方强（可审计），L3/L4 弱（全量推送 + 整列重渲）。
长会话下按 turn 增量订阅、按 block 更新是正确的演进方向，与零依赖约束
兼容（SSE 本身可携带 turn/block 语义的增量事件）。

## 二、交互模式映射（官方 webview-ui 37 组件的提炼）

### 1. 审批门（官方 ApprovalDialog → FCode 审批块）
- 标题动词化："Allow this write?"（允许这次写入？）而非"等待批准"；
- 描述区等宽字体、浅底、限高滚动；**含 diff 时自动展开**；
- 三档授权：Yes / Yes for this session / No（会话级授权）；
- 键盘序号 1/2/3 直选，选中项高亮。
FCode 现状：两档（允许/拒绝）+ remember；无 diff 感知、无序号键。

### 2. 插话队列（官方 QueuedMessagesPanel → FCode 运行中插话）
- 排队消息可**编辑**、可**上移**调序；
- **steer（⚡ 立即插入）**：琥珀色 bolt 图标，把队首消息插进当前流；
- 操作按钮 hover 才显（opacity 过渡），保持列表干净。
FCode 现状：只有"插话→下一轮生效"，无队列管理与 steer。

### 3. 工具结果展示（官方 DisplayBlocks → FCode 执行结果折叠卡）
官方把工具产出规范为**四种块**：`diff`（代码差异）/ `todo`（计划）/
`brief`（摘要）/ `removed`（删除），各配专属渲染。
FCode 现状：单一折叠卡 + JSON 表格；可按工具类别映射四块
（read→brief、计划变更→todo、删除类→removed）。

### 4. 进行中指示（官方 ChatMessage 规则）
- 指示器（转圈/脉冲）只挂在 `text|thinking && !finished` 的活动项上；
- 工具/思考/压缩类 item 有 step 级指示，完成后静默。
FCode 现状：轮次级指示，item 级无差别。

### 5. 状态行（官方 ChatStatus）
- token 分类实时计量：input_other + cache_read + cache_creation + output
  + active（当前运行中）双轨显示。
FCode 现状：预算四维 meter（calls/reads/cost/tokens）已有，缺
cache/token 分类细分——kimi provider 的收据 metadata 已带 cached_tokens，
可点亮。

## 三、设计 token 对照（官方 index.css，shadcn/oklch 体系）

| 官方 | 值 | FCode Console Light | 判定 |
| --- | --- | --- | --- |
| --radius | 0.5rem (8px) | 8px | 一致 |
| --primary（浅色） | 近黑 oklch(0.21) | #18181B 近黑 | 一致 |
| --background | 纯白 | #FAFAFB 近白 | 一致 |
| --border | oklch(0.92) 浅灰 | rgba(0,0,0,.08) | 一致 |
| --muted-foreground | 中灰 | --text-2 | 一致 |
| 字体 | Inter（可变） | 系统栈 | FCode 零依赖选择系统栈，视觉近似 |
| 暗色 | oklch(0.141) 全套 | 已退役（git 历史） | — |

结论：Console Light 的取值与官方 Web UI 基本同构，无需再动 token。

## 四、FCode 不抄的部分（边界）
- React/Tailwind/shadcn 技术栈——违背零依赖纪律；
- 产品品牌元素（logo、吉祥物、文案）；
- OAuth/远控/遥测等与治理运行时无关的产品面。

## 五、落地优先级建议
1. 审批门升级（动词化标题 + diff 自动展开 + 三档授权 + 序号键）——治理产品的心脏；
2. 插话队列（编辑/调序/steer）——"运行中可干预"的直接体现；
3. DisplayBlocks 四类块——回放可读性；
4. token 分类计量——收据里已有数据，纯渲染；
5. transcript 分层订阅——架构级，留作专项。
