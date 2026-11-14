# Fixer 受控修复通道（P3）

Fixer 的第一版是**补丁闸门**，不是自动写代码的模型：补丁由操作员或外部 agent（Work /
Codex / Claude）产出，FCode 负责在隔离副本里验证、等人批准、写回原目录，并自动发起
一次复扫。**Fixer 不会把 finding 标成“已修复”**；只有新的审计结果能改变结论。

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
