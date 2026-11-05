# Kimi 皮肤：外壳与对话面

**管辖范围**：应用外壳与对话面——侧栏、顶栏、composer、对话线程与气泡、模型菜单、
设置页、空状态、动效。这一面以 Kimi 桌面端/网页版的**实测值**为准，所有数字都可追溯到
它自己的样式表（见文末的 asar 提取一节）。审计底稿与证据面**不适用**本文件，那份规范是
[design-system.md](design-system.md)（Paper & Proof）。两面分流的理由写在根 README。
最后核对：2026-10-26。


这份文档记录 2026-10-25 在 kimi.com（1600×1000 首页 + 对话页）上直接读取的
计算样式，以及它们如何映射到 FCode。几何测量见 `desktop-design.md`；这里只写
颜色、圆角、阴影、字体与动效。

## 色号

| 用途 | Kimi 实测值 | FCode token |
| --- | --- | --- |
| 页面 / 侧栏画布 | `#FBFAF9` | `--canvas` |
| 次级面（用户气泡、卡片、悬停） | `#F5F5F5` | `--panel` / `--inset` |
| 主表面（输入框、菜单、新建对话） | `#FFFFFF` | `--raised` |
| 主文字 | `rgba(0,0,0,.90)` | `--text-1` |
| 次级文字 | `rgba(0,0,0,.60)` | `--text-2` |
| 弱化文字 / 侧栏历史标题 | `rgba(0,0,0,.45)` | `--text-3` |
| 禁用 / 占位 | `rgba(0,0,0,.30)` | `--text-4` |
| 分隔线 | `#00000021` ≈ `rgba(0,0,0,.13)` | `--line-strong` |
| 输入框边框 | `rgba(0,0,0,.09)`（对话页） | `--line` |
| 强调 / 焦点 / 链接 | `#1783FF` | `--accent` |
| 正向 | `#16C456` | `--ok`（正文用 `--ok-ink`） |
| 警告 | `#FF9500` | `--warn`（正文用 `--warn-ink`） |
| 危险 | `#FF3849` | `--bad`（正文用 `--bad-ink`） |

状态色只用于圆点、图标与细边框；正文仍用更深的 `--*-ink`，保持白底可读性。
`--accent` 换成 Kimi 蓝后，焦点环、链接和选中态自动跟随；发送键仍保持 Kimi
的黑色实心按钮。

## 圆角与阴影

| 组件 | Kimi 实测 | FCode |
| --- | --- | --- |
| 新建对话 | 40 高 / r12 / 0.5px `rgba(0,0,0,.13)` | `.side-new` |
| 导航行 | 36 高 / r8 / 0 8 内边距 | `.side-desk` / `.nav-item` |
| 历史对话行 | 28 高 / r8 / 文字 45% | `.chat-row` |
| 输入框 | 768×114 / r24 / 1px `.09` | `.composer-box` |
| 输入框阴影 | `0 4px 12px rgba(0,0,0,.03)` + `0 5px 16px -4px rgba(0,0,0,.07)` | `--shadow-stage` |
| 用户气泡 | r12 / padding 10×12 / `#F5F5F5` | `.chat-bubble` |
| 菜单 | r12 / 轻描边 / 软阴影 | `.tpl-menu` / `.model-menu` |

对话正文 16/26，编辑器 16/24，消息列 780，输入框 768；这些与 Kimi 的
`--markdown-B1_Content`、`.chat-editor-content`、`.chat-content-list` 一致。

## 双主功能：工作 / 对话

对齐 Kimi 桌面端（3.2.17 实测）：**一个 shell、两套侧栏、两套 composer 能力**。

- 侧栏顶部 `[工作 | 对话]` 分段开关（32px 行、active 白底 + 0.5px 描边），
  切换时侧栏内容与主区一起换，外壳框架不动；
- 对话模式：主按钮「新对话」+ 对话列表；composer 无治理芯片，
  占位语「给 FCode 发消息…」，空态不显示模板 chips、wordmark 无徽章；
- 工作模式：主按钮「新建工作」+ 搜索/看板·账本/判断记录/定时任务 +
  任务列表（状态点）+ Projects；composer 显示审批档位芯片，
  占位语「给 FCode 派一个任务…」，空态显示 8 个模板 chips、
  wordmark 挂黑色 `WORK` 徽章（KIMI WORK 同款）；
- 模式状态存 `fa.uiMode`；⌘N 新对话、⇧⌘N 新建工作；
- 切换用"软拆离"：拆掉对话界面件但保留 `chat.current`，
  往返后线程从内存恢复（流式中切换会中止并记录 `chat_cancelled`）；
