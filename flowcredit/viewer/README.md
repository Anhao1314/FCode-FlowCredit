# Fcode · flowcredit console — 对话优先的 Agent 控制台

一个零依赖、零构建、可离线的 Agent 控制台。三个面（Kimi 桌面端同款
分段开关）：**对话**——回答流式吐出，可追问、重新生成、编辑上一条、挂文本
附件、随时停止；**工作**——受治理的委托运行（审批 / 插话 / 调预算 / 续跑、
看板账本），并只读回放任意已签名 trace；**审计台**——只读展示
`work/audit/<audit_id>/` 的发现账本、PoC Evidence 与 REA 制品证据。

前端是原生 ES modules，没有框架、打包器或字体请求；服务端只用 Python 标准库。

```
flowcredit/viewer/
  index.html          ← Work / Chat 外壳（零依赖、零构建）
  audit.html          ← 审计台外壳（发现账本只读视图）
  audit.css           ← 审计台视觉
  audit/audit-view.js ← 审计台投影与渲染
  app.css             ← design tokens（全站唯一 :root）+ 基础基元 + 外壳布局（第一加载）
  desktop.css         ← ?desktop=1 桌面模式壳几何（第二加载）
  design.css          ← 组件视觉 / Paper & Proof 审计组件（第三加载）
  chat.css            ← 对话消息 / 气泡 / 代码块 / 附件 / 设置卡片（第四加载）
  thread.css          ← 合并壳层 v7：sidebar/composer/menus → moonshot stage
                        → shell/responsive → task thread（最后加载）
  app.js              ← 页面逻辑（对话 / 委托 / 回放 / 运行管理）
  app/
    task-thread.js    运行 trace → 统一任务线程（纯投影 + 单条渲染，Node 可测）
  core/               ← 前端纯函数层（全部 Node 可测，无 DOM 依赖）
    format.js         canonical JSON、SHA-256、链验证、格式化
    chat-format.js    Markdown 渲染与 SSE 分帧
    chat-view.js      单条消息 DOM 渲染
    run-card.js       对话内执行提案（紧凑下一步）与运行镜像的投影
    side-nav.js       两侧栏（工作 / 对话）的条目、置顶、Projects、任务与对话列表
    ai-avatar.js      对话 AI 的头像（Q 版印豆 · 圆框 · 黑白，状态由事实推导）
  report.js           ← 自包含审计报告生成器
  icons-doodle.js     ← 图标与涂鸦
  server.py           ← HTTP 表面：静态文件 + JSON/SSE API + 审计只读 API + 入口
  audit_jobs.py       ← 审计 CLI 作业（内存状态，证据落盘在 work/audit/）
  console_common.py   ← 路径、静态拒绝、模型目录与预算换算
  console_runtime.py  ← runtime 导入、单次运行、观察者与崩溃检查
  console_manager.py  ← 运行历史、创建、控制、续跑与持久化
  tests/              ← 前端纯函数测试（node --test，183 项）
  runs/               ← 运行产物（task.sqlite / trace.json / meta.json，不入库）
  chat.sqlite         ← 对话事件库（不入库）
  docs/console-live.png  ← 看板 · 账本（换肤后重拍；另有 desktop-home / -chat / -run）
```

样式栈按 tokens → layout → components → views 分层，加载顺序固定为
`app.css → desktop.css → design.css → chat.css → thread.css`：令牌只在
`app.css :root` 定义一次，其余文件只消费不改写。`kimi.css`/`moonshot.css`/
`workspace.css` 已合并进 `thread.css`（v7 收敛），后续步骤把其中的规则按
职责迁往前四层后删除对应段落。
设计语言分两面：外壳与对话面走黑白灰克制皮肤，审计底稿与证据面走
Paper & Proof（`../docs/design-system.md`）。

## 快速开始

在仓库根目录：

