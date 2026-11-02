# 能力图谱(Capability Map)

> 本文档面向两类读者:**想快速判断这个项目深度的工程师/面试官**,和**想复现任何一条能力的使用者**。
> 规矩沿用项目自己的纪律:每条能力对到**可指出的代码**和**可现场复现的命令**;没有对应证据的,直接写"未覆盖",不凑。
> 数字口径:Python 测试以 `python -m unittest discover -s tests -q` 为准(当前 679 项),前端以 `node --test viewer/tests/*.test.js` 为准(166 项)。数字由 CI 在每次提交时重新推导。

## 一、Agent 应用工程

| 能力 | 证据 | 现场复现 |
| --- | --- | --- |
| 设计 Agent 执行循环 | `runtime/loop.py`:单轮提议 → 门控 → 执行 → 观测 → 判定,终止策略与多轮驱动分离 | `python demo.py full` 看 9 轮推进 |
| 工具调用与参数校验 | `tools/registry.py` + `tools/base.py`:声明式 schema,未知键拒绝而非忽略 | `python -m unittest tests.test_security` |
| 权限与最小授权 | `runtime/permissions.py` 五道检查(注册表/任务范围/授权/效果分级/参数),单一咽喉点 | `python demo.py tool-denied` |
| 上下文与记忆管理 | `memory/context.py`:派生而非累积,观测压缩成决策字段 | 读 `state()` 与 `_summarise()` |
| 失败处理与未知结果 | `runtime/harness.py`:结果未知不重试,凭签名收据对账;四值词汇表禁止把 FAIL 当"不存在" | `python demo.py unknown` |
| 成本控制 | `runtime/budget.py` 四维硬预算,发送前扣减(事务级 CAS,并发不可双过) | `python demo.py budget` |
| 崩溃恢复与幂等 | `runtime/loop.py`:真 SIGKILL 后从事件链重建,断言无轮次重复;`store.load` 的 CAS 纪律连审计探针也被约束 | `python demo.py crash` |
| 可观测性 | `trajectory/recorder.py`;trace 含完整事件链,前端可回放并重验哈希 | `python demo.py full --open` |
| 双模型接入 | `providers/openai_compat.py`:OpenAI 兼容引擎,价格主机审定(µUSD 整数)、调用前预留;实测接通 deepseek-flash 与 TypeSafe Jev(systemone 决策模型) | 冒烟记录见 `docs/`;付费在线评测按纪律保持 `NOT_RUN` |

## 二、Agent 安全与评测(审计域 flowaudit)

| 能力 | 证据 | 现场复现 |
| --- | --- | --- |
| 漏洞分类法 | `src/flowaudit/vulns/__init__.py`:AGI-01~09,每类锚定真实样本 | `python -c "from flowaudit.vulns import AGI_CATEGORIES"` |
| 规则即数据引擎 | `vulns/engine.py`:加载 JSON 规则,draft 规则不参与检测(有测试钉死) | `python -m unittest tests.test_audit_vertical.EngineTests` |
| 静态审计与证据指针 | `targets/repo.py` + 规则 trigger:命中记录 path/line/snippet | 同上 |
| 受限重放沙箱 | `poc/replay_worker.py`:启动剥 env(无凭据)、`sys.addaudithook` 阻断 socket/子进程、临时文件系统、目标独立副本 | `python -m unittest tests.test_audit_vertical.EndToEndTests.test_replay_worker_refuses_network_escape` |
| 宿主侧 oracle | `poc/replay.py`:判定读目标事件链,PoC 自报 PASS 无效(枚举+测试双重拒绝) | `python -m unittest tests.test_audit_contracts.TamperDetectionTests` |
| 四值结果状态机 | finding / not_reproduced / invalid_poc / not_vulnerable_under_scope;负对照违反一律 invalid_poc | `python -m unittest tests.test_audit_vertical` |
| 负对照与对照证据 | 每条 PoC 必带负对照;`schema/poc-evidence.schema.json` 钉死字段 | `audit-rules/schema/examples/` |
| 审计包与第三方复核 | `bundle.py` 导出 fcode.audit-bundle/v1;`cli verify-bundle` 独立复核 | `python -m flowaudit.cli verify-bundle <bundle> --evidence <dir>` |
| 规范空白判定 | `audit-rules/schema/spec-gap-report.schema.json` + `flowaudit/spec_gap.py`;独立 JSON,不进入 audit-bundle | `python -m flowaudit.cli spec-gap-report --input audit-rules/schema/examples/spec-gap-report-example.json --out /tmp/spec-gap-report.json` |
| 审计器自身威胁模型 | `docs/threat-model.md` §3.1:目标可控字节→通道→缓解,逐环节成表 | 读表 |
| 自审计闭环 | 对本仓库审计端到端产出 FCODE-02 真实 finding(审批记忆参数无关免审批),证据含正反对照 | `python -m flowaudit.cli audit . --out /tmp/a` |

## 三、工程素养

| 能力 | 证据 | 现场复现 |
| --- | --- | --- |
| 零依赖 / 可移植 | `pyproject.toml` `dependencies = []`,CI 第一步断言为空;规则用 JSON 不用 YAML | CI "Confirm there is nothing to install" |
| 数据完整性与密码学用法 | `core/store.py` 序列号 CAS + 追加式事件;`core/hashing.py` canonical JSON(拒 float);`core/receipts.py` HMAC + `compare_digest` | 改一个字节,`load` 抛 `CHAIN_TAMPERED` |
| 状态机建模 | `core/contracts.py` 显式迁移表,存储层强制 | `test_illegal_transition_is_rejected` |
| 测试设计 | 679 项 Python(单元/集成/端到端/真 SIGKILL/对抗用例)+ 166 项前端;Ubuntu/macOS × Py3.11/3.13 | `python -m unittest discover -s tests -q` |
| 契约即测试 | 证据 Schema 用 stdlib 迷你校验器 enforced;8 类篡改用例(自报 PASS/非宿主 oracle/改环境标志…)全部被拒 | `python -m unittest tests.test_audit_contracts` |
| 前端基础 | `viewer/`:零构建 ES module、`crypto.subtle` 前端重验哈希、SSE 流式;审计台 mockup 已出 | `python viewer/server.py --repo .` |
| 文档能力 | `docs/`:架构、威胁模型、审计方法论、规则指南、外部评审裁决记录(`design-reviews/`) | 随便打开一篇 |
| AI 协作工程 | 人掌握 Design(评审裁决记录可查),AI 执行杠杆;对抗性外部评审 49 条逐条裁决归档 | `docs/design-reviews/2026-10-31-kimi.md` |

## 四、明确未覆盖(诚实清单)

- **RAG / 向量检索**:没有。工具是精确读取,不是检索。
- **多智能体编排**:没有。单 Agent 执行,理由见 design-decisions;并行探针留给子代理扩展。
- **在线模型评测**:已冒烟(deepseek-flash / jev-1.14.0 均实测可用),付费正式评测按纪律保持 `NOT_RUN`——凭据存在不等于授权。
- **OS 级沙箱**:重放隔离是"审计钩子 + env 剥离 + 临时副本"强度,不是容器/VM;容器层在路线图(Sprint 4)。
- **分布式**:单机 SQLite,无多副本与分布式锁。
- **前端框架化**:刻意零构建、零运行时依赖,模块化靠 ES module 拆分。
- **动态探针全量**:消息/HTTP 探针框架已设计,Sprint 2 只交付了静态规则 + 受限重放闭环。