- 治理边界不变：对话模式的写操作永不直接执行（只读工具除外），写操作桥接只有显式的「转为委托」。

## 字阶与元素比例

Kimi 的 UI 字阶（`:root` 里的 `--ui-*`）：

| 层 | 字号/行高 | FCode 用途 |
| --- | --- | --- |
| T1 | 18/26 | 会话标题、设置标题（`--fs-5`） |
| T2 | 16/24 | 顶栏标题、编辑器、行内码 |
| B1 | 15/22 | 强调正文（`--fs-4`、空状态提示） |
| B2 | 14/20 | 默认 UI（`--fs-3`/`--fs-2`、侧栏、菜单行） |
| C1 | 12/18 | 计数、时间、预算行、`chat-meta`、操作按钮 |
| C2 | 10/14 | （保留给更小的徽标） |

Markdown 字阶：正文/行内码 16/26、代码块 14/22、H2 20/32、H3 18/28——
FCode 的聊天 Markdown 与此逐项对齐；行内码与正文同字号是 Kimi 的刻意选择。

元素比例：导航行 36 / 历史对话行 28 / 用户行 32 / composer 操作行 36 /
模型浮窗 290 / 新建对话 40。toast 用 Kimi PC 端的深色 `#2B2B2B`。
思考强度块改为 Kimi 规格行的形态：label 14/20 + 右侧 28/r8 档位胶囊。

## 运行证据视图（"完整研究"重构）

旧版每轮是三张描边卡片（提议卡 + 结果卡 + 收据行）加一条时间线竖轨，信息密度
高但形态老化。Kimi 对过程内容的形态是**安静的摘要行 + 展开细节**，照此重构：

- 每轮压成一行 40px 摘要：`图标 · 第 N 轮 · 工具名 · 状态点(PASS/DENIED/等待)
  · 预算增量 · 展开箭头`，悬停 `#F5F5F5`，默认收起（DENIED / UNKNOWN /
  运行中的轮次默认展开）；
- 展开体缩进对齐：参数行 12/18、门控行（圆点带语义色、文字保持 60% 中性）、
  执行结果、计划修订、收据行 12/18；
- 去掉时间线竖轨与所有盒中盒；输出井与原始 JSON 变 `#F5F5F5` 圆角井；
- 结果卡/计划卡同语言去盒，标题 14/20、指标等宽字体保留；
- 治理语义一字不减：门控判定、结果哈希、tokens/µUSD/ms、`receipt …` 对账、
  "未知不等于失败"都在，只是从卡片改成了行。

内容列与对话同宽（832 容器 / 780 内容），让"委托运行"和"对话"共用一个阅读节奏。

## 浮窗锚点（模型 / 审批菜单）

Kimi 的模型浮窗（`.kimi-menu-positioner` + `.kimi-menu`）实测：

- **右缘与触发芯片右缘严格对齐**（浮窗 right 1198 == 芯片 right 1198），
  垂直间隙 4px（芯片下方 504 = 芯片底部 500 + 4）；
- 面板 290 宽、r16、无外边框，`inset 0 0 0 0.5px rgba(0,0,0,.13)` 的发丝描边
  加 `0 4px 16px rgba(0,0,0,.10)` 软阴影，内边距 8；
- 进入动画：`scale(.85) → 1`，原点取**离芯片最近的角**，opacity 同步，
  `.15s cubic-bezier(0,0,.2,1)`；
- 行结构：模型名 14/20（90% 黑）+ 描述 12/18（45%），标签色 `#1783FF`。

FCode 对应实现（`kimi.css`）：`#model-menu { right: 54px }`——54 = 操作行右
内边距 10 + 发送键 36 + 间距 8，即芯片右缘到输入框内缘的距离；审批浮窗镜像为
`left: 54px`。停靠态向上展开（视口底部没有向下空间），原点取右下 / 左下角。
480px 以下退回贴边，避免越出画布。

## 动效

Kimi 的动效很克制，只有三档：

| 场景 | 曲线 | FCode |
| --- | --- | --- |
| 背景 / 颜色 / 阴影 | `.15s` 线性（个别用 ease-in-out） | `--t` + 组件规则 |
| 图标底色 | `.3s ease-in-out` | `.c-btn` / `.c-icon` |
| 位移 / 缩放 / 透明度 | `.15s cubic-bezier(.23,1,.32,1)` | `--ease` |
| 侧栏开合 | `.3s ease-in-out` | `.sidebar` / `.sidebar-frame` |

具体符号：

