# FCode Viewer 前端对比审计（2026-11-19）

> 方法：在 8742 上跑真实 viewer（Playwright，1440×900 / 390×844），走
> Work 首页 → 任务线程 → Governance → 团队 → 审计库 → 设置 → 命令面板；
> 同时对照 `docs/claude-code-frontend-teardown.md` 的设计契约和关键源码。
> 截图证据在 `work/ui-audit/`（本地，不入库）。
>
> 本文只列“可复现、有证据、能修”的问题；不是风格偏好清单。

## 0. 结论

FCode 的视觉底子不差（留白、排版、设置页、审计账本都成立），主要问题不在
“好不好看”，而在**状态模型和导航模型**：

- 任务没有 URL/历史，刷新即丢，返回键直接离开应用；
- 移动端抽屉选完任务不关，任务被盖住；
- 失败卡给的是“判断面”，不是“诊断面”，用户看不到为什么失败、下一步修什么；
- Work 里的“新对话”实际进入 Chat，两个几乎一样的 composer 语义不同；
- Governance 的团队/工作台/审计库没有稳定的导航状态，链徽章/底栏还会串台。

一句话：**Claude Code 的前端把“当前在哪个会话、什么模式、什么状态”做成了
一等公民；FCode 目前把这些藏在了 DOM 状态里。**

## 1. 做得好的部分（先保留）

- 首页 stage 的排版、留白、建议 chips 清晰；移动端不横向溢出。
- 设置页是完整的信息架构：外观/委托默认/模型与工具/记忆/快捷键/安全，
  文案解释到位，改动即时保存。
- 审计账本的证据语言专业：授权范围、稳定指纹、负对照、重放阈值、未覆盖面、
  provenance chain；这是产品差异化，不要为了“像 Claude Code”把它压扁。
- 治理团队页的角色卡、待办、频道 rail 结构完整。
- 命令面板有完整键盘操作（↑↓ / ↵ / esc）和快捷键提示。
- 已有 `reduce-motion`、`aria-live`、`role=alert` 等基础可访问性投入。

## 2. P0：功能性缺陷（先修这些）

### P0-1 任务没有深链，刷新/返回即丢

**现象**：点开任意任务后 URL 仍是 `http://127.0.0.1:8742/`；刷新回到首页；
浏览器返回直接离开应用（Playwright 里回到 `about:blank`）。

**证据**：
- 实测 `afterOpenUrl = /`，`afterBackUrl = about:blank`。
- 源码只有审计 hash（`#/audit`），任务打开路径没有 `history.pushState` /
  `location.hash`；`app.js` 的 hash 处理只覆盖 audit。

**影响**：任务不可分享、不可收藏、不可刷新恢复；多任务并行时用户只能靠
侧栏记忆。对比 Claude Code 的 session resume，这是基础能力缺口。

**修法**：加 `#/run/<run-id>`（或 `#/task/<id>`）路由：
- `openRun` 写 `history.pushState`；
- 启动时读 hash 恢复任务，死链回首页并 toast；
- `popstate` 处理前进/后退；
- E2E：打开→刷新→仍在任务；打开→返回→首页；直接深链→任务。

### P0-2 移动端抽屉选完任务不关闭

**现象**：390px 下点“打开导航”展开侧栏，点任意任务后抽屉仍保持
`x=0, width=282`，任务被盖住；用户必须再点右侧空白 backdrop 才能看到任务。
抽屉展开时又盖住了汉堡按钮，没有可见关闭按钮。

**证据**：`work/ui-audit/mobile-task.png`；实测选择后 sidebar rect 仍为
`0,0,282,844`；点击 drawer 外坐标才关闭。

**影响**：移动端“选任务”这个主路径多一步且没有提示，像卡死。

**修法**：任务选择、`新对话`、导航到 Governance/审计后统一调用 `closeNav()`；
给 drawer 加 close 按钮或让汉堡在抽屉内可见；E2E 断言选择后 sidebar 移出视口。

### P0-3 失败卡不给诊断，只给判断表

**现象**：失败任务卡显示：

- 标题 `运行未能完成`
- 原始码 `ARTIFACT_CHECK_FAILED`
- 英文原因重复两遍：`capability stopped in motion: ARTIFACT_CHECK_FAILED`
- 折叠项“失败依据与判断”展开后是**审阅判断表单**（接受/补材料/保留/退回），
  不是失败证据。

**证据**：`work/ui-audit/task-thread.png`；展开文本只有
`JUDGMENT · 判断面 failed · 未产出可签字结论` + 判断选项。
`FAILURE_REASONS` 只覆盖旧 runtime 的 9 个码（`app.js:135`），没有
`ARTIFACT_CHECK_FAILED / QUALITY_STALLED / ACTION_REPAIR_EXHAUSTED_* /
MODEL_CALL_FAILED`；`capability_failed` 的 payload（stage、quality.issues、
required_fixes、artifact）没有进入失败卡。

