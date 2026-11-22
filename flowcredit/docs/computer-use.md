# Computer Use（工具级 MVP）

> 状态：P1 已落地，默认关闭；范围是 **macOS 桌面 + 独立外部浏览器**。
> 这是一条受治理的能力扩展，不是常驻自动化，也不是无审批的“随便点”。

## 它解决什么

Work 原有的 `browser_inspect` / `browser_act` 只能操作 `127.0.0.1 / localhost / ::1` 的本地预览。真正的工作经常发生在 TextEdit、Finder、Safari 这类本机应用或外部网站上；把这些动作直接交给模型，会同时放大误操作、隐私和越权风险。

Computer Use 的做法是：**主机拥有策略，操作员拥有授权，模型只拥有会话内的执行权**。每次动作仍然走现有工具注册、审批、预算、事件链与收据，不新增私有链格式。

入口是 Work 输入框工具栏的「电脑」开关（默认关闭；Chat 模式没有这个开关）。**打开开关只代表该任务允许申请，不等于授权**；真正的权限来自下面这次会话级审批。

## 权限链

```text
run spec: {"computerUse": true}        ← 操作员在工作台显式开启，默认没有
  │
  ▼
computer_request(mode, apps, domains, duration_seconds, max_actions, allow_destructive, reason)
  │  写入审批卡；模型看到的只是“申请”
  ▼
operator approves                       ← 会话级授权，一次批准覆盖后续动作
  │
  ▼
host session (scope + deadline + counters, 模型不可扩大或续期)
  │
  ├── computer_look  → AX 树 / 外部页面 + 截图
  └── computer_act   → AX 优先的动作；坐标回退需 allow_destructive
        │
        └── 每次 before/after PNG + AX/DOM 快照 → run artifacts → 链上只存路径/哈希/整数
```

- `computer_request` 在 general domain 的 `APPROVAL_TOOLS` 里；批准后会话才存在。
- 会话默认时长 300 秒（上限 900），默认动作上限 30（上限 100）；`look` 不占动作额度。
- 会话只存在于当前 run 进程内；重启、run 结束、`computer_stop` 都不恢复。

## 工具

| 工具 | effect | 说明 |
| --- | --- | --- |
| `computer_request` | write（审批） | 申请会话；App/域名只能从主机 allowlist 里选 |
| `computer_look` | read | 桌面：前台窗口 + AX 树 + 截图；浏览器：页面文本/元素 + 截图 |
| `computer_act` | write（会话内） | 桌面：`click_element` / `click_xy` / `type` / `press` / `scroll`；浏览器：`selector` 的 click/fill/press |
| `computer_stop` | read | 释放输入、关闭 sidecar、写结束记录 |

`computer_act` 不逐动作弹审批；它依赖会话 scope。`click` 没有 `element_id` 时等价于坐标点击，必须 `allow_destructive=true`，并且 `x/y` 缺一不可。

## 主机配置

策略只从本机读取，模型不能写：

```json
{
  "allowed_apps": [
    {"bundle_id": "com.apple.TextEdit", "name": "TextEdit"},
    {"bundle_id": "com.apple.Safari", "name": "Safari"}
  ],
  "allowed_domains": ["example.com", "docs.example.com"],
  "denied_apps": ["com.apple.Terminal", "com.apple.keychainaccess"],
  "max_session_seconds": 900,
  "max_actions": 100
}
```

默认路径 `~/.fcode/computer.json`；可用 `FCODE_COMPUTER_CONFIG` 指向别的文件。示例见 `flowcredit/computer.json.example`。**配置缺失 = 什么都不允许**，`computer_request` 返回 `COMPUTER_CONFIG_MISSING`，不会静默放宽。

## 桌面执行（macOS）

`viewer/computer_sidecar.py` 是可选 pyobjc sidecar；依赖缺失时 `available:false`，不会让 run 白死。pyobjc 必须装在**运行 FCode 的同一个解释器**里（sidecar 用 `sys.executable` 启动）：

```bash
python3.12 -m pip install -r requirements-computer.txt
python3.12 viewer/server.py --repo .
```

macOS 自带的 Python 3.9 在当前 Command Line Tools 下可能无法从源码构建 `pyobjc-core`；优先使用 3.11+（项目 CI 覆盖 3.11/3.13）。

`requirements-computer.txt` 同时包含 Pillow：它只用于把安全字段区域涂黑。Pillow 缺失时桌面 `look` 仍可运行，但带安全字段的截图会被删除而不是留下未涂黑证据（fail-closed）。