- **5 点螺旋加载**：Kimi 的 `core-spiral-loading-dot-*` 只改变透明度
  （0.149 → 0.451），不上下跳。FCode 用 5 个 `<i>`、每点错开 `.12s`，
  `kimiDot 1.2s linear infinite`。
- **状态呼吸**：运行中的圆点用 `kimiPulse`（0.45 ↔ 0.85，alternate），
  取代原来的闪烁。
- **等待额度**：`chat-wait` 用渐变文字 `kimiShimmer`，对应 Kimi 的
  `shimmer` / `skeleton-loading`。
- **菜单进入**：`kimiFade`（透明度 + 4px 位移 + 0.99 缩放，`.15s`）。
- **新建对话图标**：悬停时放大 1.12；输入框的 “+” 悬停时旋转 90°，
  打开后换成 “×”；发送箭头悬停上移 1px。
- 所有动效都尊重 `body.reduce-motion` 与 `prefers-reduced-motion: reduce`。

## 加载顺序与边界

```text
app.css        令牌 + 外壳几何
design.css     组件视觉
chat.css       对话消息
desktop.css    Kimi 测量比例（html.desktop-app）
kimi.css       Kimi 色号 + 动效（最后加载，只覆盖皮肤）
```

`kimi.css` 不改变布局和治理语义：运行时间线、审批卡、预算、链验证仍是
FCode 自己的证据界面；Kimi 皮肤只负责外壳、对话、输入框和菜单。品牌标志、
FCode 的域模块与运行入口刻意保留，不做成 Kimi 的复制品。

## 结构对齐 F1：一组设置、一个模型目录

**问题**：设置页保存的 provider 只喂对话；委托运行的模型来自宿主环境变量。
于是应用里存了 Kimi key，Work 的模型菜单仍然写 `KIMI_API_KEY is required`——
用户看到的就是"work 的模型选择没有 kimi"。

**做法**：`providers/catalog.settings_as_env()` 把设置页的字段翻译成预设的
环境变量名（`base_url` 故意不在表里：云端端点不能从设置页改，防的是 key 外泄），
`merged_env()` 把它叠在环境之上（**设置 > 环境 > 默认**，与对话侧一致），
然后三个注入点共用同一份视图：目录（`provider_catalog(settings_env=…)`）、
解析（`RunManager.resolve_model`）、构造（`build_provider(env=…)`）。
`RunManager.settings_env()` 按需读配置文件，保存后无需重启。

**代价与边界**：价格日期 `price_as_of` 进入设置 schema，对话与委托都要求它——
预算顶不能建立在猜来的价格上，这条规则委托侧一直有，现在两侧一致（缺它时
两个菜单都不可用，理由里点名 `KIMI_PRICE_AS_OF`）。没有设置时行为与从前完全
一样（Host 环境声明仍然有效）。key 依旧只落在本机配置文件（0600）、只回显尾 4 位。

**顺带修掉的两个真 bug（就是"看不到 kimi"的直接原因）**
- 舞台态 composer 贴近页面下沿时，模型浮窗一律向上展开，顶部越出画布——
  最上面几行（mock、local、Kimi）直接不可见也不可点。现在打开后量一次高度，
  放不下就向下展开（`#model-menu.drop-down`）。
- `.tabs` 带 `hidden` 属性但被 CSS 拉成 `display:flex`，留下 1040×7 的透明带压
  在浮窗顶部，`elementFromPoint` 命中的是它而不是菜单行。`.tabs[hidden]` 现在
  真的消失（与 `.stage-chips[hidden]` 同一个坑，第二次踩）。

**设置页新增**：Kimi/DeepSeek 卡片补「价格日期」「价格来源」两个字段，卡片头部
说明这组配置同时用于对话与委托；卡片上方一行如实报告配置来源与时效——
`配置文件：<路径> · 最后修改 … · 本进程已加载最新配置 / 配置文件比本进程新
（本进程加载于 …）——重启后生效`。后者正是"我明明存过 key"那类困惑的答案。

## 结构对齐 F1 的验收把三个真缺陷逼了出来

**① 蓝图预填了"本该由模型撰写"的内容。** 通用域的脚本蓝图把这一份
checkout 的演示脚本（fibonacci 首版 + 它的锚定修复）写进了 todo 的
arguments。mock 需要它来保持确定性，但真实 provider 收到的是"照抄这段内容"
的指令——于是**每一次真实的脚本委托都在跑演示脚本**，与操作员的任务无关。
我此前那次"成功"的 Phase A 验收也是：插话要求的文件名写对了，可脚本内容
仍是 fib；报告里还带着"fib(10) = 34 → 55"的固定判读。现在蓝图只承载意图
（路径、命令），撰写内容与锚点由提议者给出；mock 在 `providers/mock.py` 里
自带夹具；`_results()` 只机械地并排两次运行的输出与提议者用过的锚点，不再
替任何任务下结论。回归测试：`BlueprintHonestyTests`（蓝图无夹具符号 +
提议者自己的内容落到磁盘）。