**影响**：用户知道“失败了”，但不知道卡在哪个阶段、哪条几何/检查失败、
产物在哪、下一步改什么；只能点“重试”烧钱。

**修法**：
- 补 capability failure 码的中文映射；
- 新增“失败诊断”块：stage、reason、quality.issues、required_fixes、
  artifact path/sha、frames/contact sheet、最后一轮 repair ticket；
- 把“失败依据与判断”保留为治理审阅，和诊断块分开；
- 去掉重复的英文 message。

### P0-4 “新对话”与“新任务”语义冲突

**现象**：Work 首页 composer 是“描述任务”，但侧栏“新对话 ⌘N”和快捷键
`⌘N` 实际进入 **FCODE CHAT**（`Bring proof to ideas.`），不是新任务。
两个 composer 外观几乎一样，提交语义不同。

**证据**：Playwright 按 `Meta+n` 后 main 变为 `FCODE CHAT Bring proof to
ideas...`；`newChat()` 由 `⌘N` 路由。之前也发生过“以为在提交任务，实际发到
chat”的真实误操作。

**影响**：用户会在错误 surface 里输入任务，得到错误的路由/预算/产物。

**修法**：拆成两个明确动作——“新任务”（Work composer）与“新对话”（Chat）；
`⌘N` 跟随当前 zone 或改成 `⌘⇧N` 明确新任务；两个 composer 用不同占位文案
和不同模式标签（Task / Chat）。

## 3. P1：信息架构与状态一致性

### P1-1 Governance 的导航状态不跟 surface 走

**现象**：
- 团队页 header `治理团队`、sidebar 高亮的是 `对话`；
- 工作台 header `治理台`、sidebar 仍是 `对话`；
- 审计库是独立 view，sidebar 的 `审计记录` 与右侧 `发现账本` 同时存在。

**证据**：`work/ui-audit/gov-team.png`、`gov.png`、`audit.png`。
`govSurface` 有 team/workbench/bot/dialog，但主 sidebar 只有 对话/审计库。

**影响**：用户不知道自己在哪一层；`对话` 高亮在团队/工作台页是错的。

**修法**：sidebar 的 active 由 `govSurface` 派生；或在 header 做
`团队 / 工作台 / 审计库` 面包屑；把 team/workbench 纳入导航模型。

### P1-2 审计入口重复、默认不选

**现象**：审计库左侧 `审计记录` 列 2 个目标，右侧 `发现账本` 列 3 个 run；
两边计数/粒度不同。进入审计库主区是空态，要求用户“从右侧选择一次审计”，
没有自动选中最近一次。

**证据**：`work/ui-audit/audit.png`；左 2 条（按目标聚合），右 3 条（按 run）。

**影响**：同一批数据两套入口，用户不知道哪边是权威；多一步选择才能看到内容。

**修法**：保留一个权威列表（建议右侧发现账本），左侧按目标聚合时明确标
“目标”而不是“审计记录”；进入时自动选最近 run；空态只在真的没有审计时出现。

### P1-3 任务在侧栏/面板里不可区分

**现象**：8 条任务标题都被截成同一个 `编写单文件HTML，内含SVG动画：卡通鹈鹕…`；
命令面板里 6 条同名，只靠“7 轮/11 轮”区分，没有时间、状态、模型、项目。

**证据**：`work/ui-audit/home-1440.png`、`palette.png`。

**影响**：找历史任务只能逐个点；重名任务无法搜索。

**修法**：侧栏保留截断但加状态/时间；palette 行补 `状态 · 时间 · 模型 · 轮数`；
标题生成时保留可区分后缀（目标/场景/项目），或允许用户重命名后持久展示。

### P1-4 顶栏链徽章与详情事实不一致

**现象**：审计详情已经显示 `Provenance chain 🔗 链完整 (7 events)`，但页头
徽章仍显示 `未加载`。

**证据**：`work/ui-audit/audit-detail.png`。

**影响**：用户不确定链到底验没验；这是治理产品最不该模糊的状态。

**修法**：徽章绑定当前 surface 的链（任务链 / 审计包链），进入详情时自动
验证或显示“已验证”；加载中/失败/未加载三态明确。

### P1-5 底栏状态跨 zone 串台

**现象**：Governance 页底栏仍显示上一个 Work 任务的失败状态与预算
（`deepseek-flash · 失败 calls 7/24 · reads 5/60 · 28,475 µUSD`）。

**证据**：`work/ui-audit/gov.png` 底部状态栏。

**影响**：用户会以为治理任务失败了/花了钱；状态不可信。

**修法**：状态栏按当前 zone/surface 取状态；没有当前运行时显示“空闲”，
或明确标注“上次运行”。

### P1-6 Composer 缺模式语义与状态