```sh
python3 flowcredit/viewer/server.py --repo . --port 8642
# 浏览器打开 http://127.0.0.1:8642/
# 桌面比例视图（Kimi 测量值）
open 'http://127.0.0.1:8642/?desktop=1'
```

- `--pace 0.8`：委托运行每轮模拟思考时长（秒），`0` 全速；
- `--approval-timeout 300`：审批等待秒数，超时自动拒绝；
- `--repo` 指向仓库根（`flowcredit/src` 所在位置），也可用 `FLOWAGENT_REPO`；
- `--local-endpoint`：Ollama 兼容地址，启用本地 Qwen 模型项；
- `--host 0.0.0.0` 必须同时给 `--token`，否则服务拒绝启动；桌面壳会自动生成 token；
- 找不到 runtime 时服务仍可用：页面退化为**静态回放模式**，`/api/health` 说明原因。

## 对话

- **流式回答**：每个对话轮次是一次模型调用（不调用工具），SSE 增量渲染；
- **追问 / 重新生成 / 编辑上一条**：编辑或重生成会 supersede 旧分支，活跃分支由事件推导；
- **附件**：纯文本，最多 3 个，每个 ≤64 KiB；图片 / PDF / Office 不在 v1；
- **停止**：`⌘.` 或发送键旁的停止按钮；已产生的内容与收据保留；
- **归档与分页**：侧栏列表分页，删除是两步确认，服务端写归档墓碑而不是抹掉事件；
- **模型选择**：菜单可选 offline-mock / Kimi / DeepSeek / 本地端点；默认 mock，
  无需密钥即可验证整条流式链路。

每次对话都写入同一条 SHA-256 事件链并取得 HMAC 收据；预算在调用前扣减，
默认每线程 60 次调用 / 300,000 µUSD / 200,000 tokens（`FCODE_CHAT_MAX_*` 可调）。
界面不把回答标成“已验证”——它的可信性来自调用收据与事件链，不来自 verifier。

### 模型配置（设置页）

`⌘,` 打开设置，或从用户菜单进入。配置优先级：**设置页 > 环境变量 > 默认值**。

- 设置页保存的 `chat-config.json` 位于用户配置目录（macOS 为
  `~/Library/Application Support/FCode`），权限 `0600`，可用 `FCODE_CONFIG_DIR` 覆盖；
- API key 只存本地，API 响应只返回 `api_key_set` 与末 4 位掩码，从不回传明文；
- 远程 provider 的 `base_url` 不可在设置页改写，避免把密钥发往任意主机；
- Kimi 使用 `max_completion_tokens`；组织 RPM 限制通过进程内 pacing 表现为 `wait` 事件。

## 审计台

`/audit.html` 是只读的发现账本，不是另一个执行入口：

- 左侧列出 `work/audit/<audit_id>/` 的 bundle；
- 中间渲染 findings、PoC Evidence、负对照与 `uncovered_surfaces`；
- REA 制品审计的 `rea-evidence/`、`facts.json` 与 `control-facts.json`
  作为证据入口展示：目标 artifact 哈希、REA 版本、Go 构建信息、entitlements
  与声明差异、library-validation 静态加载链判定、sidecar 摘要、未覆盖面都在页面上；
  API 只读取 bundle 引用过的文件，不执行目标；
- 写操作仍只发生在 CLI（`python audit.py audit ...`）或 `/audit <target>`
  斜杠命令，前端不提供“提升 finding”的按钮。

## 委托运行

- **8 个模板**：`通用委托`（抓取公开来源 → 写 `research.md` / 写脚本 → 受限运行 → 修复 → 复跑），
  以及 7 个财务参考负载上的故障注入（越权、预算耗尽、预算追加、崩溃恢复、未知结果、
  目标无法完成、完整研究）。
- **审批门**：默认只在关键工具前暂停；可切「逐步审批」或「自动」。允许 / 拒绝 / 超时都入链。
- **运行中插话**：消息排队到下一轮边界，以 `operator_message` 写入同一条链；
  排队 / 入链 / 未送达三种状态都在气泡上标明。
