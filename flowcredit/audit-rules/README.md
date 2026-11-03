# audit-rules

版本化的审计规则库。**规则是数据,不是代码**——引擎(`flowaudit.vulns`)读它,不 import 它;规则作者不需要懂 Python。

## 为什么是 JSON 而不是 YAML

Runtime 的零依赖约束(`pyproject.toml` 的 `dependencies = []`)是 CI 强制断言的卖点。规则用 JSON,stdlib 直接解析,约束不破。

## 规则文件契约(`docs/rule-authoring.md` 是完整指南)

```json
{
  "version": 1,
  "rules": [
    {
      "id": "RULE-AGI02-001",
      "category": "AGI-02",
      "title": "Tool executes without a permission gate",
      "severity": "high",
      "trigger": { "type": "static", "match": "…" },
      "poc": { "kind": "command", "spec": "…" },
      "verdict": "poc_replay_passes",
      "references": ["file:line …"]
    }
  ]
}
```

字段语义:

| 字段 | 含义 |
| --- | --- |
| `id` | `RULE-<分类号>-<序号>`,规则稳定标识 |
| `category` | AGI 分类号(见 `flowaudit.vulns.AGI_CATEGORIES`) |
| `severity` | `critical` / `high` / `medium` / `low` / `info` |
| `trigger` | 触发条件(静态匹配或动态探针引用) |
| `poc` | PoC 规格:规则必须能生成一个**可独立重放**的复现物 |
| `verdict` | 判定契约:`poc_replay_passes` 表示重放成功才算发现 |
| `references` | 规则依据(已知案例、CWE 映射、真实样本) |

## 版本化

`version` 字段单调整数递增;规则只增不删,废弃的规则置 `"deprecated": true` 保留在库里——审计报告的历史结论要能追溯到当时的规则集。