**② 推理模型把补全预算吃光。** 真实运行报 `MODEL_CALL_FAILED: returned empty
content`——k2.6 的 reasoning 内容有 9653 字，`content` 是空的。现在这一条失败
被单独命名（"补全预算被推理占满（reasoning N 字）；把
KIMI_MAX_COMPLETION_TOKENS 调大后重试"），预设默认从 512 提到 8192（实测：
512 必炸，4096 在长轮次上也会炸）。

**③ 没选档位时被强制 medium。** `resolve_model` 在操作员未选档位时返回
"medium"，覆盖了 provider 预设自己的低档——成本与推理长度一起上去，正是②
的助推器。现在"不选"= 用 provider 默认档，菜单里的说明也改成同一句话。

**验收**（真实 Kimi，5 轮，$0.17，证据 `work/kimi-e2e/f1-settings-only-run/`）：
服务进程**没有任何 KIMI_\* 环境变量**，只靠设置页保存的 provider；Work 模型
菜单里 Kimi 可选（不灰）；运行写出的脚本是操作员要求的三行问候语，`results.md`
如实归档两次运行的真实输出。

**顺带修掉的两个真 bug**

## 结构对齐 F2：运行详情 = 线程 + 内嵌块

Kimi 的任务详情就是对话线程：一条回答 = 消息 + 工具块（`toolcall-rollup`
把多次工具调用收成一行摘要，展开看每段）。FCode 的运行详情原本是一张
"证据表"：每一轮一行、右边挂着预算增量，看上去像日志而不像回答。

现在同一个页面读起来是：

- **线程开头是任务**（`.msg.objective` + 模式/模板 chips）——Kimi 里那是用户
  那条消息；此前我们把它当文档标题。头部改成**短名**（重命名或截断的目标），
  完整目标只出现在消息里。
- **过程收成一行 rollup**：连续"安静"的轮次折成
  `已执行 N 步 · 全部通过 · write_file → run_command → …`，点开就是每一轮
  （参数/门控/结果/收据照旧）。"安静"的定义在 `core/round-fold.js`：已执行、
  已通过、没有插话、不是拒绝/未知/进行中——**任何需要操作员看见的轮次一律
  不折**。折叠从不隐藏治理事实，只是不再把"calls +1 · reads +1"重复 N 遍
  （那条挪进了展开体）。
- **审批卡与排队插话移到线程尾部**（实时边沿）：审批卡渲染在 `#turns` 内，
  插话面板同理——位置就是"运行此刻所在的地方"。已决定的门控本来就在轮次内联。
- **插话气泡留在原位**并写着"已入链 · 第 N 轮前生效"：那是真的生效（见 Phase A
  的上下文通道），不是"排队中"的模糊说法。
- 证据 rail 仍在，默认收起；头部一行 meta + 一个"事件 / 链验证"按钮。

折叠是**可见性**操作，不动任何 `data-*` 契约：`syncLiveRounds` 仍按索引打补丁，
折叠状态由 `applyRoundFolding()` 在每次绘制后按当前分组重算，所以流式运行里
组会随新轮次生长，而"拒绝/未知"一旦出现就把那一组拆开。

验收（真实 9 轮运行 `run-20261024-180908-ba5c`）：详情页从 9 行日志变成
任务 → 一行"已执行 9 步 · 全部通过 · list_documents → read_document → …" →
结论卡；点开可看到 9 轮的参数、门控、结果与收据。纯规则测试 8 项
（`viewer/tests/round-fold.test.js`）。

## 结构对齐 F3：侧栏条目与 Kimi 一一对应

Kimi 侧栏的节奏是：**新建 → 功能行 → More → Projects → 列表**。我们此前把
两个治理页（看板·账本、判断记录）摆在功能行里、Projects 又排在任务列表之后，
读起来像后台而不是产品。

现在工作侧栏是：

```
＋ 新建工作
🔍 搜索任务…
📅 定时任务            ← Kimi 的 Scheduled，同级
⋯ 更多 ▾              ← 折叠：看板·账本 / 判断记录
Projects         0     ← Kimi 把 Projects 放在列表之前
  ＋ 新建项目
任务             2
  昨天 2
  [运行行：状态点 · 名字 · 模板 · 轮数 · 成本]
```

