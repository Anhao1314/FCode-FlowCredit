# Claude Code 前端设计逆向（2.1.302 · CLI/TUI）

> 目的：拆交互与视觉模式，不复制源码/资产。Claude Code 是专有软件；本文只记录
> 可从行为、字符串和公开界面观察到的设计契约，供 FCode 自研前端参考。
>
> 证据：本机 `~/.local/share/claude/versions/2.1.302`（Mach-O，Bun 编译）的
> 可读字符串与内嵌 JS 片段；临时 `HOME` 下真实 TUI 首启捕获（未登录、未读取
> 用户会话）。桌面/网页壳是另一个前端，本文不覆盖。

## 0. 一句话结论

Claude Code 的前端不是“终端里的聊天框”，而是**一个键盘优先、状态透明、
渐进披露的会话工作台**：一个 composer 承载全部控制，工具调用是紧凑卡片，
权限是内联决策，状态（模式/上下文/后台任务/子代理）始终可见，视觉上克制、
人格上不克制（ASCII 吉祥物 + 200 个 spinner 动词）。

## 1. 表面地图

```text
首启
├─ 主题选择（7 种，实时 diff 预览 + 语法主题）
├─ 登录方式（Claude 订阅 / Console API / Bedrock·Vertex·Foundry）
└─ 信任目录 / 项目初始化

主会话
├─ 欢迎区（窄屏：标题+版本；宽屏：ASCII 吉祥物 + 版本 + 提示）
├─ 对话流
│  ├─ 用户消息（浅色块 / hover 态）
│  ├─ 助手消息（流式）
│  ├─ 工具卡（动词 + 目标 + 状态；输出折叠，ctrl+o 展开）
│  ├─ diff 卡（行号、+/-、语法高亮、二进制/超大文件降级）
│  └─ 子代理 / 后台任务 / 队友状态
├─ 权限卡（内联：允许一次 / 会话允许 / 永久规则 / 拒绝并给反馈）
├─ 队列条（运行中可继续输入；↑ 编辑队列）
├─ Composer（模式、模型、提示行、粘贴、图片）
└─ 状态行（模式、上下文%、token、成本、模型、任务数）
```

## 2. 视觉系统

### 2.1 颜色 token（源码中的命名角色）

| token | 示例值 | 用途 |
| --- | --- | --- |
| `claude` | `rgb(215,119,87)` | 品牌橙：标题、主强调 |
| `claudeShimmer` | `rgb(245,149,117)` | 流式/加载的呼吸高亮 |
| `permission` | `rgb(87,105,247)` | 权限/审批蓝 |
| `permissionShimmer` | `rgb(137,155,255)` | 审批等待动画 |
| `planMode` | `rgb(0,102,102)` | Plan 模式深青 |
| `autoAccept` | `rgb(135,0,255)` | 自动接受编辑紫 |
| `bashBorder` | `rgb(255,0,135)` | 命令执行的粉色边框 |
| `promptBorder` | `rgb(138,138,138)` | composer 边框 |
| `clawd_body` / `clawd_background` | `rgb(215,119,87)` / `rgb(0,0,0)` | ASCII 吉祥物 |
| `userMessageBackground` | `rgb(240,240,240)` | 用户消息块 |
| `composerSidebarBackground` | `rgb(245,245,245)` | composer 侧栏 |
| `selectionBg` | `rgb(180,213,255)` | 选区 |
| `memoryBackgroundColor` | `rgb(230,245,250)` | 记忆/上下文卡 |

设计含义：**颜色是语义角色，不是装饰**。同一个“等待审批”在 spinner、边框、
状态行里都用 permission 蓝；plan/auto-accept 各有独立色相，用户扫一眼就知道
当前模式。

### 2.2 主题与降级

- 7 个首启选项：Auto（跟随终端）、Dark、Light、Dark/Light colorblind-friendly、
  Dark/Light ANSI-only。
- 主题选择页**不是纯色板**：右侧实时渲染一段代码 diff，让用户在当前终端里
  直接看可读性；语法主题默认 Monokai Extended，`ctrl+t` 可关。
- 全部颜色有 ANSI 回退（`ansi:redBright` 等），不假设真彩色。
- `Apple_Terminal` 有单独渲染分支；窄终端/矮终端退化为“标题 + 版本”的一行
  欢迎，不硬塞 ASCII art。
- 有 `--ax-screen-reader`：扁平文本、无装饰边框、无动画。

