# FCode 桌面端设计：Kimi 比例参照

这份文档记录桌面端的参照测量、映射关系和实现边界。目标不是复制 Kimi 的皮肤，
而是用它的**比例与视图节奏**，让 FCode 在桌面窗口里像一件完整的应用。

## 1. 参照测量

2026-10-25 在 kimi.com 实测，窗口 1600×1000（Chrome，DPR 1），
首页与对话页各一次。几何值与主题无关；FCode 的色号与动效已改为 Kimi 实测皮肤
（见 `kimi-skin.md`），审计底稿与证据区仍保留 Paper & Proof。

| 部件 | Kimi 实测 | 备注 |
| --- | --- | --- |
| 侧栏 | 240 × 1000 | 全高，内边距 8 |
| New chat | 224 × 41，r12 | 侧栏内唯一实心控件 |
| 导航行 | 高 36，行距 37 | 标签 14/20，图标列 20，标签 x=40 |
| 分组标题 | 14/20，x=16 | Chats / Projects |
| 历史条目 | 高 36，r12 | 标题 14/20，截断 |
| 用户区 | 180 × 44，r12 | 距左/底 8，名称 14/20 |
| 首页内容列 | 1320 | 左 17 / 右 23 的留白 |
| 输入框 | 768 × 114，r24 | 边框 1px，阴影 `0 5px 16px -4px rgba(0,0,0,.07)` |
| 编辑区 | 734 × 48，16/24 | 上内边距 12，左 16 |
| 操作行 | 高 36 | 模型/插件按钮同高，胶囊 r20 |
| 首页快捷 chips | 高 36，r999 | 输入框下方居中 |
| 对话顶栏 | 高 52，上边距 7 | 比画布深一档 |
| 消息列 | 780 | 正文 16/26，气泡 r12 |
| 对话输入框 | 768 × 114，距底 35 | 与首页同一输入框 |

## 2. 映射到 FCode 桌面端

实现集中在 `flowcredit/viewer/desktop.css`，全部规则挂在 `html.desktop-app` 下，
浏览器视图不受影响。

| FCode 位置 | 桌面值 | 对应 Kimi |
| --- | --- | --- |
| `--sidebar-w` | 240 | 侧栏 240 |
| `--reading-w` | 768 | 输入框/内容列 768 |
| `--fs-3` / `--fs-2` | 14 / 13 | UI 正文 14 |
| `.side-new` | 224 × 41，r12 | New chat 41/r12 |
| `.side-nav .nav-item` | 高 36，padding 0 8，标签 x=40 | 导航行 36/37 |
| `.side-section` | x=16 | Chats 分组 |
| `.side-user` | 224 × 44，r12，8px 内边距 | 用户区 44/r12 |
| `.topbar` | 52；首页隐藏 | 对话顶栏 52 |
| `.composer` | 768，r24 | 输入框 768 |
| `.composer-ta` | 16/24，60px 编辑区 | 编辑区 48 + 12 上边距 |
| `.composer-bar` | 9 + 36 + 9 | 操作行 36 |
| `.c-btn` / `.c-icon` / `.send` | 36 高，胶囊 r20/圆形 | 模型/插件按钮 |
| `.stage-dock` / `.stage-chips` | 768 | 首页输入框与 chips |

首页的顶部工具条在桌面模式下隐藏（`#app.stage-mode .topbar`），与 Kimi 的新对话
视图一致；进入运行、委托确认或设置页时恢复。

## 3. 视图

### 首页 / 新对话

字标 + 居中输入框 + 模板 chips，没有顶部工具栏。与 Kimi 的首页结构相同：
侧栏导航在左，输入框略高于视口中线，chips 在下方。

![desktop home](../viewer/docs/desktop-home.png)

### 运行页

52px 顶栏显示当前委托标题与链验证状态；内容列 768，工具卡是 FCode 自己的
结构化证据视图；输入框吸底，右侧证据浮层宽 320。

![desktop run](../viewer/docs/desktop-run.png)

### 委托确认、设置、报告

- 委托确认：顶栏 + 760px 会话列，草稿卡显示模式/来源/审批/预算。
- 设置：全覆盖页面（侧栏一并收起），沿用 Kimi 的设置页进入/返回方式。
- 报告：全屏浮层，通用委托显示“产物”表（路径/大小/哈希/复核方式）。

## 4. 实现与启动

- `flowcredit/viewer/desktop.css`：桌面比例层，约 80 行，全部 scoped。
- `flowcredit/viewer/index.html`：`?desktop=1` 时在 `<html>` 上加 `desktop-app`，
  确保首帧就是桌面比例；加载 `desktop.css`。
- `desktop/app.py`：窗口 URL 追加 `&desktop=1`；窗口 1500×950，最小 1080×680，
  背景 `#FBFAF9`，使用原生标题栏（macOS 红绿灯/Windows 标题栏保持系统行为）。
- `flowcredit/viewer/server.py`：token 引导重定向只剥掉 `token`，保留
  `desktop=1` 等其他查询参数。否则桌面标识会在第一次 302 后丢失。

```sh
python3 flowcredit/viewer/server.py --repo . --port 8643
open 'http://127.0.0.1:8643/?desktop=1'
bash desktop/build.sh          # 重新打包后 .app 自动带上 desktop=1
```

## 5. 边界与下一步

- **原生标题栏**：不做 frameless 自定义红绿灯。pywebview 的 frameless 窗口会失去
  系统窗口控制，WKWebView 对 `-webkit-app-region: drag` 的支持也不如 Chromium 稳。
  保持原生标题栏，窗口内只做内容区。
- **窗口几何**：当前固定 1500×950 / 最小 1080×680；记住上次位置与尺寸是下一步。
- **消息字号**：Kimi 对话正文 16/26；FCode 运行页是密集的证据卡与哈希，正文保持
  14，避免工具卡膨胀。若未来增加“阅读模式”，再单独提升自由文本字号。
- **主题**：Kimi 参考页是深色；FCode 桌面端继续浅色。比例与主题解耦。
- 桌面端不是第二套实现：同一个 server、同一个 viewer，只多一层比例与窗口参数。
