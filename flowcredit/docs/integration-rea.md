# REA 制品证据融合

**状态：MVP 已落地（2026-11-03）** — FCode 现在可以通过 `--adapter rea` 审计
没有源码的交付物；REA 只提供证据，FCode 继续独占判定权。相关：
[威胁模型](threat-model.md)、[审计方法论](audit-methodology.md)、
[规则编写](rule-authoring.md)、[架构](architecture-map.md)。

## 0. 定位

FCode 原来的 `targets/repo.py` 只接受源码树：复制、哈希、文本规则扫描。
它无法审计一个已签名的 `.app`、一个 Mach-O/ELF/PE 二进制，或一个只交付
JavaScript 产物的应用。

REA 补的正是这一层：

| 层 | 所有方 | 职责 |
| --- | --- | --- |
| 目标与证据 | `flowaudit.targets.rea` + `flowaudit.rea` | 固定制品身份、调用 REA、保留原始 Evidence、投影事实 |
| 规则 | `flowaudit.vulns.engine` | 在事实快照上匹配 `trigger.type = "rea"`，只产生候选 |
| 判定 | `flowaudit.poc.replay` | 宿主 oracle 读取 pinned facts，决定 finding / not_reproduced |
| 报告 | `flowaudit.bundle` | 绑定制品、REA 版本、原始 Evidence、事实快照和 PoC 记录 |

**REA 是证据来源，不是裁判。** 一个 REA 字段、一条 `limitations`、一个
`confidence=inferred` 都不能直接晋升为 finding；只有宿主 oracle 的判定可以。

## 1. 信任边界

REA 输出和被审计目标的文件内容一样，是**不可信输入**：

- 目标可以控制自己的字符串、符号名、README、反编译文本；
- 这些字节会进入 REA 的 `normalized_result`，再进入 FCode 的事实投影；
- 因此事实投影是 **allow-list**，不是过滤器；未知字段不会自动进入规则或
  Analyst 上下文。

落点：

| 环节 | 缓解 |
| --- | --- |
| REA 子进程 | `shell=False`、固定 operation 白名单、env allow-list、超时、输出上限 |
| Evidence | 校验 `evidence_id`、`subject`、`normalized_result`、`limitations`；原始文件按 SHA-256 固定 |
| 事实 | 按 operation 提取固定字段；列表/字符串/深度有上限；float 归一化 |
| 规则 | 只支持小谓词词汇表，不执行任意表达式 |
| 重放 | 场景只读 `facts.json`；worker 的 audit hook 继续阻断 socket/subprocess |
| 晋升 | 宿主 oracle；REA 的 `confidence` 只影响候选强度，不参与晋升 |

REA 自身会启动 Ghidra/Hopper 等引擎，**这不是沙箱**；它只在宿主侧调用，
不进入 FCode 的受限 replay worker。PoC 重放继续使用 FCode 的
`replay_worker`，不用 REA 代替。

## 2. 数据流

```text
target (.app / Mach-O / JS tree)
        │
        ▼
flowaudit.targets.rea.prepare()
  ├─ 固定 artifact_sha256（主可执行文件/JS root）
  ├─ 调 REA（白名单 operation）
  ├─ 提取 facts（allow-list）
  ├─ 读取宿主事实（codesign entitlements/signature）
  └─ 写 snapshot：
       facts.json / control-facts.json / evidence/<op>.json
        │
        ▼
flowaudit.vulns.engine.rea_scan(facts)   →  Candidate（只产生候选）
        │
        ▼
flowaudit.poc.replay.replay_candidate()  →  受限 worker 读 facts
        │
        ▼
宿主 oracle（entitlements / prerelease / …）→ finding / not_reproduced
        │
        ▼
audit-bundle（制品 hash + REA evidence + facts ref + PoC 记录）
```

## 3. 使用

```bash
python audit.py audit /Applications/EvoX.app \
  --adapter rea \
  --target-md fixtures/evox/target.md \
  --rea-bin /absolute/path/to/rea \
  --rea-timeout 600 \
  --out ./work/audit/evox
```

`--target-md` 用于已安装的制品：bundle 内不能放 `target.md`，把授权、范围和
`## Declared features` 放在仓库的 target descriptor 里。EvoX 的样例见
[`fixtures/evox/target.md`](../fixtures/evox/target.md)。也可以继续用
`--declared-features` 显式传逗号分隔列表；两者同时存在时显式列表优先。

