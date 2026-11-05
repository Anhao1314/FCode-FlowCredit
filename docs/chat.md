# Chat：可流式回答的对话面

FCode 现在有两种模型交互，边界是刻意画清的：

| | 对话（Chat） | 委托运行（Delegation） |
| --- | --- | --- |
| 模型输出 | 可读文本（流式） | 一个受门控的工具动作/计划 |
| 是否执行工具 | **不执行** | 执行，逐次过门控、预算、审批 |
| 结果 | 回答、追问、重生成、编辑 | 文件/报告 + 独立重算 |
| 审计 | 每次调用扣预算、进事件链、签收据 | 完整 trace + 哈希链 + 收据 |
| 入口 | 侧栏「新对话」、首页输入框 | 侧栏「委托运行」、首页模板 chips |

需要执行的对话，用消息上的「转为委托」交接给受治理的运行；对话本身不碰工具注册表，
因此不存在“聊天绕过门控”的路径。

## 能力

- **流式回答**：SSE 逐段渲染，Markdown（标题/列表/引用/代码围栏/行内代码/链接）。
- **追问**：同一线程保留活跃分支，模型看到完整历史（受 `CHAT_HISTORY_MESSAGES` 限制）。
- **重新生成**：追加 `chat_regenerate` 事件，旧回答被 supersede，事件链不删。
- **编辑上一条**：追加 `chat_edit` 事件，编辑点之后的旧分支全部 supersede。
- **文本附件**：最多 3 个、每个 ≤64 KiB、总计 ≤128 KiB；内容折进用户消息，事件链保留原文。
- **停止生成**：中断 fetch，服务端把已生成的部分记成 `chat_cancelled`，UI 显示“已停止生成”。
- **线程持久化**：线程与消息在 `chat.sqlite` 的 `task`/`event` 表里，重启后可从侧栏继续。
- **分页与归档**：侧栏按 `updated_at` 分页加载；删除是 tombstone（`chat_thread_meta.archived`），
  事件链、收据和直接按 id 读取都不受影响。

## 治理

对话没有工具，所以不走 `Permissions.gate` / verifier；但**模型调用本身仍然受治理**：

- 发送前用 `Budget.charge_call` 预留 tokens/cost；预算耗尽返回 `BUDGET_EXCEEDED`，不重试。
- 每次回答写一个 `chat_assistant` 事件，进入与运行相同的 SHA-256 事件链。
- 每次成功回答签一条 HMAC 收据；`external_billing` 仍是 `not-verified`。
- 事件链只追加；编辑/重生成通过 supersede 事件表达，不改写历史。

## 服务端

- `flowagent/chat.py`：`ChatService`——线程、预算、事件投影与取消。
- `flowagent/chat_config.py`：设置 > 环境 > 默认的 provider 解析、价格校验、本地配置文件。
- `flowagent/chat_provider.py`：mock 与 OpenAI 兼容流式客户端（SSE 分帧、错误分类）。
- `POST /api/chat/send`：`{threadId?, text, attachments?, model}` → SSE。
- `POST /api/chat/threads/{id}/edit`：`{messageId, text, attachments?, model}` → SSE。
- `POST /api/chat/threads/{id}/regenerate`：`{messageId, model?}` → SSE。
- `GET /api/chat/threads?limit=50&offset=0`：线程分页，返回 `threads` / `total`。
- `GET /api/chat/threads/{id}`：完整线程（直接审计仍可用）。
- `DELETE /api/chat/threads/{id}`：**归档**而不是删除事件。侧栏隐藏，事件链与收据保留；
  需要彻底清理时由操作者直接处理本地数据库。

流式事件：`start` / `wait` / `reasoning` / `delta` / `done` / `error`。客户端断开时
`_stream_chat` 关闭生成器，服务端在 `finally` 里落 `chat_cancelled`。
本地服务默认只绑定回环地址；监听非回环地址时必须同时给出 `--token`，否则拒绝启动。

Provider：