- **统一任务线程**：任务 → 状态／计划 → 工具调用与审批 → 最终回复。实时运行、
  历史回放、普通项目与完整研究共用 `app/task-thread.js` 的同一套投影与 DOM；
  工具默认一行摘要，展开才看参数、命令、真实 diff 与签名收据；预算与链验证
  退到"运行详情／证据"入口。
- **计划与预算**：线程内紧凑清单，可跳过 / 提前某个 todo；预算只能调高，
  下一轮生效，明细收在可展开的用量区。
- **续跑**：仅接受 `BUDGET_EXCEEDED` / `ROUND_LIMIT`，从事件日志续写，已付的读取不重发。
- **随时停止**：轮次边界停止，终态 `STOPPED_BY_OPERATOR`，链仍完整可验。
- **工具卡按运行时词汇渲染**：通用工具是 `ls_workspace` / `read_file` / `write_file` /
  `edit_file` / `run_command` / `fetch_url`；财务参考负载另有结构化细节卡
  （文档正文、口径换算、重述对比、毛利率三联指标）。未知工具按通用卡渲染。
- **审计报告**：运行结束后签发自包含 HTML 底稿（结论、账单、证据、人工决定、链附录）。
- **运行记录**：按今天 / 昨天 / 更早分组，搜索、置顶、重命名；失败运行可重试或复制诊断。

## 键盘

| 键 | 行为 |
| --- | --- |
| `⌘K` / `Ctrl+K` | 命令面板 |
| `⌘N` / `Ctrl+N` | 新对话 |
| `⌘,` / `Ctrl+,` | 设置 |
| `⌘.` / `Ctrl+.` | 停止当前回答或运行 |
| `⌘↵` / `⌘⌫` | 允许 / 拒绝当前审批 |
| `1` / `2` / `3` | 审批直选：允许 / 允许并记住 / 拒绝 |
| `Enter` / `⇧Enter` | 发送 / 换行 |
| `R` | 重放当前场景（静态回放） |
| `↑` / `↓` | 运行列表导航；输入框首行为历史 |
| `Esc` | 关闭面板 / 菜单 / 设置 / 报告 |

## API（`server.py`，标准库实现）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | live 是否可用、runtime 路径、pace |
| GET | `/api/templates` | 8 个场景模板（标题 / 目标 / 故事 / 预算 / 模块） |
| GET | `/api/modules` | 已注册领域模块 |
| GET | `/api/models` | 模型目录与可用性 |
| GET | `/api/tools` | 当前工具词汇 |
| GET | `/api/runs` | 运行历史（含 `tracePath` / `name` / `pinned` / `approval`） |
| POST | `/api/runs` | `{"objective": "…", "template": "full", "approval": "consequential"}` |
| GET | `/api/runs/{id}/stream` | SSE：`hello / snapshot / done / failed` |
| POST | `/api/runs/{id}/stop` | 在下一个轮次边界停止 |
| POST | `/api/runs/{id}/decision` | 放行或拒绝审批 |
| POST | `/api/runs/{id}/messages` | 运行中插话，排队到下一轮边界 |
| POST | `/api/runs/{id}/steer` | 排队一条引导指令 |
| POST | `/api/runs/{id}/queue` | 查看待处理的人工输入 |
| POST | `/api/runs/{id}/plan` | `{"action": "skip"\|"promote", "todoId": "t4"}` |
| POST | `/api/runs/{id}/budget` | 只能调高，下一轮生效 |
| POST | `/api/runs/{id}/resume` | 追加预算并续跑 |
| POST | `/api/runs/{id}/meta` | 重命名 / 置顶 |
| GET | `/api/chat/models` | 对话可用模型（含设置覆盖与可用原因） |
| GET | `/api/chat/threads?limit=&offset=` | 活跃对话，`total` / `limit` / `offset` |
| POST | `/api/chat/send` | `{"threadId"?, "text", "attachments", "model"}` → SSE |
| POST | `/api/chat/threads/{id}/edit` | 编辑上一条并重答 → SSE |
| POST | `/api/chat/threads/{id}/regenerate` | 重新生成最后一条 → SSE |
| DELETE | `/api/chat/threads/{id}` | 归档（墓碑），事件链保留 |
| GET/POST | `/api/settings/chat-config` | 读取（脱敏）/ 保存模型配置 |
| GET | `/api/audit/bundles` | 审计 bundle 列表（只读） |
| GET | `/api/audit/bundle/{audit_id}` | 单个审计 bundle |
| GET | `/api/audit/evidence/{audit_id}/{poc_id}` | 单条 PoC Evidence |
| GET | `/api/audit/rea/{audit_id}/{filename}` | bundle 引用过的 REA Evidence/facts（allow-list） |
| GET | `/api/audit/jobs/{job_id}` | CLI 审计作业状态（内存态） |