`--rea-bin` 省略时读取 `REA_BIN`，再退回 `PATH` 上的 `rea`。重复审计同一
制品会得到相同的 `manifest_sha256`，`episodes.sqlite` 会按目标哈希做
known/new/fixed/regressed 回归。

支持的目标类型：

| 类型 | 判定 | REA operation |
| --- | --- | --- |
| macOS `.app` | `Contents/Info.plist` 存在 | `inspect_desktop_application` + 主可执行文件的 `inspect_go_binary`（若适用） |
| Mach-O / ELF / PE | 文件 magic | `inspect_go_binary` |
| JS 目录 / `.asar` | 目录含 `package.json`/JS，或 `.asar` | `analyze_javascript_application` |

宿主事实：

- **entitlements**：`codesign -d --entitlements :-`，失败则写入
  `uncovered_surfaces`，不猜测；
- **签名**：`codesign --verify --deep --strict`，结果作为事实记录；
- **declared features**：`--declared-features` 优先；否则读目标目录
  `target.md` 的 `## Declared features` 列表；常见别名（microphone /
  calendar / apple-events / library-validation）会映射到 entitlements key。

## 4. 事实契约

`facts.json` 的 schema 为 `fcode.rea-facts/v1`：

```json
{
  "schema_version": "fcode.rea-facts/v1",
  "target": {
    "identity": "/Applications/EvoX.app",
    "version": "1.1.0",
    "artifact_sha256": "…",
    "format": "macho",
    "architecture": "arm64"
  },
  "operations": {
    "inspect_go_binary": {
      "evidence_id": "ev_…",
      "provider": {"id": "go-build-info", "name": "…", "version": "1"},
      "confidence": "observed",
      "authority": "shipped-artifact",
      "facts": {
        "go_version": "go1.26.10",
        "module": {"path": "github.com/EvoMap/evox-desktop"},
        "dependencies": [
          {"path": "…", "version": "…", "prerelease": false}
        ],
        "settings": {"-tags": "desktop,production"},
        "limitations": ["…"]
      }
    }
  },
  "host": {
    "entitlements": ["com.apple.security.device.audio-input"],
    "declared_features": ["com.apple.security.device.audio-input"],
    "signature": {"status": "valid", "tool": "codesign"}
  },
  "uncovered": [{"surface": "…", "reason": "…"}]
}
```

约束：

- 每个 operation 只投影固定字段；完整 `normalized_result` 不进入规则上下文；
- `inspect_desktop_application` 只保留 Info.plist、Main executable、Library、
  sidecar 相关文件（最多 80 个），不复制 4k+ 文件清单；
- `analyze_javascript_application` 只保留统计、summary、route 计数，不保留
  完整图；
- 原始 Evidence 写在 `rea-evidence/<operation>.json`，由 bundle 的
  `rea_evidence_refs` 逐条固定；
- `facts.json` 同时写入 `rea-evidence/facts.json`，由 `rea_facts_ref` 固定；
  `control-facts.json` 由 `rea_control_facts_ref` 固定。

## 5. 规则

新增 `trigger.type = "rea"`：

```json
{
  "type": "rea",
  "operation": "inspect_go_binary",
  "selector": "dependencies",
  "predicate": {"any": {"field": "prerelease", "equals": true}}
}
```

`operation` 可以是 REA operation，或 `"host"`（读取 `facts.host`）。
`selector` 是点分隔的对象键/列表索引；`predicate` 只支持：

```text
equals · matches · exists · absent · count_gt · count_lt
any · all · not · field
```

已落地两条：