- 首次使用必须在系统设置里授予 **辅助功能（Accessibility）** 与 **屏幕录制（Screen Recording）**；未授权时返回 `COMPUTER_PERMISSION_DENIED`，不自动绕过 TCC。
- `look` 只截目标窗口，不截全屏；AX 树按深度/节点数截断，元素 id 用稳定路径（如 `0/3/2`）。
- `AXSecureTextField` 的值不读取、不可输入；它的 bounds 会在截图落盘前涂黑。
- 破坏性标题（删除、支付、发送、重置、关机等中英文关键词）在 `allow_destructive=false` 时返回 `COMPUTER_DESTRUCTIVE_BLOCKED`，动作不执行。
- 动作前后校验前台 App/窗口；焦点漂移返回 `COMPUTER_FOCUS_CHANGED`。AX 元素失效返回 `COMPUTER_ELEMENT_STALE`，由模型决定是否申请带坐标权限的会话。

## 外部浏览器

`viewer/browser_external.mjs` 与本地预览的 `browser_session.mjs` 完全分离，避免削弱 loopback 策略：

- 临时 Chromium profile，不导入用户 cookie、密码或扩展；`acceptDownloads:false`，文件选择器被清空。
- 顶层导航和每个子请求都过域名 allowlist；`localhost`、私网、`file:`、`data:` 与带凭据 URL 一律拒绝。
- 只支持 selector 的 click/fill/press；找不到元素返回 `BROWSER_SELECTOR_NOT_FOUND`。
- 不处理验证码，不做反检测伪装。

## 证据与链安全

每次 `look` / `act` 都在 run 的 `computer/` 目录留下：

- `look-*.png` 或 `act-*-before.png` / `act-*-after.png`
- `look-*.json`（AX 树）或 `look-*.html`（DOM）
- 工具结果里的 `{path, url, bytes, sha256, width, height}`

工具结果是 **chain-safe** 的：没有浮点数（坐标、时长、尺寸全是整数），像素不进事件链。截图通过 `/api/runs/<id>/computer-frame` 提供，只允许 run 目录内 `.png` / `.json`，20 MiB 上限。

安全字段涂黑是 fail-closed：一旦存在 secure region 但 Pillow 缺失或图像无法解码，截图会被删除（`redacted_regions: -1`），而不是把未涂黑的帧留在证据目录。

## 失败码

| code | 含义 |
| --- | --- |
| `COMPUTER_UNAVAILABLE` | sidecar/依赖缺失，或 sidecar 退出 |
| `COMPUTER_PERMISSION_DENIED` | macOS 辅助功能/屏幕录制未授权 |
| `COMPUTER_CONFIG_MISSING` | 本机没有 allowlist |
| `COMPUTER_SESSION_REQUIRED` | 还没批准会话 |
| `COMPUTER_SESSION_MODE` | 当前会话不覆盖 desktop/browser |
| `COMPUTER_APP_DENIED` / `COMPUTER_DOMAIN_DENIED` | 目标不在 allowlist |
| `COMPUTER_SCOPE_INVALID` | scope 形状/上限非法 |
| `COMPUTER_ACTION_LIMIT` | 动作额度用尽 |
| `COMPUTER_LOOK_LIMIT` | `look` 截图数量达到 120 张上限 |
| `COMPUTER_ACTION_INVALID` | 缺参数、桌面会话传 selector、浏览器动作用 scroll 等 |
| `COMPUTER_SECURE_FIELD` | 目标是安全输入框 |
| `COMPUTER_DESTRUCTIVE_BLOCKED` | 危险标题或未授权的坐标点击 |
| `COMPUTER_WINDOW_NOT_ALLOWED` / `COMPUTER_FOCUS_CHANGED` | 前台窗口越界或漂移 |
| `COMPUTER_ELEMENT_STALE` | AX 元素已失效 |
| `COMPUTER_TIMEOUT` | sidecar 请求超时 |

## 验证

```bash
# 策略、会话、工具契约、涂黑与参数校验（无需 GUI）
PYTHONPATH=src:viewer python3 -m unittest tests.test_computer_tool -v

# 前端渲染与审批卡
node --test viewer/tests/approval-card.test.js viewer/tests/task-thread.test.js viewer/tests/intake.test.js

# Work 线程的 mocked 证据帧
cd viewer && npx playwright test e2e/work-thread.spec.js
```

真机验收（需要操作员手动授权）覆盖：TextEdit 的 look → AX 点击 → 输入 → before/after；坐标回退与 Retina 对齐；会话超时/动作上限；破坏性阻断；安全输入框；外部浏览器 allowlist 内外导航；run stop 后输入释放、sidecar 退出。

## 非目标

- 不做常驻后台 computer use，不做无会话自动操作，不做提权/root。
- 不操作密码管理器、钥匙串、银行/支付类 App；不读写剪贴板；不执行 shell。
- 不破解验证码，不做反检测伪装，不导入用户浏览器凭据。
- 第一版只支持 macOS 桌面 + 外部浏览器；Windows/Linux 不在范围。
- 破坏性识别是关键词启发式，不是语义保证；高风险场景请只给窄 App 范围。
