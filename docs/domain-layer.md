# 域层：内核、模块与装配

这份文档记录域层的边界、当前保留的两个模块，以及这次清理移除的东西。
它回答三个问题：内核知道什么、模块负责什么、谁把两者接起来。

## 三层

```
core/ + runtime/ + 共享 tools/          内核：门控、预算、执行、独立重算、哈希链
        ↑ 只依赖接口
domains/                                模块：世界、工具、规划器、验证器、结果工具
        ↑ 由装配层显式选择
demo/ + viewer/server.py                装配：场景 → 模块，Provider → 运行
```

**内核不 import 任何模块。** `runtime/harness.verify` 只把 observation 交给
调用方给的 verifier；`runtime/loop` 只认 `result_tools` 这个字符串元组。
“什么算结果、怎么独立重算”由模块回答。

**装配层是唯一选择具体实现的地方。** `demo/fixtures.py` 的每个 `Scenario`
都显式写出 `module`，没有默认值——新场景必须自己决定它跑在哪个域上，而不是
被静默绑到某个历史默认。`demo/runner.py` 与 `viewer/server.py` 用
`get_domain(scenario.module)` 组装 world、工具、planner 与 verifier。

## 当前注册的两个模块

| 模块 | key | 角色 |
| --- | --- | --- |
| `general-work` | `general-work` | **默认入口，通用 agent 原型。** 文件读写、锚定编辑、受限命令、网页抓取；结果是文件状态哈希。 |
| `finance-research` | `finance-research` | **参考负载。** 合成公司、七个只读工具、蓝图规划器、独立重算毛利率。可靠性场景与 SFT 保真测试跑在它上面。 |

`general-work` 在注册表里排第一：控制台的默认模板、`list_domains()[0]` 与
`python demo.py general-research` 都指向它。金融模块没有被删掉，因为它提供了
一个可精确重算的世界，是 `full / budget / crash / unknown / open-ended`
这些故障注入场景的确定性载体，也是 prompt 保真与 SFT 管线的回归基线。它是
**参考负载**，不是产品定位。

## 这次清理掉了什么

- `domains/credit.py`、`tools/credit/`、`demo/credit.py`、`credit_demo.py`
- `labs/credit_dd/`、`labs/sec_10k/`（依赖 credit 的 memo 与世界）
- `docs/credit-due-diligence.md`、`docs/sec-10k-extraction.md`
- 对应的四个测试文件，以及 `memory/context.py` 里只为 credit 存在的分支

原因不是这些代码质量差，而是范围决定：当前方向是通用 agent 原型，不再维护
一条专用的信用尽调产品线。完整实现保留在 git 历史里（`git log --all --
flowcredit/src/flowagent/tools/credit`），需要时可以按模块协议重新接回。

清理后 `memory/context.py` 不再出现 `compute_credit_metric` 之类的工具名；
内核与共享记忆层不认任何具体域的结果字段。

## 加一个域要做什么

1. 在 `tools/<name>/` 实现 world（含 `apply_state` 重启钩子）、工具、
   planner（`decompose` / `advance`）、验证器（独立重算）。
2. 在 `domains/<name>.py` 构造一个 `DomainModule`：`key`、`title`、
   `create_world`、`build_tools`、`make_planner`、`verify`、`result_tools`、
   `approval_tools`、`report_template`。
3. 在 `domains/__init__.py` 注册，并在某个 `Scenario` 上显式写
   `module="<key>"`。
4. 不需要改 `core/`、`runtime/` 或 `harness`。

`DomainModule` 的注释把这条契约写在代码里：模块自给自足，内核只在“模型不能
自报完成”这一点上保留否决权。

## 已知的剩余耦合（诚实边界）

清理之后，还有两处“参考负载的形状”留在共享代码里，它们不影响通用委托的
**运行**，但影响真实模型在通用委托里**看到什么**：

1. ~~**`memory/context.py` 的 state / summary。**~~ **已解决（2026-10-25）**：
   压缩进 `ToolSpec.summarize`（`tools/*/summaries.py`），状态视图进
   `DomainModule.summarize_state`，`memory/context.py` 不再出现工具名；
   finance 键集逐字节保留（v10 合约），general 得到文件/命令/抓取状态卡。
   设计与测试见 [context-management.md](context-management.md)。
2. ~~**`providers/prompt.py` 的 system prompt 串域。**~~ **已解决**：
   通用委托显式选择 `fcode.prompt/general-v3`，不再继承财务数值与只读规则。
   通用系统合约按角色、输出协议、工作方式、拒绝恢复、证据验收分节；域指令
   只有 `GENERAL_DOMAIN_INSTRUCTION` 一处定义。解析器拒绝多动作与未知顶层字段，
   不再静默丢弃 `steps[1:]`。finance 的 v10、2271 条 SFT 样本和历史上下文仍走
   缺省 legacy profile，黄金哈希不变；新域必须在模块上显式声明自己的 profile。

## 验收

```sh
cd flowcredit
python demo.py general-research --fast     # 通用委托，离线脚本模式，5 轮
python demo.py all --fast                  # 7 个参考负载场景 + 1 个通用场景
python3 -m unittest discover -s tests -p 'test_*.py'
```