- **「更多」是折叠的**（`fa.sideMore` 记住状态），治理页因此可达但不占前门；
  打开账本会自动展开它，免得"页面在哪"成为谜。与舞台的「演示场景」折叠同一个
  手法：`.more-nav[hidden] { display: none !important }`——这个坑本项目踩过两次
  （`.stage-chips[hidden]`、`.tabs[hidden]`），所以新折叠一律显式写这条。
- **Projects 移到任务列表之前**，与 Kimi 的顺序一致；项目内计数与"新建项目"
  行保持原样。
- 对话侧栏不动（新对话 → 对话列表），与 Kimi 的 Chats 同形。

**设置章节**同步扩到"应用的六个面"：外观与动效 / 委托默认 / 模型与工具 /
**记忆与产物** / 通知与快捷键 / 安全与关于。记忆页多了**产物**计数
（`/api/memory` 现在返回 artifacts 的 active/total，来自已接线的 `ArtifactBook`），
为 F4 的产物浏览器留好位置；桌面与调度两节等 F6 与真调度器落地再进设置，
不摆空页面。

## 验收

在 `http://127.0.0.1:8643/?desktop=1` 上逐页检查：

```sh
# 计算样式对比（body / sidebar / composer / bubble / send）
# 首页、对话、运行详情、设置、模型菜单、附加面板、900px 窄屏
```

浏览器实测：`body #FBFAF9`、`.sidebar #FBFAF9`、新建对话 `224×40 / r12`、
导航行 `224×36 / r8`、输入框 `768×114 / r24`、用户气泡 `#F5F5F5 / r12`、
助手正文 `16/26`、发送键 `36×36`；无控制台错误、窄屏无横向溢出。

## 侧栏复刻：数值来自桌面 app 自己的样式表

「按 Kimi 桌面端复刻侧栏」这一步没有靠目测。Kimi 桌面端（本机 3.2.20，
bundle id `com.moonshot.kimichat`）是 Electron 应用，渲染层资产就在
`/Applications/Kimi.app/Contents/Resources/app.asar` 里；asar 是「头部 JSON 索引
＋连续字节」的简单格式，用标准库 `json + struct` 就能按偏移读出其中的 CSS
（`out/renderer/assets/kimi-agent-*.css`，157 KB）。下面每个数字都出自那份样式表：

| 元素 | 桌面端实测 | 出处（选择器） |
| --- | --- | --- |
| 侧栏宽 | 240px | `--work-sidebar-width` |
| 侧栏内边距 | `9px 8px 8px` | `.sidebar` |
| header | 高 36（mac 上是 52 的系统标题栏高度），`margin: 0 -8px -3px` | `.sidebar-header` |
| 折叠钮 | left 8、居中于 y=32、32×32 | `--work-sidebar-toggle-left/top` |
| 品牌标记 | 距折叠钮 +36，gap 4 | `.sidebar-header-marks` |
| flavor 徽章 | 高 18、padding 0 6、r5、KMBlue 底白字 11/600 | `.sidebar-flavor-badge` |
| 行 | 高 36、padding 8、gap 6、**r12** | `.nav-item` |
| 行悬停/选中 | Fills-F1 `rgba(0,0,0,.03)` / Fills-F2 `rgba(0,0,0,.05)` | `.nav-item:hover/.active` |
| 分组标题 | 12/18、Tertiary `.45`、padding 4 8 | `.workspace-section-title` |
| 滚动区 | 上下各 24px 渐变遮罩 | `.sidebar-scroll-top-mask` / `.sidebar-footer::before` |
| footer | padding 8、`margin: 0 -8px -8px`、gap 6 | `.sidebar-footer` |
| 分段开关 | 外 36/r10/F2 打底/padding 2；tab 32/r8、padding 0 12、14/20 | `.segmented-tabs--md` / `.segmented-tab` |
| 标签色 | Primary `.9` / Secondary `.6` / Tertiary `.45`；KMBlue `#1783ff` | `--Labels-*` / `--Colors-KMBlue` |

**两条没抄的**：① 负外边距——桌面端那条 `margin-right: -8px` 属于「包住 padding
的外层容器」（滚动条贴窗外缘），而我们的 `.side-list` 本身就是带 padding 的那一层，
照抄会把每行撑宽 8px；② mac 上 header 的 52px——那是它自绘标题栏的高度，我们用
系统标题栏，取 36 的常规值。

