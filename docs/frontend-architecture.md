# 前端收敛：一个主区，一套状态

Phase 1–3 已完成，之后又加了一层对话面。目标始终不是换视觉，而是把历史层收成
一个可维护的结构：**主区只有一个渲染入口，运行状态只有一个事实源，动态列表
只挂一层事件；对话有独立的线程状态，但不复制运行状态。**

## 当前结构

```text
Sidebar（唯一导航）
Main（data-view = chat | run | loading | error | intake | empty | ledger | reviews | scheduled）
Settings（workspace 内的主区页面，打开时让位）
Rail（只随 run 出现）
Composer（chat / run / intake）
Overlay（report / palette）
```

- 新对话的默认入口是 `newChat()`：`#new-run`、`⌘N`、侧栏「新对话」、空状态都走这里。
- 委托入口是模板卡、「转为委托」和 `startIntake("")`；对话回答不会自动执行工具。
- 设置、账本、判断记录、定时任务继续是主区模式，不是弹层；报告和命令面板保留为浮层。
- 审批、判断记录留在行内。

## Phase 1：删掉重复入口

- 删除 `#wizard-layer` 整块 HTML；
- 删除 `wizard` 状态与 `openWizard/closeWizard/renderWizard/wizardNext/submitWizard/bindWizard`；
- 删除 palette 的「高级建单向导」及 `.wizard-*` / `.wz-*` CSS；
- 净减约 **220 行**（JS 154、HTML 17、CSS 约 60）。

## Phase 2：主区写入与样式分层

- 全部主区写入收口到 `renderMain(html, { mode, scroll })`；16 个直接写入点归零。
- 主区带 `data-view`，同时记录 `store.mode`。
- 删除 app.css 的 3 个废弃 `:root`（约 33 条声明），当时把 token 收到 `design.css`；
  后续 Console Light 改版又把令牌移回 `app.css` 顶部，`design.css` 只留组件视觉。
- 补齐 `--accent` / `--accent-soft` / `--accent-fill` 三个别名。
- 删除 11 个无引用类和 21 处死 selector。

## Phase 3：结构收口

### CSS：布局与组件分离

- `app.css` **326 行**：顶部 `:root` design tokens（Console Light 三层表面 / 单强调色 /
  语义色），下面只保留外壳结构：box-sizing、shell、侧栏/主区/右栏几何、滚动容器、
  浮层定位、结构性响应式规则。
- `design.css` **2,139 行**：组件视觉（排版、边框、圆角、图标、动效）与 Paper & Proof
  覆盖层；在 app.css 之后加载，可以覆盖其非令牌规则。
- `chat.css` **326 行**：对话消息、气泡、代码块、附件、设置卡片。
- `desktop.css` **48 行**：只在 `html.desktop-app` 下套用 Kimi 测量比例。


### State：单一 UI store

```js
const store = {
  key: null,        // 当前视图键
  sessionId: null,  // 当前激活会话 id
  trace: null,      // 非会话视图的 trace
  check: null,      // 当前链校验结果
  mode: "run",      // 最近一次主区模式
};

function activeSession() { ... }  // sessions.get(store.sessionId)
function shownTrace() { ... }     // 激活会话 trace，其次 store.trace
```

- 删除 `currentKey` / `currentTrace` / `currentCheck` / 模块级 `live`，旧状态引用为 **0**。
- `sessions` 仍是会话注册表，不是第二份 UI 状态。
- `store.trace` 与 `s.trace` 指向同一对象，不复制 trace。
- 删除会话时同时清理 `store.sessionId`；设置页关闭时恢复 `store.mode`。

### Interaction：容器委托

- `addEventListener` 从 **73** 降到 **38**。
- 动态列表的监听器全部收口到稳定容器：
  `#nav`、`#run-nav`、`#tabs`、`#rail-tabs`、`#rail-body`、`#palette`、`#tpl-menu`、`#approval-menu`、`#settings-page`、`#main`。
- 保留的监听器：4 个 SSE 事件、`pywebviewready`、`DOMContentLoaded`、输入框/搜索/rename 的输入事件、slash menu、拖放、全局键盘。
- 行内交互语义不变：双击重命名、两步删除确认、菜单互斥、Enter/Space 打开运行、Escape 关闭浮层。

### View：显式视图入口

```js
const MODE_ENTRY = {
  run: payload => render(payload.trace, payload.title),
  intake: () => renderIntake(),
  ledger: () => openLedger(),
  settings: () => openSettingsPage(),
};
function renderMode(mode, payload = {}) { ... }
```

- 导航入口只走 `renderMode`；`render` / `renderIntake` / `openLedger` / `openSettingsPage` 是具体实现，不反向调用，避免递归。
- 设置页是 workspace 内的页面，不用 `renderMain`，但会同步 `store.mode = "settings"`。

### Modules：零构建 ES module 边界

- `core/format.js`（45 行）导出 canonical JSON、SHA-256、链验证与格式化；
  `core/chat-format.js`（92 行）导出 Markdown 渲染与 SSE 分帧。
- `app.js` 是 `type="module"`，从这两个文件导入；无打包器、无第三方依赖。
- `report.js` 保持独立 classic script：报告必须是可离线打开的独立产物，刻意不依赖控制台。
- `icons-doodle.js` 保持 classic script，先于 module 执行，提供 `window.DOODLE_ICONS`。
- 桌面打包 `desktop/fcode.spec` 不再维护文件清单：复制根级 html/css/js 与
  `core/`、`brand/`、`icons/`，新增静态资源不会漏进 bundle。