| key | 配置 | 价格 |
| --- | --- | --- |
| `mock` | 无 | 0 |
| `kimi` | `KIMI_API_KEY`、`KIMI_MODEL`、`KIMI_BASE_URL` | `KIMI_INPUT_PRICE_MICROUSD_PER_MTOK` / `KIMI_OUTPUT_PRICE_MICROUSD_PER_MTOK`（必填） |
| `deepseek` | `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL`、`DEEPSEEK_BASE_URL` | `DEEPSEEK_INPUT_PRICE_MICROUSD_PER_MTOK` / `DEEPSEEK_OUTPUT_PRICE_MICROUSD_PER_MTOK`（必填） |
| `local` | `FCODE_LOCAL_ENDPOINT`、`FCODE_LOCAL_MODEL` | 0 |

OpenAI 兼容 SSE；Kimi 使用 `max_completion_tokens`，不发送 `temperature`（K2/K3 只接受 1）；
`KIMI_MIN_CALL_INTERVAL_SECONDS` 生效，等待时会向客户端发 `wait` 事件。

## 模型配置（设置页）

设置 → 模型与工具 现在直接配置对话模型：

- **默认对话模型**：mock / Kimi / DeepSeek / 本地，只对未手动选择过的会话生效；
- **Kimi / DeepSeek 卡片**：API Key、模型名、输入/输出价（µUSD/MTok）、最小调用间隔、最大补全 tokens；
- **本地卡片**：OpenAI 兼容端点、模型名、可选 Key。

规则：

- 配置保存在用户配置目录的 `chat-config.json`（macOS: `~/Library/Application Support/FCode/`，
  可用 `FCODE_CONFIG_DIR` 覆盖；**不在 viewer 静态根下**），权限 0600，
  **不进仓库、不进事件链、不出现在任何 API 响应里**（GET 只返回
  `api_key_set` 与 `sk-…后四位`）；
- 优先级：**设置页 > 环境变量 > 默认**；留空保存 = 清除该项回落到环境变量，
  「清除密钥」按钮只清 Key；
- 价格必填且必须为正整数（fail-closed 计费不放松）；Kimi 用
  `max_completion_tokens`、不发 `temperature`；
- **写接口只接受 `application/json`，并拒绝跨站 `Origin`/`Sec-Fetch-Site`**；
  静态服务器对 `chat-config.json` 与 `.sqlite` 一律 404；
- Kimi/DeepSeek 的 `base_url` 不允许从设置/API 覆盖（只认环境变量或内置域名），
  避免一次配置写入把已保存的 key 引到别处；本地模型的端点仍可编辑；
- **当前只作用于对话**；委托运行的 provider 仍由环境变量配置
  （provider 泛化合并后统一）。

API：`GET/POST /api/settings/chat-config`、`GET /api/chat/models`。

## 执行卡（chat v2 · P1）

对话可以调用**只读工具**（ls_workspace/read_file，多轮、受权限门与 read 预算约束）；写操作仍不直接执行，模型可以在回答里**提出**一次执行。它写在回答末尾的一个
围栏块里：

```fcode_action
{"goal": "查周三上海到杭州的高铁", "mode": "research", "sources": ["https://…"], "reason": "需要实时车次"}
```

- **解析是 fail-closed 的**（`flowagent.chat.parse_action_block`）：JSON 不合法、目标不在
  5–600 字、mode 不是 `script`/`research`、块超过 2 KB，都只当作普通文本——回答原样展示，
  绝不猜测修补。多个块时取**最后一个读得通的**：模型解释格式时引用的示例，不能顶掉它真正
  做出的提案；反过来，一处坏块也不能作废前面已经成立的提案。有提案时所有块都从气泡里去掉。
- **卡片**（`viewer/core/run-card.js`，纯函数、Node 测试覆盖）在回答下方渲染目标 / 方式 /
  来源 / 审批档位，并说明「确认后才会创建运行」。用户可以改目标、切方式（切到脚本时来源
  框消失）、改来源；输入不触发重绘，光标不会跳。`取消` 只是把它收成一行「已忽略这个提案」，
  可以「重新考虑」——提案是消息本身的事实，不因界面收起而消失。
