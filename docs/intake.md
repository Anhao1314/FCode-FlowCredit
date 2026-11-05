# 澄清式委托：先回复，再给选项

委托入口不是问卷。用户先说一句，clarifier 理解后返回：

1. 一句回复；
2. 一段可展示的“理解摘要”（不是隐藏 chain-of-thought）；
3. 0–4 个具体选项；
4. 当信息足够时，返回任务卡。

前端只负责渲染和回传，不预设问题顺序。

## API

```http
POST /api/intake
Content-Type: application/json

{
  "message": "帮我看看这份财报能不能直接比",
  "spec": {"intent": "compare"},
  "choice": null,
  "history": [{"role": "user", "text": "..."}]
}
```

响应：

```json
{
  "reply": "我理解你关注「两期可比性」。结果怎么用？",
  "thought": "已识别：两期可比性；还缺：输出方式。",
  "choices": [
    {"id": "output:memo", "field": "output", "value": "memo",
     "label": "一页结论 + 审计底稿", "description": "...", "badge": "推荐"}
  ],
  "spec": {"intent": "compare"},
  "ready": false,
  "task": null
}
```

用户点选后，客户端回传 `choice: {"field": "output", "value": "memo"}`，再取下一轮。所有字段和取值都会在服务端校验；未知选项不会推进。

信息齐了以后：

```json
{
  "ready": true,
  "task": {
    "objective": "...",
    "template": "full",
    "approval": "consequential",
    "spec": {"version": "fcode.intake/v1", "intent": "compare", ...}
  }
}
```

任务卡确认前不会创建运行；`spec` 会写进 run 的 `binding`，因此能解释这次委托为什么是这个范围。

## 当前实现

`flowagent/intake.py::clarify` 是**无状态、确定性的占位 clarifier**：

- 从消息里识别意图、输出、审批；
- 只问还缺的字段，能推断就跳过；
- 选项根据缺失字段生成，不是固定三连问；
- 无消息时不抛选项，只邀请用户描述；
- 支持 `missing` 路径映射到 `open-ended` 模板。

它是为训练中的本地模型预留接口：模型版只需要保持相同的响应 schema，把 `clarify()` 换成一次带 JSON 输出的 provider 调用，前端和任务卡都不用改。模型输出仍会经过字段白名单校验，不能发明新选项。

## 前端行为

- 用户消息 → “正在理解…” → 模型回复 + 理解摘要 + 选项。
- 点选项视为一条用户消息，进入下一轮。
- 用户也可以不选，直接继续描述；clarifier 会重新推断。
- 每轮压一个快照，支持「上一步」；取消恢复原运行视图。
- 任务卡上的预算、审批、输出与最终创建运行一致。
- 选择结果在本机记住，下一轮标「上次选择」。

## 边界

- 当前只覆盖内置合成资料上的两期财务核查；选项不会假装支持不存在的任务。
- `thought` 是理解摘要，不是模型的完整推理过程。
- 本地 Qwen 接入前，clarifier 是规则版；接入后应保留同样的超时、错误和字段校验语义。