### Chat：线程状态与流式渲染

对话不是第二次实现，而是同一套外壳里的一个 `data-view`：

- `chat` 对象只保存对话自己的状态（当前线程、消息、流式句柄、附件、编辑目标、
  删除确认）；运行状态仍在 `store` / `sessions`，两者不互相复制。
- 每次发送、编辑、重新生成都追加事件（`chat_user` / `chat_assistant` /
  `chat_edit` / `chat_regenerate` / `chat_cancelled` / `chat_error`），活跃分支
  由 supersession 推导；删除线程写墓碑，不抹事件。
- 流式渲染只重绘当前 assistant 节点，避免整段 Markdown 每次 delta 都重建；
  滚动容器是 `.main`（不是 `#main` 的内部 div）。
- 对话模型选择存 `fa.chatModel`，与委托运行的模型选择分离；设置页
  （设置 > 环境 > 默认）只在用户未手动选择时提供默认值。
- Markdown / SSE 分帧是纯函数，放在 `core/chat-format.js`，由
  `node --test viewer/tests/*.test.js` 覆盖；不需要浏览器或测试框架。

### 工作台入口：任务在前，夹具在后

空状态的 chips 原来把 `/api/templates` 的每一项都摆在"按模板开始委托"里——
包括七条可靠性夹具（越权工具/预算耗尽/崩溃恢复/未知结果/目标无法完成…）。
它们是这台机器的故障注入场景，用来证明门控与恢复是真的，属于**演示素材**，
不是产品前门。现在只有真实任务模板直接露出（通用委托、完整研究），夹具收进
折叠的「演示场景」，仍然可达。空状态文案也从治理语（"受门控执行、独立重算、
入签名链"）改回任务语（"自己规划、动手执行、完成后给你产物"），治理事实降为
输入框下方一行脚注——访客先看懂能做什么，再看它凭什么可信。

### 历史的运行：一条读取路径

运行索引、轨迹和 task 数据库都落在磁盘上，所以重启之后运行记录会回来：
`RunManager._load_history()` 只重建内存里的那条记录（无线程、无观察者、不再
ingest、不假造 snapshot），排序按 `created` 倒序。两个诚实性细节值得记住：

- meta.json 里记着 `model` / `effort`（列表接口不需要，但恢复出来的运行要在
  原 provider 上继续，静默退回 mock 就是撒谎）；
- 进程死在半途的运行，meta 里写着 `running`。恢复时按 `stopped` + 一句说明
  处理（`FINISHED_STATUS` 之外的都算中断）：说它"运行中"是在承诺没人做的
  工作，说它"失败"又违反内核的未知结果语义——轨迹停在进程消失那一刻，记录
  就这么说。
- 一个损坏的 meta.json 只跳过自己，不拖垮启动：半写的文件不该藏起其余历史。

**轨迹一律走 `GET /api/runs/{id}/trace`，不走 `runs/<id>/trace.json` 这个静态
路径**。静态路径只在运行目录恰好位于 viewer 检出内（本地开发默认）时才成立；
桌面版把运行放在 Application Support，重启后点开任何已结束的运行都会 404
（历史 bug）。服务端用 `console_common.run_trace_path()` 决定"哪个文件算这次
运行的轨迹"，并拒绝任何不是运行目录里单一目录名的 id；前端只有一个
`runTraceUrl()`，账本补成本、打开运行、记录判断后重读轨迹三处共用。


## 当前数字

| 项目 | 数值 |
| --- | --- |
| `app.js` | 7,005 行 |
| `design.css` | 2,139 行 |
| `app.css` | 326 行 |
| `chat.css` | 326 行 |
| `kimi.css` | 467 行 |
| `desktop.css` | 48 行 |
| `core/format.js` | 45 行 |
| `core/chat-format.js` | 92 行 |
| `index.html` | 366 行 |
| `main.innerHTML` 直接写入 | 1 处（`renderMain` 内） |
| 旧状态引用 | 0 |
| `addEventListener` | 56 |
| 后端测试 | 395 通过 |
| 前端纯函数测试 | 12 通过（`node --test`） |

## 仍待收敛

1. `app.js` 仍有 7,000 行；下一步按视图拆 module：chat、run、intake、settings、ledger，
   各自只依赖 `core/format.js`、`core/chat-format.js` 与共享 store。
2. `renderSettings()` 仍返回大段 HTML；拆视图时一并迁出。
3. 对话 UI 仍与运行 UI 同处 `app.js`；目前只有 Markdown/SSE 纯函数被抽出并有测试。
4. `design.css` 仍有 5 处 `!important`，集中在减少动效与少量覆盖；拆视图后逐条确认能否删除。
5. 前端测试只覆盖纯函数，没有浏览器 E2E；继续坚持零依赖，不为测试引入构建链。
6. 对话附件只收纯文本；图片/PDF/Office 不在 v1。


## 约束

- 零构建、零第三方运行时依赖不变。
- 新样式只进 `design.css`；`app.css` 只保留布局。
- 收敛期间不改视觉效果；先保持像素稳定，再谈美化。
- 所有入口、键盘、空状态、窄屏都要重新验收。
- 事件链仍是唯一事实源；UI 状态必须能从 trace/events 重建。