| 规则 | 分类 | 事实 | 宿主 oracle |
| --- | --- | --- | --- |
| `RULE-AGI05-101` | AGI-05 | Go 依赖中的 pre-release 版本 | 重读 facts，要求存在 pre-release 行；控制投影移除后不得命中 |
| `RULE-AGI07-101` | AGI-07 | macOS entitlements | `entitlements - declared_features` 非空即 finding；控制投影声明全部后不得命中 |
| `RULE-AGI02-102` | AGI-02 | sidecar listener 无 auth marker | 宿主检查 `listener_without_auth` 非空；控制投影标记为已认证 |
| `RULE-AGI03-102` | AGI-03 | sidecar 源码中的 key-shaped literal | 宿主重读 file/line；只保留位置与 kind，不保留值 |
| `RULE-AGI05-102` | AGI-05 | sidecar 源码在运行时调用 npm install/exec | 宿主检查 `npm_install` 非空；控制投影移除 |
| `RULE-AGI05-103` | AGI-05 | Go 依赖命中离线 OSV 记录 | 宿主检查 `vulnerabilities` 非空；控制投影移除 |
| `RULE-AGI05-104` | AGI-05 | 主二进制包含运行时全局 npm 安装命令 | 宿主检查 `runtime_installers` 非空；控制投影移除 |

负对照是**同一批事实的清理投影**（`control-facts.json`），不是第二个真实
目标；报告会把这一点写进 `uncovered_surfaces`/notes。P1 会增加真实干净
目标的对照与目标特有的 oracle。规则靶子数据在
[`fixtures/rea/`](../fixtures/rea/README.md)。

## 5.1 Sidecar 源码事实

bundle 里的 Node/Node sidecar 往往同时带源码和 `runtime/npm`。直接把整个目录交给
REA JS 分析会产生数百 MB Evidence；因此 `flowaudit.rea.sidecars` 只做**源码模式扫描**：

- 只读 `.ts/.js/.mjs/.cjs`；`runtime/`、`node_modules/`、构建产物完全不读；
- 测试文件只做 inventory，不参与安全模式匹配（避免测试 fixture 误报）；
- 每个文件 ≤256 KiB，总量 ≤4 MiB，命中数有上限；
- 只记录 `file:line`、类别和计数；secret-like literal 的值从不写入；
- 提取 `listeners`、`auth_markers`、`dangerous_calls`、`secret_like`、`npm_install`。

这些是宿主事实，不是 REA Evidence；它们进入 `facts.host.sidecars`，由
`RULE-AGI02-102` / `AGI03-102` / `AGI05-102` 产生候选，宿主 oracle 再判定。

EvoX 1.1.0 的结果：

- `weixin-bridge`：2 个源文件、5 个测试；4 个 listener、16 个 auth marker；
  无未授权 listener、无 secret-like、无 npm 调用；
- `whatsapp-baileys-bridge`：19 个源文件、20 个测试；2 个 listener、5 个 auth marker；
  无未授权 listener、无 secret-like、无 npm 调用；4 个 `dangerous_calls` 仅作为事实展示，
  未自动升级为 finding。

所以这一步没有新增发现；规则的正向价值是“有证据地确认没有命中”，而不是凑数。

## 5.1.1 离线 OSV 依赖情报

`flowaudit.rea.osv` 读取操作者提供的 OSV JSON 快照（不联网），只匹配
`affected[].package.ecosystem = Go` 的记录：

- 支持 `affected[].versions` 精确匹配；
- 支持 `SEMVER` range 的 `introduced` / `fixed` / `last_affected`；
- Go 版本的 pre-release 排序低于同版本 release；
- 无法解析的版本仍可命中显式 `versions`，但不参与 range 比较；
- 结果只保留 OSV id、依赖、版本、aliases、summary、fixed_versions。

```bash
python audit.py audit /path/to/app --adapter rea \
  --target-md fixtures/evox/target.md \
  --osv-snapshot /path/to/osv-snapshot.json
```

没有提供快照时，`host.vulnerabilities = []`，并在 `uncovered_surfaces` 写明
“dependency vulnerability intelligence: no OSV snapshot supplied”；不会把
“没查”写成“安全”。

## 5.2 Library validation 深挖

`flowaudit.rea.macho` 解析主二进制的 Mach-O load commands，回答一个具体问题：

> `disable-library-validation` 是否有静态声明的加载链作为理由？

它会提取：

- `LC_LOAD_DYLIB` / `LC_LOAD_WEAK_DYLIB` / `LC_REEXPORT_DYLIB`
- `LC_RPATH`
- 每个加载项是 Apple 系统库、bundle 内文件，还是外部路径
- 非系统文件的 `codesign` Team ID
- 用户可写路径、未解析路径、嵌套 Mach-O（helper / Node runtime）的 Team ID

结果是 `facts.host.library_validation`：

