# Fixer 受控修复通道（P3）

Fixer 有两条入口，但只有一条写路径：

1. **Work agent 生成补丁**：治理派一个 Work 修复 run，项目是审计目标的隔离副本；
   Work 产出 `changes.patch` 后，Fixer 自动导入并在自己的隔离 worktree 里再验证一次。
2. **粘贴外部补丁**：操作员或 Codex/Claude 等外部 agent 产出 unified diff，直接交给
   Fixer 验证。

无论哪种，**批准前原目录不动**，**Fixer 不会把 finding 标成“已修复”**；只有新的审计
结果能改变结论。

## Work 修复的验证命令

Work 的 project verification 走宿主机命令白名单，不是任意 shell：

- 允许的程序：`cat`、`diff`、`find`、`git`、`grep`、`ls`、`node`、`pytest`、`python`、
  `python3`、`unittest`、`wc`；
- 拒绝 inline/module 代码（如 `python3 -c ...`），要求执行工作区里的脚本文件；
- 对 `git` 的子命令/flag 有独立白名单（例如 `git diff --check` 会被拒绝）。

所以填 `pytest -q`、`python3 verify.py`、`node verify.js` 这类命令；不要填 `npm test`。

## 边界

- 只支持**仓库内的源码目录**；`.app` 等 artifact 目标拒绝（签名制品不能这样改）。
- 原目录在批准前不动；验证发生在 `work/fixes/<fix_id>/worktree`。
- 补丁只能改目标目录内的路径；拒绝绝对路径、`..`、`.git` 和二进制补丁。
- 补丁上限 200KB；隔离副本上限 5,000 文件 / 100MB。
- 批准时先 `git apply --check`，通过才 `git apply`；回执写 `apply.json`。
- 应用后通过现有手动 run 入口发起复扫（`source=console`），复核仍需操作员派复核员。

## API

- `POST /api/gov/fixes` `{audit_id, finding_id, patch}` → 隔离验证，返回 `fix`（status=proposed）。
- `GET /api/gov/fixes?limit=` → 最近修复记录。
- `POST /api/gov/fixes/<id>/approve` → 应用补丁 + 发起复扫，返回 `reaudit_run_id`。
- `POST /api/gov/fixes/<id>/reject` → 拒绝，原目录不变。

## UI

工作台 → 选择一条发现 → 「修复补丁」：

1. 粘贴 unified diff；
2. 「验证补丁」在隔离副本里 check；
3. 通过后显示文件清单与「批准并应用 / 拒绝」；
4. 批准后显示复扫运行入口，频道里能看到新的审计 run。

artifact 目标的发现不显示补丁框；`派 agent 修复` 仍保留为 Work 区的修复委托路径。

## 为什么先做补丁闸门

写代码的模型和批准写回的权力必须分开。先把隔离验证、审批、应用回执、复扫链路做扎实，
再把 Work agent 产出 patch 自动接入；否则“模型改代码 + 模型宣布修好”会破坏治理可信度。