**现象**：
- `关键审批` 是按钮但没解释三个档位；`Fast` 是开关但看不出影响；
- 没有 `/`、`@`、`Shift+Tab` 等提示；
- 390px 下模式按钮只剩一个图标，模型名 `deepseek-flash` 折成两行；
- 没有上下文%、队列、后台任务、子代理状态。

**证据**：`home-1440.png`、`home-390.png`。

**修法**：模式 chip 显示文字 + 当前档位 tooltip；加一行上下文提示
（`/ 命令 · @ 文件 · Shift+Tab 模式`）；移动端把模型名缩短/省略、模式保留文字；
状态行加 context/queue/tasks。

## 4. P2：可访问性与细节

### P2-1 关着的移动抽屉仍在可访问树里

sidebar 关闭时只有 `transform: translateX(-102%)`，没有 `aria-hidden`/
`inert`；离屏按钮仍是 `display:flex; opacity:1`，键盘/读屏仍可进入。
**修法**：关闭时 `inert` + `aria-hidden=true`，打开时移除；焦点回到汉堡。

### P2-2 任务行/更多按钮语义不足

`.run-row` 是 `div tabindex=0`，没有 `role=button`；`.row-more` 只有 `title`，
没有 `aria-label`。**修法**：补 role/aria-label，Enter/Space 行为与 click 一致。

### P2-3 跑马灯没有悬停延迟

长标题 hover 立即 `translateX(-45px)`，标题开头被裁掉；选中任务后鼠标仍
停在行上，跑马灯继续跑。**修法**：500ms 延迟启动、active 行暂停、减少动效
时禁用。

### P2-4 设置里的“主题”是静态文本

`设置 → 外观与动效 → 主题` 显示“月之暗面 / 浅色”，但没有控件；用户会以为
可以切主题。**修法**：做成真实 select（浅色/深色/跟随系统），或明确标为
只读环境信息。

### P2-5 审计详情是一整块密集长文

发现、证据、指纹、负对照、重放、链全部平铺；长审计要滚很久。**修法**：
每条 finding 可折叠，默认展开第一条；严重度/处置筛选 sticky；链放到
独立 tab/section。

## 5. 对比矩阵（Claude Code 契约 → FCode 现状）

| 契约 | Claude Code | FCode 现状 | 判断 |
| --- | --- | --- | --- |
| 会话可恢复 | session resume / 深链 | 任务无 URL | ❌ P0 |
| 一个 composer 控制面 | 模式/模型/队列/提示在输入框 | 控件分散，模式语义弱 | ⚠️ P1 |
| 工具卡渐进披露 | 动词+目标，输出折叠 | “执行摘要”整体折叠，无逐工具卡 | ⚠️ P1 |
| 失败可诊断 | 错误、证据、下一步 | 失败卡=判断表，无 quality/fixes | ❌ P0 |
| 权限内联 + steering | 允许/永久/拒绝并反馈 | 审批存在，但失败卡无 steering | ⚠️ P1 |
| 状态透明 | context%、agents、tasks | 只有底栏预算，且跨 zone 串台 | ❌ P1 |
| 导航可预期 | 单一会话流 | 多 surface，active 状态错位 | ❌ P1 |
| 移动/窄屏 | 自适应、降级 | 抽屉不关、composer 拥挤 | ❌ P0/P1 |
| 可访问性 | screen-reader/色盲/ANSI | reduce-motion + aria 有基础，抽屉漏 | ⚠️ P2 |

## 6. 建议切片（按投入产出）

**Slice A：状态与路由正确性（P0，先做）**

1. 任务深链 + 刷新恢复 + 返回栈；
2. 移动抽屉选择后关闭；
3. 失败诊断块 + failure 码映射；
4. “新任务/新对话”拆开，⌘N 不再误导。

**Slice B：导航与列表（P1）**

5. Governance active 状态跟 surface；
6. 审计列表去重 + 自动选中；
7. 任务/palette 补状态、时间、模型；
8. 链徽章与底栏 scope 修正。

**Slice C：Composer 与可访问性（P1/P2）**

9. 模式 chip/提示行/移动端布局；
10. 抽屉 inert、semantic rows、marquee 延迟；
11. 主题控件或明确只读。

## 7. 证据清单

- `work/ui-audit/home-1440.png` — 首页/侧栏/composer
- `work/ui-audit/task-thread.png` — 失败任务线程
- `work/ui-audit/gov.png` — 治理对话 + 团队 rail + 串台底栏
- `work/ui-audit/gov-team.png` — 团队页 + sidebar active
- `work/ui-audit/audit.png` — 审计库空态 + 双列表
- `work/ui-audit/audit-detail.png` — 审计详情 + 链徽章不一致
- `work/ui-audit/mobile-task.png` — 移动抽屉未关闭
- `work/ui-audit/palette.png` — 命令面板同名任务
- `work/ui-audit/settings.png` — 设置页
- 实机断言：URL/历史、sidebar rect、marquee transform、移动端 overflow。