```json
{
  "entitlement_present": true,
  "main_team_id": "6BM7JCYW8P",
  "loaded_count": 22,
  "system_count": 22,
  "non_team_signed": [],
  "unresolved": [],
  "user_writable_load_paths": [],
  "verdict": "no_static_justification"
}
```

EvoX 1.1.0 的结论：

- 主程序 22 条直接加载全部是 Apple 系统框架/库；
- 没有 `@rpath`，没有第三方 dylib，没有 `.node`；
- `EvoXKeepAwake` 与 Node runtime 都是同 Team ID；
- 0 个非同团队、0 个未解析、0 个用户可写加载路径；
- `verdict = no_static_justification`。

边界：只读 load commands，不观察运行时 `dlopen`/`NSBundle`。主二进制确实导入 `dlopen`/`dlsym`（部分来自 `modernc.org/sqlite`），也有 `pluginblocker` 与“external plugin installs are disabled by policy”字符串；因此结论是“静态加载链没有必要性证据”，不是“绝对不存在运行时加载路径”。这一区分会写进报告和 facts 的 `limitations`。

## 5.3 隔离动态验证

静态结论需要一次受控运行时观察来避免过度声称。EvoX 的验证方式是：

- `sandbox-exec` + 临时 `HOME`/`TMPDIR`；
- 拒绝网络与对真实用户目录的写入；
- 启动 5 秒后终止整个进程组；
- 不点击 UI、不登录账号、不发送业务请求；
- 用 `vmmap` / `sample` / `lsof` 读取已加载镜像。

EvoX 1.1.0 的结果：

- 主进程只映射主二进制与 Apple 系统库，没有应用自带 dylib、`.node` 或
  `@rpath` 第三方库；
- 因此动态启动阶段与静态 load graph 一致，进一步支持
  `library_validation.verdict = no_static_justification`；
- 但只覆盖启动后 5 秒，插件/扩展/Cowork/浏览器等后续路径未覆盖，不能据此
  断言不存在运行时 `dlopen`。

同时观察到 EvoX 首次运行会尝试：

```text
npm install -g @evomap/evolver
```

命令没有显式版本号；沙盒拒绝网络后安装被终止。这个字符串已由
`flowaudit.rea.strings.scan_runtime_installers` 固化为
`host.runtime_installers`，并由 `RULE-AGI05-104` 晋升为 AGI-05 finding。
动态确认仍是本报告的本地证据，不进入 audit-bundle 的哈希链；完整动态探针
仍需独立立项。

## 6. 审计包新增字段

`audit-bundle` 仍是 `fcode.audit-bundle/v1`（新增字段全部可选，旧包继续
可验证）：

- `target.artifact_sha256`
- `target.rea_evidence_id`
- `target.target_snapshot_sha256`
- `target.profile`（`artifact`）
- `target.manifest_sha256`：**目标内容**的稳定哈希（artifact bytes + version +
  format/arch），跨 REA 版本不变，供 `episodes` 做回归；
- `toolchain.rea.version` / `evidence_schema` / `facts_schema` /
  `manifest_sha256`：**分析依据**的哈希（target manifest + facts + Evidence +
  REA 版本），REA 升级会改变它，但不会改变 target manifest。
- `rea_evidence_refs[]`：`{evidence_id, operation, path, sha256}`
- `rea_facts_ref` / `rea_control_facts_ref`：`{path, sha256}`

`poc-evidence` 可选新增：

- `target_ref.artifact_sha256`
- `replay_environment.target_snapshot_sha256`（`target_copy: false` 时使用）

`verify-bundle --evidence <dir>` 除了 PoC 记录，还会校验每个 REA Evidence
文件的 SHA-256 与 `evidence_id`，以及 facts/control 文件的 SHA-256。

审计台(工作台侧栏「审计台」,即 `#/audit` surface)展示 `target.artifact_sha256`、`toolchain.rea`、
`rea_evidence_refs`、facts 摘要与 `uncovered_surfaces`。`/api/audit/rea/`
只读取 bundle 引用过的文件，未引用的文件与路径穿越一律 404。

## 6.1 MCP 接入

REA 原生 MCP 工具大量使用嵌套 object / union，FCode 的
`params_from_schema` 会拒绝这些 schema；因此提供一个**扁平 schema 的 wrapper**：