实现要点：`desktop.css` 用 `html.desktop-app .sidebar` 这类选择器抬了优先级，
所以复刻块里每条规则都写成「单类名 + `html.desktop-app` 前缀」两遍，
否则会被桌面模式的旧值盖掉（第一次就是这样：`.sidebar { padding }` 明明在文件末尾
却不生效）。舞台页的 wordmark 不再出现在侧栏（桌面端 header 只有 logo + 徽章），
侧栏顶部改成 logo 32 + WORK/CHAT flavor 徽章，`⌘K` 降为右上角的安静按钮。

## 侧栏是两套：工作（Kimi Work）与对话（网页版）分别复刻

「把左边侧栏按 Kimi 复刻」还有一个前提没写下来：**Kimi 桌面端的「工作」和「对话」
并不是同一个侧栏**。证据来自应用自己的代码，而不是目测：

1. `out/renderer/assets/kimi-agent-*.js`（980 KB）里有一个 `__name:"AppSidebar"`
   的组件——把它按字节解出字符串后读它的渲染函数，就是**工作侧栏**的权威结构；
2. 同一个 bundle 里 `KimiAgentChat` 渲染 `AppSidebar`，而 `SidebarModeTabs` 的
   两个分段是 `[{value:"studio",label:"Work"},{value:"kimi",label:"Chat"}]`；
   点「对话」执行的是 `closePage()`（遥测名 `close_kimi_agent_page`）——也就是
   **关掉 kimi-agent 页面，回到网页版 chat**。所以对话侧栏的结构要去网页版那份
   bundle 里找；
3. 桌面 app 的 `Code Cache`（本机 `~/Library/Application Support/kimi-desktop/Code
   Cache/js/*`）里缓存着网页版 chat 的 bundle，里面的类名直接给出**对话侧栏**的
   结构：`sidebar next-sidebar` / `sidebar-mode-switch` / `sidebar-fixed-new-chat` /
   `next-sidebar__top-list` / `next-sidebar__more-list` / `next-sidebar-pinned-list` /
   `next-sidebar-project-list`（`__create-label`）/ `NextSidebarSection` /
   `next-sidebar-history-list`（`__task-status` / `__action` / `__chat-unread`）/
   `next-sidebar__footer` + `sidebar-user-menu-trigger`。

两边的条目（从各自 bundle 的 i18n 键读出，不是编的）：

| | 工作侧栏（Kimi Work） | 对话侧栏（网页版 chat） |
| --- | --- | --- |
| header | 折叠钮 + logo + **flavor 徽章**（`sidebar-flavor-badge`） | 只有 logo（无徽章） |
| 分段 | `[Work｜Chat]`（⌘1 / ⌘2） | `mode-switcher`（同一组） |
| 主按钮 | **New task**（图标 `addConversation` + ⌘K chip），固定区 | **New chat**（`sidebar-fixed-new-chat`） |
| 顶部导航 | 平坦一列：canvas / plugins / cron / remoteControl / apps(Beta) | 主入口 4 条 + **More 折叠**：mykimi / plugin / scheduled-tasks / inspiration，More 里放 kimi-work / kfc / kimi-claw / platform |
| 置顶 | Pinned（`pinned-list`，空则不渲染） | Pinned（同上） |
| 分组 | Projects（`＋`：从零/用已有文件夹）→ Tasks（标题可折叠 + `＋` + 行 + 空态 `tasksEmpty`） | Projects（`__create-label`）→ 会话历史（行内 `pin` / `delete`，溢出 `showAllChats`） |
| footer | quickstart 三步卡 + 账户行 | 账户行（无三步卡） |

FCode 的两侧栏按这份对照落在 `core/side-nav.js`（纯渲染）+ `app.js` 接线 +
`index.html` 两个面板（`#pane-work` / `#pane-chat`）：

- 工作侧栏 = 看板·账本 / 判断记录 / 定时任务 **平坦一列**（`WORK_NAV`），
  置顶 → Projects（`＋` 内联输入）→ 任务（标题可折叠 + `＋` + 行状态点 + 空态
  「任务会出现在这里」），footer 里是**快速开始**三步卡；
- 对话侧栏 = 判断记录 / 定时任务 作主入口，**看板·账本收进「更多」**
  （`next-sidebar__more-list` 同构），置顶 → Projects → 对话（行内 ★ 置顶 / × 移除、
  溢出「显示全部 · 还有 N 条」）；
- 导航条目统一用 `data-open`，两个面板各放一份也不抢 active（`paintNavActive()`）。
- 快捷键对齐：⌘1 工作 / ⌘2 对话 / ⌘B 折叠侧栏（网页版 chat 的 `⌘B` 同款），
  ⌘K chip 按 Kimi 挪到主按钮行上；侧栏不再有搜索框（Kimi 两侧栏都没有），
  搜索走命令面板（⌘K 或 `/`）。

