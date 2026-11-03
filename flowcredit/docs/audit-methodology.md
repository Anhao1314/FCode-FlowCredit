# 审计方法论

> 状态:**v2**(2026-10-31 增补交互模型与 Codex 对齐)。本文定义一次审计的
> 执行流程、证据纪律与产出契约。分类依据见 [threat-model](threat-model.md),
> 规则写法见 [rule-authoring](rule-authoring.md),逐项验收见
> [sprint2-acceptance](sprint2-acceptance.md)。

## 〇、交互模型:CLI 优先,三面投影

本项目自身是 agent 应用,交互结构对齐 Codex/Claude Code 一代的范式,
但裁决纪律比它们更硬:

- **会话是脊柱**:审计是用户与审计 agent 的会话(Analyst 主导,工具流每步
  过门控、入链、带收据);账本、审批门、审计包都是**同一条事件链的投影**,
  不是独立应用。
- **CLI 一等入口**:`python -m flowaudit.cli audit <target>` 提供批处理模式
  (CI/audit-as-code 基础,零网络可跑静态规则);交互式对话面随后,桌面/Web
  与 CLI 共享同一会话内核。
- **`target.md` 约定**:目标目录可放置审计档案(范围、已知面、联系点)。
  它经工具通道读入、标记为不可信数据——**对 target.md 本身的注入尝试就是
  AGI-01 样本**。
- **operator 对话规则:自由发言,硬编码裁决。** operator 在会话中的一切
  输入以 `actor=operator` 入链:可以否决发现(`false_positive_disputed`)、
  接受风险(`accepted_risk`)、暂停、扩缩范围与预算;**不能晋升发现**——
  晋升只有重放一条路,不因对话中的任何措辞改变。
- **权限模式会话级可切**:static-auto(静态自动)/ dynamic-approval(动态
  逐探针审批)/ read-only,映射到内核既有门控档位;不提供 full-access 档——
  治理产品不给自己留裸奔模式。

## 一、审计循环

```
① 测绘(profile)→ ② 静态(static)→ ③ 动态(dynamic,需授权)
→ ④ 治理(governance)→ ⑤ 报告(report,每条发现过 PoC 重放)
```

每一步的产出都是带证据指针的结构化记录,全部进审计事件链。任何一步
可以中断恢复——恢复语义与 Runtime 的任务恢复相同:已完成轮次不重复,
凭收据对账。

**静态与动态分会话**(2026-10-31 设计评审后修订):两阶段共享同一个
`audit_id` 与不可变目标清单(版本、配置哈希、环境),但使用独立会话——
静态阶段积累的不可信内容不带入高权限的动态阶段;阶段间只传递带哈希的
结构化工件,不继承整段对话。

### ① 测绘

回答"这是什么、能碰到什么":应用形态(单二进制/Electron/脚本)、数据
目录树与权限位、entitlements 与 OS 授权、网络监听、持久化项、自更新
机制。产出权限暴露面图。**本步纯只读。**

### ② 静态

不运行目标。规则引擎扫:注入面(不可信文本→上下文路径)、工具权限模型
(有无门控/自我批准/审批粒度)、危险数据流(输出→shell/eval/SQL/沙箱外写)、
秘密存储形态、声明-实现一致性(README 逐条对实现)。静态发现的证据是
`file:line` + 代码摘录。

### ③ 动态(需 operator 授权)

在隔离环境运行目标并施放探针:注入 payload、工具滥用诱导、边界探测、
崩溃恢复(SIGKILL 后副作用对账)、预算耗尽行为。探针是版本化数据
(`audit-rules/` 引用),有频次与预算上限。动态发现的证据是完整请求/
响应对与事件链摘录。

### ④ 治理

检查目标自己的纪律:审计日志防篡改性(试着改,看能否发现)、审批流
存在性、签名/收据机制的有效性(密钥是否与数据同库)、崩溃恢复语义。

### ⑤ 报告

Finding 结构见 `flowaudit.report`。**候选发现必须 PoC 重放成功才进报告**;
重放结果三值化,FAIL 不当安全结论:`not_reproduced`(保留候选,可补证
重试)/ `invalid_poc`(harness 或 oracle 自身缺陷,如实记录)/
`not_vulnerable_under_scope`(需负对照证据,预期罕见)。重复发现按**根因
归并**计分,不按 PoC 数量计分。报告附:授权声明与范围、目标清单哈希、
PoC/oracle/重放器版本、负对照与清理结果、**未覆盖面与无法测试项单列**、
以及审计自身事件链的校验入口。验收标准:第三方仅依据审计包即可重放结论。

## 二、证据纪律

1. **只看权限位,不读密钥内容。** 秘密检测报"存在 key 形态字符串"的计数
   与位置,不输出内容。泄漏测试使用**植入的伪秘密**与专用测试账号,以
   "伪秘密是否出现在禁止出口"判定,不接触真实凭据——审计报告本身不能
   成为泄漏源。
