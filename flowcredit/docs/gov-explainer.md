# 治理解释层（model as explainer, not decider）

治理 Bot 的决策保持确定性（`model_policy: deterministic`，模型不进裁决环）。解释层在此边界之上加一块**面向操作员的模型解读**：

## 边界（由测试钉死）

- 输入只有确定性审计管线产出的 bundle 事实：findings（≤40 条，含 fingerprint/finding_id）、链验证头、回归/未覆盖面计数、授权范围摘要。原始 PoC 文本不入 prompt。
- 输出是解释文本，**不进入审计包**：bundle 在解释前后逐字节比对，任何写入即视为裁决泄漏并拒绝。
- 输入 JSON 按**不可信数据**对待：审计对象内容里的指令式文字不是指令（system 硬边界 + 测试）。
- 产物单独存 `governance.sqlite / bot_explanations`，与 bundle 表永不相混；记录 model/tokens/cost/prompt_sha256，全程可对账。
- 模型缺位（mock / 目录不可用）时 fail-closed：503，治理功能一个不少。

## API

```text
POST /api/gov/runs/{run_id}/explain   {question?, model?}   生成/重生成解读
GET  /api/gov/runs                    响应含 explanations 映射（run_id → 产物）
```

- 只对 `status=done` 且关联 `audit_id` 的治理运行开放；
- 重复调用替换旧解读（操作员显式动作）；
- 前端块固定标签：**AI 解读 · 非裁决 · 引用确定性证据 · 不进审计包**，并显示 model/tokens/prompt 指纹。

## 模型与预算

- 默认模型：`FCODE_DEFAULT_RUN_MODEL`（本机 = glm）；`max_tokens 4096`；
- provider 走与运行相同的目录纪律（可用性/价格），`complete()` 回执整数入链；
- 解释调用按次计费入 `bot_explanations`，暂不设独立日预算（调用是操作员显式动作，频率天然受限）。

## 非目标

- 不生成 verdict、不写 severity、不改 oracle——这些永远是 deterministic 管线 + 宿主；
- 不做自动解读（每次都要操作员点）；不做跨运行汇总问答（后续 slice）。