**有意保留的三处差异**（不是没对齐）：

1. 任务行保留第二行 meta（模板 · 轮数 · 成本 µ）——Kimi 只有标题 + 时间，
   但 FCode 的行要能一眼看出花了多少钱；
2. 任务列表仍按「今天 / 昨天 / 更早」分组——强大到 5 条以上时折叠，
   对应 Kimi 的溢出行（`showAllChats`）；
3. 置顶语义两套：任务用服务端 `run.pinned`（`POST /api/runs/{id}/meta`），
   对话目前没有可变 meta，用本机 `fa.pins`（行上 ★ 开关），
   文档里写明「本机置顶」而不是假装是服务端事实。

**验证**（`http://127.0.0.1:8643/?desktop=1`，桌面模式、真数据）：工作侧栏
`#work-nav` 3 条 + 任务段 2 行 + 快速开始 1/3（由真实状态推导）；对话侧栏
主入口 2 条 + 更多折叠开合、徽章隐藏、footer 无三步卡；⌘1/⌘2/⌘B 生效；
段落折叠写 `body[data-hide]`（`tasks` / `projects` / `chats` / `pinned`）；
置顶把运行移入置顶段、取消后回到主列表（两侧同步，`renderNav()` 一次重绘）；
运行行的事件委托从 `#run-nav` 上移到侧栏容器——否则置顶段里复用的同一套行
有菜单却点不开（这是本轮浏览器验证抓到的真 bug）。

## 侧栏第二批：交互结构、动画符号与三条失效修复

第一批（上面「侧栏复刻」「侧栏是两套」两节）覆盖了几何与基础色号。第二批是重新解包
`app.asar`（Kimi 3.2.20，`kimi-agent-DMtZmzV-.css` 153 KB / `kimi-agent-BHrERAnD.js`
958 KB）后补齐的，三条都是第一批漏掉的。

**① 交互结构（此前完全没有）**

| Kimi | 实测值（出处选择器） | FCode 落地 |
| --- | --- | --- |
| `.sidebar-resizer` | 4px 命中区 / `col-resize`；`:after` 1px、高 400px、拖拽中 360px、`opacity .5`、`--sidebar-indicator-y` 跟指针 | `.side-resizer` + 双击回 240 + `fa.sidebarW` 记忆 |
| `.sidebar-scroll-top-mask` | `height:0; flex:0 0 0; opacity:0`，`.is-visible{opacity:1}`，`transition: opacity .16s cubic-bezier(.23,1,.32,1)`；`::after` 24px 顶部渐变 | 同名元素 + 捕获阶段 scroll 监听；**替换**了原先把 `mask-image` 挂在 `.side-list` 上的做法（那只能常亮，没有"滚了才淡入"） |
| `.sidebar-footer::before` | `bottom:100%`、高 24px、`transparent → Bg-GroundPC` | 同 |
| `.sidebar-toggle-tooltip` / `-shortcut` | 浮层 + 快捷键帽，`--keycap-chips-gap: 4px` | `.side-tip` / `.side-tip-key`（`--k-fill-1-invert` 底、`--font-mono`） |

**② 动画符号（keyframes 名字与节奏照抄）**

| Kimi | 值 | 用在哪 |
| --- | --- | --- |
| `.sidebar-reveal-*` | `grid-template-rows .16s cubic-bezier(.2,0,0,1), opacity .12s ease-out`；enter-from/leave-to = `0fr + opacity 0` | 段落折叠。**替换**了原来的 `display:none` 硬切；实测 70ms 时 `rows 7.80px / opacity 0.409`，确认为补间 |
| `kimi-spin` | `rotate(360deg)` | 运行中状态点（`border` 环，`border-top-color` = KMBlue） |
| `quickstart-dismiss` | `scale(.804); opacity:0` | 快速开始卡退场 |
| `conversation-title-marquee` | `translate(calc(-1 * var(--…-distance)))` | 悬停溢出标题。距离在**悬停态**下量：悬停时行尾时间戳让位给 ⋯ 按钮，标签 142→185 变宽，所以未悬停时算出的 154px 不是播放时的值（悬停态 296-185=111px，正好露出尾巴） |
| `kimi-shimmer-text-sweep` | `background-position 0% → -200%` | 已记，未接线（等有 shimmer 场景再用） |

**③ 三条此前写了但没生效的规则**（kimi.css 里写了，被更早的文件以更高优先级压住；
用同序或更高序的选择器重述，靠"最后加载"取胜）