```text
flowcredit/scripts/rea_mcp.py        → flowaudit.rea.mcp
```

只暴露四个工具，每个只有一个 `path: string`：

| 工具 | 作用 |
| --- | --- |
| `inspect_go_binary` | Go build info / module graph / dependencies |
| `inspect_desktop_application` | bundle 解剖 / sidecar 路径 / coverage |
| `analyze_javascript_application` | JS 统计 / routes / graph 计数 |
| `inspect_library_validation` | Mach-O load commands / Team ID / 静态必要性 |

`mcp.json.example` 已附 entry：

```json
{
  "rea": {
    "command": "python3",
    "args": ["flowcredit/scripts/rea_mcp.py"],
    "env": {"REA_BIN": "env:REA_BIN"},
    "readonlyTools": [
      "inspect_go_binary",
      "inspect_desktop_application",
      "analyze_javascript_application",
      "inspect_library_validation"
    ],
    "timeout": 300
  }
}
```

四个工具都不执行目标、不改变目标状态；`analyze_javascript_application` 会启动
REA 的 JS worker，但只读输入。若主机采用更保守的策略，可把它标为 consequential。

## 7. 失败语义

| 情况 | 结果 |
| --- | --- |
| REA 未安装 | `REA_UNAVAILABLE`；审计停止，不产生“空结果” |
| REA 超时 | `REA_TIMEOUT`，不自动重试 |
| REA 非零退出 | `REA_COMMAND_FAILED`，保留 stderr 尾部 |
| 输出超过 16 MiB | `REA_OUTPUT_TOO_LARGE`，提示使用 summary/更窄 operation |
| JSON/schema 不合约 | `REA_PROTOCOL_ERROR` |
| codesign 不可用 | 写入 `uncovered_surfaces`，entitlements 规则不产生候选 |
| Wails/未知运行时 | `inspect_desktop_application` 的 candidates 可能为 0；这是识别边界，不是“没有内容”；按目标类型手动走 Go/JS 路径 |

## 8. 当前边界与下一步

已交付：

- CLI artifact adapter、facts 投影、bundle/verify 扩展；
- REA 规则：AGI-02-102、AGI-03-102、AGI-05-101/102/103/104、AGI-07-101
  （连同原有 3 条静态规则，规则集 v6）；
- sidecar 源码模式扫描（排除 runtime/node_modules，测试仅 inventory）；
- Mach-O load-command 深挖与 `library_validation` 判定；
- 扁平 schema MCP wrapper（4 个 path-based 工具），已通过 FCode MCP 桥导入测试；
- 离线 OSV Go 依赖匹配（`--osv-snapshot`）；
- 真实 REA CI lane（`.github/workflows/real-rea.yml`，合成 app + 真实 REA）；
- 审计台展示 REA Evidence、facts、library validation、sidecar 摘要与未覆盖面；
- 用真实 EvoX 1.1.0 跑通：2 份 REA Evidence、2 条 finding（AGI-07
  entitlement + AGI-05 运行时 npm 安装）、`library_validation.verdict =
  no_static_justification`、sidecar 规则未命中、`verify-bundle` 通过；
- 791 项 Python 测试（含 23 项 REA 集成测试 + 2 项 MCP wrapper 测试）全绿。

未交付（明确欠账）：

- **完整 sidecar JS 图**：当前是源码模式扫描，不是 REA 的完整 JS 应用图；
- **动态探针**：REA browser/Frida/process 工具不在白名单内；
- **真实干净对照**：当前 control 是事实投影的清理版，P1 需要第二个真实
  干净制品；
- **OSV 覆盖面**：只匹配 Go modules，且只读取操作者提供的离线快照；没有
  实时 CVE feed、JS 依赖或 reachability 分析；
- **REA 版本漂移**：bundle 固定 `toolchain.rea.version`，但 CLI 合同变化
  仍需要适配器回归测试。

## 9. 复现

```bash
# 1) 确认 REA 可用
rea --version

# 2) 跑一次 artifact 审计
python audit.py audit /Applications/EvoX.app \
  --adapter rea --target-md fixtures/evox/target.md \
  --out ./work/audit/evox

# 3) 第三方复核
python -m flowaudit.cli verify-bundle \
  ./work/audit/evox/audit-bundle.json \
  --evidence ./work/audit/evox
```