- **确认**走 `POST /api/chat/threads/{id}/confirm`：校验 `messageId` → 用
  `chat.merge_action` 复核用户的编辑（与模型自己的块同一套规则）→ 与「转为委托」同一条
  创建路径（模板、spec 校验、审批档位都不另开分支）→ 建 run → `chat.link_run` 在消息上
  落一个 `chat_run_started` 锚点。**一个提案只对应一次运行**：第二次确认返回 409 和已经
  存在的 run id，不静默改写锚点。锚点记录的是**用户批准的目标与方式**，不是模型的初稿。
- **携带上下文**：确认时把线程尾部最多 24 条、每条 600 字的对话放进 `spec.origin_thread`
  （与「转为委托」同一条 `threadOrigin()`，越近越相关）。所以「周三 / 当天 / 到了之后」
  在运行里仍然可解析。
- **进度是镜像，不是推断**：卡片的运行态订阅运行自己的 SSE（与 Work 视图同一份签名轨迹），
  显示最近三轮「轮次 · 工具 · 一句话结论 · 判定」，审批门直接就是允许 / 拒绝，结束后列出
  产物与「链已校验」。还没执行的轮次显示「等待」，不会假装完成；上报中的决定只锁住它对应
  的那道门，服务器换下一道门时按钮立刻恢复。`在 Work 中打开` 会**先切到工作模式**再打开
  这次运行的证据视图。

没有真实模型时，`FCODE_CHAT_MOCK_ACTION`（JSON）让离线 mock 追加一个脚本化的提案块，
仅 mock 读取，真实 provider 永远不看它；`tests/test_chat_action.py` 用它覆盖解析、锚点、
确认端点与编辑合并。

## 边界（诚实）

- **写操作不在对话里执行**。这是治理选择，不是能力缺口：只读工具可直接调用，写操作只能提出提案，真正执行要用户点
  「开始执行」，从那一刻起它是一次普通的受治理运行（门控、预算、审批、哈希链一样不少）。
- **附件只收纯文本**（代码、Markdown、JSON、CSV 等）。图片/PDF/Office 不在 v1；IAB 的文件选择器本身也不支持上传。
- **每线程预算**默认 60 次调用 / 300,000 µUSD / 200,000 tokens；可用 `FCODE_CHAT_MAX_*` 调整。
- **保留策略**：`FCODE_CHAT_RETENTION_DAYS`（默认 0 = 关闭）在服务启动时把超过 N 天
  未更新的线程归档；事件链与收据保留，不做硬删除。
- **没有图片理解、没有跨线程记忆、没有并行工具调用**。这些仍属于委托/评估那条线。
- **pacing 是进程级、按端点计的**（provider + base URL）；对话与委托运行共享同一张
  预约表，同一个 Kimi 组织 RPM 不会被两条调用路径同时打满。对话首调立即开跑，
  委托运行首调保守等一个间隔。
- 对话回答不可“独立重算”；它的可信性来自模型调用收据与事件链，不来自 verifier。界面不把回答标成“已验证”。

## 验收

```sh
cd flowcredit
python3 viewer/server.py --repo . --port 8643
open 'http://127.0.0.1:8643/?desktop=1'
python3 -m unittest tests.test_chat tests.test_chat_action tests.test_chat_work_bridge
node --test viewer/tests/*.test.js
```

聊天里的 Markdown 渲染、SSE 分帧和单条消息渲染是不依赖 DOM 的纯函数，放在
`viewer/core/chat-format.js` 与 `viewer/core/chat-view.js`；`viewer/tests/` 用
Node 内置 test runner 覆盖转义、围栏未闭合、行内格式、多行 `data:` 帧与
消息状态，不需要浏览器或测试框架。

Mock 下可以完整验证流式、追问、重新生成、编辑、附件、停止，以及执行卡的提出 → 编辑 →
确认 → 进度 → 审批 → 完成整条链路；真实 Kimi 需要在环境里配置 API key 与价格快照，
然后在模型菜单选择 Kimi。