实现要点：运行在服务进程内的线程中驱动真实 runtime；观察者线程每 ~120ms 从 SQLite
WAL 只读连接取已提交事件，重建快照推给浏览器。静态文件一律 `Cache-Control: no-cache`，
改完刷新即生效。

## 安全边界

- 静态服务拒绝 `*.sqlite` / `*.sqlite-wal` / `*.sqlite-shm`、`chat-config.json*`
  与 `*.tmp`，命中直接 404；运行 trace 本身仍可读，供回放使用；
- 所有写操作要求 `application/json` 且同源（`Origin` / `Sec-Fetch-Site`），跨站表单无效；
- 请求体上限 1 MiB，超限在读取前返回 413 并关闭连接；
- 非回环地址必须带 `--token`；token 只用于建立 cookie，随后从 URL 中剥离；
- 对话配置不在静态根内，API 从不返回 API key 明文；远程 provider 的 `base_url` 不可改。

## 设计

- 浅色三层表面（画布 `#FBFAF9` / 次级面 `#F5F5F5` / 主表面 `#FFFFFF`），
  分隔线 `rgba(0,0,0,.13)`，8px 圆角（composer 舞台 24px）；
- 强调色是 Kimi 蓝 `#1783FF`（焦点 / 链接）；发送键保持黑色实心；状态色
  `#16C456` / `#FF9500` / `#FF3849` 只用于圆点与图标；
- 动效沿用 Kimi：背景 / 颜色 `.15s`、图标底色 `.3s ease-in-out`、位移
  `cubic-bezier(.23,1,.32,1)`；加载是 5 点螺旋透明度动画，等待额度用 shimmer。
- UI 正文 13px，聊天 Markdown 16/26；哈希、时间戳与预算数字等宽 + tabular-nums；
- `?desktop=1` 时套用 Kimi 测量比例：侧栏 240、阅读宽度 768、输入区 16/24、
  操作行 36、聊天头部 52；测量见 `docs/desktop-design.md`，色号与动效见
  `docs/kimi-skin.md`；
- 页面不向外部网络发送数据；模型调用只到达你本机 `server.py` 拉起的 runtime。

## 测试

```sh
# 后端 + 场景
cd flowcredit && python3 -m unittest discover -s tests -v

# 前端纯函数（Node 内置 test runner，无测试框架依赖）
node --test flowcredit/viewer/tests/*.test.js
```

## 数据

运行产物在 `viewer/runs/{id}/`（`task.sqlite` + `trace.json` + `meta.json`），对话在
`viewer/chat.sqlite`，两者都不入库。审计产物在 `work/audit/<audit_id>/`
（`audit-bundle.json` + PoC Evidence + `rea-evidence/`），也不入库。演示数据由 runtime 生成：

```sh
cd flowcredit && python demo.py all
# 起本地静态服务并打开 viewer（ES module 不能从 file:// 加载）
python demo.py --serve
# 终端会打印地址；在地址后追加 ?trace=../work/demo/full.trace.json
```