### 2.3 排版与密度

- 终端 cell 网格：58 列是宽屏欢迎区的固定内容宽；行数 <30 直接切窄版。
- 边框克制：单线/虚线（`╌`）用于分区，不用重边框包裹整页。
- 层级靠 **dim / bold / 颜色角色**，不靠字号（终端没有字号）。
- 一屏内优先放“当前动作 + 最近结果”，历史通过滚动/transcript 查看。

## 3. 欢迎区：品牌与降级

- 宽屏：`Welcome to Claude Code` + `v2.1.302` + ASCII 吉祥物（Clawd）+ 一行
  提示；吉祥物由 `clawd_body` / `clawd_background` 两个角色上色。
- 窄屏/矮屏：只有标题 + 版本，不显示 art。
- 浅色主题、colorblind、ANSI、Apple Terminal 各有独立 art 变体。
- 首启主题页用“真实代码 diff”做预览，而不是色块；这直接降低选择成本。

## 4. Composer：唯一控制面

Composer 不是一个输入框，而是**模式 + 上下文 + 队列**的复合控件：

- 模式：`accept edits on` / `plan mode on` / `bypass permissions` / `auto`；
  `Shift+Tab` 循环。模式不是设置页里的开关，而是常驻可见状态。
- 提示行（按上下文出现，不是一次全列）：
  - `/ for commands`
  - `@ for file paths`
  - `/btw for side question`
  - `double tap esc to clear input`
  - `auto-accept edits`
  - `for verbose output`
  - `toggle tasks`
  - `ctrl+z suspend`
  - `paste images`
  - `switch model`
  - `toggle fast mode`
  - `stash prompt`
  - `edit in $EDITOR`
  - `/keybindings to customize`
- 多行：`shift+enter` / `Option+Enter`；外部编辑器 `ctrl+g`（可改 `ctrl+e`）。
- 运行中继续输入：消息进队列；`↑` 编辑队列，`Enter` 立即发送；
  “send now” 是显式动作。
- 图片粘贴 `ctrl+v`；模型选择 `meta+j`；历史 `ctrl+r`；详细输出 `ctrl+o`；
  后台会话 `ctrl+b`。

关键取舍：**所有控制都在输入框附近**，没有“设置 → 模式 → 保存”的页面跳转。

## 5. 对话流：工具卡与 diff

- 工具卡是“动词 + 目标”的紧凑行，不是 JSON 转储；输出默认折叠，`ctrl+o`
  展开完整输出。
- diff 卡：行号、`+/-`、语法高亮；明确处理
  `Binary file`、`Diff too large`、`Large file - diff exceeds 1 MB`、
  `No code changes`、`untracked`。
- 长输出/长会话有 pager/transcript；`ctrl+o` 是“展开”而不是新页面。
- 子代理：`/tasks to see subagents`、`1 idle agent`；队友有独立状态行。
- 后台任务：`ctrl+b` 把会话移到后台；`Background this session?` 明确列出
  “will be stopped / will carry over”；`/tasks` 查看全部在飞任务。

## 6. 权限：内联决策 + 转向

权限卡不是“允许/拒绝”二选一，而是**带记忆和反馈的决策点**：

- `Yes` / `Yes, and don't ask again for <pattern>` / `No, and tell Claude what
  to do differently`。
- 会话级：`Allow for this session (N)`；管理策略可锁死为只允许一次/直接拒绝；
  `suppressAlwaysAllowRule` 会隐藏永久规则选项，避免“一次批准变成全工具放行”。
- `shift+tab to approve with this feedback`：拒绝不是终点，反馈直接进入下一轮。
- 权限模式：`default`、`acceptEdits`、`plan`、`bypassPermissions`、`dontAsk`、
  `auto`（模型分类器先评估风险，低风险自动跑、其余拦截）。
- Computer Use 有专门的 app 授权卡：列出要控制的应用、隐藏应用数量、
  请求的 flags；拒绝同样可以附带“告诉 Claude 怎么改”。

设计含义：**把审批做成对话的一部分**，而不是弹窗打断；拒绝要能变成下一步
指令，否则用户只会反复点“No”。

## 7. 状态与反馈

- 上下文：`% context used`、`% until auto-compact`、`Context low (% remaining)
  · Run /compact to compact & continue`；状态行可自定义。
- 用量：每轮 token、累计 session tokens、成本、elapsed；状态行有
  `statusWidth` / `statusParts` 的槽位设计。