2. **证据必须可重放。** 每条发现的证据是命令/请求,不是截图,不是转述。
3. **好实践也记录。** 权限收敛、签名完整、无持久化项——正面发现进报告,
   评分卡需要分母。
4. **失败保留在分母里。** 没找到的已知缺陷如实报告"未检出",与 Runtime
   基准的预置失败同一纪律。

## 三、靶子与验收

- `fixtures/fcode-self/`:7 个已确认自体缺陷,划分为 **dev 集**
  (FCODE-01/02/06,参与规则开发)与**盲测集**(FCODE-04/07,不进规则、
  提示词与任何开发示例);FCODE-03/05 预留。引擎验收线 = 已知样本回归
  检出(dev 集)+ 盲测集检出 + 无缺陷负对照零误报——"独立检出"一词
  仅在规则开发未接触该样本时才可使用。
- `fixtures/synthetic/`:每条规则一个最小靶,Sprint 2 起与规则同步提交;
  另建对已知缺陷做路径/命名/结构**变异**的样本,防规则过拟合。
- 靶子文件头注明缺陷位置与分类号——靶子是声明的,不是暗桩。

## 四、边界

- 应用层沙箱不构成 OS 级隔离:动态探测桌面应用前先整目录备份;探针跑在
  容器/快照中的方案在 Sprint 4 落地,当前动态步仅对已授权且可备份的目标开放。
- **两个对象两种约束,不混用**:由 Analyst 生成的 **PoC 重放**必须运行在
  能力受限环境(无真实凭据、默认禁网、临时文件系统、目标独立副本)——
  这是 confused deputy 防线,没有能力隔离就不执行;**驱动目标应用本身**
  的探针无法被沙箱(目标是审计对象),靠授权前置、备份前置、审批门控与
  honeypot 外传检测约束。
- 消息类探针(AGI-01)只使用测试账号/自建会话,不触碰真实联系人。
- 审计发现面向修复,不面向武器化(见 threat-model 第五节)。

## 五、spec-gap-report:规范空白判定

`spec-gap-report` 是 **audit-bundle 之外的独立 JSON**,用于记录两类当前
finding 结构无法表达的判定:

- **conforms**:已检查目标行为,且目标文档明确允许该行为;
- **undetermined**:检查项命中事实,但目标文档没有给出可判定的规范,
  无法判为 finding,也无法判为安全。

没有判定时,`judgments` 为空数组,报告仍可有意义(表示本次没有生成
规范空白结论),但它不是 finding,也不替代 `uncovered_surfaces`。

### 结构

报告顶层:`schema_version`、`report_id`、`audit_id`、`target`
(`identity` / `version?` / `artifact_sha256` / `session_log_sha256`)、
`generated_at`、`judgments`。`target.artifact_sha256` 是被审计应用产物的
哈希,`target.session_log_sha256` 是本次判定依据的会话日志哈希。

每条 judgment 必须有 `judgment_id`、`subject`、`verdict`、`evidence_refs`。
条件规则在 writer 代码层检查(schema 不支持 if/then):

- `verdict=conforms`:必须带 `basis.doc_refs` 且至少一条;
- `verdict=undetermined`:必须带 `missing_spec`,含 `question`、
  `searched_refs`、`result`。

### evidence_refs 与 absence_refs

- `evidence_refs` 指向 DSH 的现存事件:
  `{kind, session_id, seq, event_type, log_path, log_sha256, log_format}`。
  `session_id + seq + event_type` 唯一定位事件;`log_path` 便于取回日志;
  `log_sha256` 固定日志内容。
- `absence_refs` 用于“事件未出现”的断言(例如零 `approval/asked`):
  `{kind, session_id, event_type, scope, log_path, log_sha256, log_format}`,
  其中 `scope` 必须是 `{from_seq, to_seq, turn?}` 的结构化区间,不接受自由字符串。
  **absence 断言必须 pin 住它基于的日志(`log_path + log_sha256`),否则
  第三方无法复核“未出现”。**
- `log_format` 为 `jsonl` 或 `zstd`。**即使 `log_format=zstd`,
  `log_sha256` 也固定为解压后 raw JSONL 字节流的 SHA-256**,这样第三方
  先解压再按同一字节流复算。

### 生成命令

```bash
python -m flowaudit.cli spec-gap-report \
  --input audit-rules/schema/examples/spec-gap-report-example.json \
  --out /tmp/spec-gap-report.json
```

`--audit-id` 可覆盖输入中的 `audit_id`;不提供 `--out` 时默认写到
`work/audit/<audit_id>/spec-gap-report.json`。输入或 schema 校验失败
返回非零且不写文件。该命令只负责落盘,不读取 DSH 日志,也不生成判定。
