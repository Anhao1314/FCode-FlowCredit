# 规则编写指南

> 规则是数据(`audit-rules/*.json`),不是代码。引擎读它,不 import 它;
> 规则作者不需要懂 Python。格式契约与 `audit-rules/README.md` 一致,本文
> 讲怎么写一条**合格的**规则。

## 一、一条规则的解剖

```json
{
  "id": "RULE-AGI02-001",
  "category": "AGI-02",
  "title": "Remembered approval keyed by tool name whitelists all future arguments",
  "severity": "medium",
  "trigger": {
    "type": "static",
    "match": "approval memory keyed on tool name only"
  },
  "poc": {
    "kind": "command",
    "capabilities": ["workspace write", "no network", "no credentials"],
    "stimulus": "approve 'run_command ls' with remember=true; then issue 'run_command python payload.py'",
    "oracle": {
      "kind": "event_absent",
      "spec": "no approval event recorded for the second call; the host checks the event chain itself and never reads the PoC output as verdict"
    },
    "negative_control": "same stimulus with remember=false must produce an approval event"
  },
  "verdict": "poc_replay_passes",
  "references": ["fixtures/fcode-self/known-issues.json#FCODE-02", "viewer/console_runtime.py:722-727"]
}
```

| 字段 | 硬性要求 |
| --- | --- |
| `id` | `RULE-<分类号>-<序号>`,永不复用 |
| `category` | 必须在 `flowaudit.vulns.AGI_CATEGORIES` 内 |
| `severity` | `critical`/`high`/`medium`/`low`/`info` |
| `trigger` | `static`(可局部匹配)、`probe`(引用版本化探针代码)或 `rea`(匹配 REA 事实快照);**含糊的触发条件 = 不可实现的规则** |
| `poc.capabilities` | PoC 声明的能力范围(文件/网络/凭据/副作用);宿主校验实际重放不越声明——进程隔离不是能力隔离 |
| `poc.stimulus` | 必须能生成**一个能力受限环境可重放**的复现物(无真实凭据、默认禁网、临时文件系统、目标副本);"手动检查"不是 PoC |
| `poc.oracle` | 判定不变量,由宿主执行与读取;**PoC 是刺激物不是裁判**,PoC 自报 PASS 无效 |
| `poc.negative_control` | 未满足触发条件(或已修复)时必须不通过——没有负对照的 PoC 不可信 |
| `verdict` | 重放结果四值:`finding` / `not_reproduced` / `invalid_poc` / `not_vulnerable_under_scope`;只有 `finding` 进报告,FAIL 不当安全结论 |
| `references` | 至少一条:真实样本指针或 fixtures 靶子 |

## 二、合格规则的四条验收线

1. **可判定**:给定靶子,两条不同的执行要么都命中要么都不命中——判定
   不能依赖模型的心情。需要语义判断的部分写在探针的固定输入里,不写在
   判定里。
2. **可重放**:PoC 是命令/请求序列,独立进程执行,产出机器可读结果。
3. **有分母**:规则必须能在 fixtures 里指出它对应的靶子;没有靶子的规则
   进 `status: "draft"`,不参与评分。
4. **不武器化**:PoC 证明缺陷存在即止(见 threat-model 第五节)。

## 三、规则的生命周期

```
draft(无靶子)→ active(靶子就位,验收线通过)→ deprecated(只标记,不删除)
```

规则只增不删。审计报告引用规则时同时记录规则集版本,历史结论要能追溯到
当时的规则状态。`audit-rules/index.json` 的 `version` 单调递增。

## 四、当前规则集(2026-11)

只做 dev 集(FPCODE-01/02/06)对应的确定性规则;**盲测集(FCODE-04/07)
不进入规则、提示词与示例**;FCODE-03/05 预留。

| 规则 | 分类 | 对应靶子(dev 集) |
| --- | --- | --- |
| 审批记忆参数无关免审批 | AGI-02 | fcode-self #FCODE-02 |
| 工具执行绕过门控/无咽喉点 | AGI-02 | fcode-self #FCODE-06 |
| 控制台无 token 模式写保护可绕过 | AGI-07 | fcode-self #FCODE-01 |
| Shipped dependency pre-release | AGI-05 | `RULE-AGI05-101` / `fixtures/rea/facts-prerelease.json` |
| Entitlements 超出声明功能 | AGI-07 | `RULE-AGI07-101` / `fixtures/rea/facts-entitlements.json` |
| Sidecar listener 无 auth marker | AGI-02 | `RULE-AGI02-102` / `fixtures/rea/sidecar-unauth.json` |
| Sidecar 源码 key-shaped literal | AGI-03 | `RULE-AGI03-102` / `fixtures/rea/sidecar-secret.json` |
| Sidecar 运行时 npm install/exec | AGI-05 | `RULE-AGI05-102` / `fixtures/rea/sidecar-npm.json` |
| Go 依赖命中离线 OSV | AGI-05 | `RULE-AGI05-103` / `fixtures/rea/facts-vulnerable.json` |
| 主二进制运行时 npm 安装 | AGI-05 | `RULE-AGI05-104` / `fixtures/rea/facts-runtime-install.json` |

验收:dev 集回归检出 + 盲测集检出 + 负对照零误报;每条发现一条命令可重放。
"独立检出"仅可用于规则开发未接触的样本。

## 五、规则与探针的边界

`trigger.type=static` 只覆盖**局部可匹配**的模式。跨文件数据流、运行时权限
组合、模型行为类缺陷,用**版本化探针代码**承载:探针是仓库内代码(自带测试、
进评审),规则以 `trigger.type=probe` 按 id 引用它。探针代码不引入第三方依赖,
与内核同一零依赖纪律。

`trigger.type=rea` 用于 shipped artifact：REA 先把目标投影成
`fcode.rea-facts/v1` 的事实快照，规则只匹配快照，不直接读目标、不执行
REA。格式：

```json
{
  "type": "rea",
  "operation": "inspect_go_binary",
  "selector": "dependencies",
  "predicate": {"any": {"field": "prerelease", "equals": true}}
}
```

`operation` 为 REA operation 或 `"host"`；`selector` 是点分隔的对象键/列表
索引；`predicate` 只支持 `equals / matches / exists / absent / count_gt /
count_lt / any / all / not / field`。规则仍必须声明 `poc.scenario =
"rea_fact_evidence"`、`probe_params` 与 `control_params`，并提供一个宿主
oracle；REA 事实本身不是 finding。详见 [integration-rea](integration-rea.md)。