- 任务：`1 idle agent`、`/tasks`、后台任务更新、`/tasks` 面板。
- Spinner：约 200 个动词（Accomplishing、Actioning、Clauding、Pondering、
  Noodling、Smooshing…）轮换；还有随机的 spinner tip。人格化只发生在等待时，
  不占用结果区。
- 自动继续：`auto-continue in Ns · any key to stay`，给用户一个可撤销的默认。

## 8. 工程实现观察

- React + Ink 风格的终端组件树；源码里有明显的 memo 缓存（按 props/主题
  缓存渲染结果），说明终端高刷新率是性能约束。
- 主题是一个角色表 + ANSI 回退表；组件只引用角色名，不直接写 hex。
- 键盘绑定是数据：`keybindings.json`，按 context（Global/Chat）分作用域，
  可覆盖、可备份。
- 可访问性不是补丁：screen-reader 模式从渲染层切换；colorblind/ANSI 是
  一等主题；窄终端/Apple Terminal 是布局分支。
- 终端前端没有 DOM/CSS；它的“设计系统”是角色色 + 字符画 + 布局算法。

## 9. FCode 该借什么、不该抄什么

### 借（模式，不抄像素）

1. **一个 composer 承载模式/模型/上下文**：FCode Work 顶部应有模式 chip
   （accept / plan / ask）与上下文%，而不是把模式藏进设置。
2. **工具卡 = 动词 + 目标 + 折叠输出**：task-thread 里现在是事件卡/交付卡，
   可把普通工具调用压成一行，点击展开。
3. **内联审批 + 拒绝即反馈**：FCode 已有 approval 卡，补上
   “拒绝并告诉我怎么改”的输入，不要只有 approve/reject。
4. **状态透明**：上下文%、token/成本、后台任务、子代理数常驻；FCode 已有
   artifact delivery，可加一条 status line。
5. **队列/steering**：运行中继续输入，↑ 编辑队列；这是“对话即控制面”的关键。
6. **渐进披露**：默认折叠长输出/diff，`ctrl+o` / 展开按钮；不要把所有证据
   一次铺满。
7. **降级**：窄屏、减动效、色盲/高对比主题；FCode 是 web，可以用 CSS
   media query + `prefers-reduced-motion` 做等价物。
8. **人格化只在等待时**：吉祥物/spinner 动词可以学，但不要复制 Clawd 资产；
   FCode 需要自己的角色。

### 不抄

- 不复制 ASCII 吉祥物、具体色值组合、文案原文、图标资产。
- 不把终端 cell 网格硬搬到 web；FCode 有真正的排版能力，应该用响应式布局，
  而不是模拟 58 列。
- 不复制“200 个 spinner 动词”这种规模；选 12–20 个符合 FCode 语气的即可。

## 10. 给 FCode 的落地切片（建议顺序）

**P0（一周内可做）**

- Work 顶部 status line：模型、模式 chip、上下文%、队列数、后台任务数。
- Composer 提示行：`/` 命令、`@` 文件、Shift+Tab 模式循环；窄屏隐藏次要提示。
- 工具卡紧凑化：`动词 + 目标`，输出默认折叠，展开看详情。
- 审批卡加“拒绝并给反馈”输入；保留 allow once / always / deny。

**P1**

- Diff 卡：行号、+/-、语法高亮、二进制/超大文件降级。
- 运行中队列条 + ↑ 编辑；发送 now。
- 上下文警告（80% / 95%）与 `/compact` 入口。
- 子代理/后台任务 badge，点击筛选 `/tasks` 等价面板。

**P2**

- 主题角色表（FCode 品牌色 + ANSI/高对比/色盲变体）。
- 可访问性：`prefers-reduced-motion`、纯文本模式、键盘全导航。
- 可配置 keybindings（先做 Global/Chat 两个 context）。

## 11. 证据与限制

- 字符串来自 2.1.302 二进制；内嵌 JS 片段可读，但 minified，无法证明每个
  字符串都映射到当前 UI。
- TUI 实机捕获只在临时 HOME 的首启流程（主题/登录方式）；主会话需要登录，
  未读取用户账号或会话。
- 桌面端 `Claude.app` 是 Electron 壳，前端资源在 `app.asar`；如果要拆
  “Claude Code 桌面/网页前端”，需要另做一轮（登录后的 claude.ai/code 或
  asar 里的 React 组件）。
- 本文只记录设计模式，不含可直接复用的专有代码或资产。