1. `.app.side-collapsed .sidebar` 的纵向内边距 10px → **9px**（Kimi 恒为 `9px 8px 8px`）
2. `html.desktop-app .side-list` 的 `padding:0` → **`0 2px`**（Kimi 的 `.sidebar-scroll` 有
   `padding-right:2px`）
3. 导航图标染色：`design.css` 在 `<svg>` 元素上写 `color: --text-3`，压过给 span 写的规则
   （`stroke=currentColor`，谁定 svg 的 color 谁赢）。Kimi 的 `.nav-item` 是
   `Labels-Primary(.9)`，且 `.active` **只换背景不改颜色** → 现在工作侧图标是 `.9`

**两条没有改**（因为 Kimi 里没有可测值，不编）：`.side-user .side-gear` 的 24 vs 20px
（`.account` 规则里只有 `min-width:0`）、`.side-nav` 的 `gap` 1 vs 2px。它们不是"Kimi
规则失效"，是 FCode 内部 desktop/非 desktop 不一致，没有实测依据可判。

**留存的差异**（有意，不是没对齐）

- Kimi Work 的 5 槽位顺序是 `canvas / plugins / cron / remoteControl / apps`。FCode 把
  3 个**真页面**排在前、4 个**预留槽位**排在后：第一眼能点开的应该是真页面。
- 预留槽位不是 Kimi 的 `:disabled`（点不动 + `opacity .5`），而是**可点的、标注过的预留页**：
  页面上直说"对应 Kimi 的哪一项 / 落地需要什么 / 现在能做什么"。行上带一个安静的「预留」小标。
  理由：把未实现的能力说成已实现，比留一个标好的空位更糟。

**验收**（`http://127.0.0.1:8650/?desktop=1`，桌面模式、真数据，浏览器实测计算样式）

| 项 | Kimi | 实测 | |
| --- | --- | --- | --- |
| 侧栏宽 / 内边距 / 底 | 240 / `9px 8px 8px` / `#fbfaf9` | 240 / `9px 8px 8px` / `rgb(251,250,249)` | ✅ |
| header | 高 36、`margin 0 -8px -3px` | 36、`0 -8px -3px` | ✅ |
| nav-item | 36 / p8 / gap6 / r12 / Primary .9 | 36 / 8px / 6px / 12px / `rgba(0,0,0,.9)` | ✅ |
| nav 图标 | 18px、Primary | 18px、`rgba(0,0,0,.9)` | ✅（修复后） |
| 悬停 / 选中填充 | F1 `.03` / F2 `.05` | `rgba(0,0,0,.03)` / `.05` | ✅ |
| flavor 徽章 | 18 / p0 6 / r5 / KMBlue / 11-600 / 字距 .06em / 大写 | 同（本轮补了 letter-spacing 与大写） | ✅ |
| 折叠补间 | grid `1fr→0fr` + `.16s cb(.2,0,0,1)` | 70ms 采样 `7.80px / opacity .409` | ✅ |
| 拖拽调宽 | 4px / col-resize / 1px KMBlue 指示条 | 拖动 240→300px 并写入 `fa.sidebarW` | ✅ |
| toggle tooltip | 浮层 + 键帽 | `(8,51)` 视口内、文案「折叠侧栏 ⌘B」 | ✅ |
| 运行中 | 转动环 | `kimi-spin 1s`、`border-top` KMBlue | ✅ |

**本轮浏览器验收抓到的真 bug（两个，都已修）**

1. **段标题被一起折进去了**：第一版把 `#work-tasks`（段标题）和 `#run-nav`（行）都放进
   `.side-block-inner`，折叠后标题被 `overflow:hidden` 裁掉 → `[data-section-toggle]`
   点不到 → 该段**再也展不开**。Kimi 的 reveal 只收内容、标题在 reveal 之外。改成只包行，
   用 `.side-block-inner:has(#run-nav)` 认领各自的列表。复验：折叠态下标题仍可点。
2. **tooltip 落到视口外**：`.side-tip` 是 absolute，但 `.side-fold` 没有 `position: relative`，
   于是它按 `.sidebar`（absolute）定位，`top:100%` 跑到了 y=728。加了
   `.side-fold { position: relative }` 后落在 (8,51)。

（另有一条是既有缺陷而非本轮引入，顺手修掉：`quickState().providerReady` 写成
`!!quickState.providerReady`——一个从未被赋值的静态属性，导致快速开始第一步**永远**无法完成。
现在由 `modelCatalog` 里是否存在可用的非 mock provider 推导。）
