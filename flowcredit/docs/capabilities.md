# Capability Runtime（Work 第二切片）

> 状态：`motion.render` 已接入真实 Work/Console 执行链。capability 只负责阶段迁移、
> 动作门和 artifact-derived checks；模型计费、不可变授权、操作员控制、工具执行、
> 持久事件、最终 host probe 与声明验证仍由 Work Harness 唯一拥有。
> 其他 capability 仍是库级能力，不自动路由。

## 1. 为什么要独立一层

`motion.render` 是 **capability**，不是主循环里的 profile。主 agent 走的是
`user → agent → tool call → result → agent`；capability 在自己的独立上下文里
跑阶段机，返回一个 typed result。父 agent 不结算 capability 内部的 stage，
也不把内部 trace 塞进对话——内部 journal 只通过 `child_run_id` 关联。

因此这一层：

* 不创建任何任务级 checklist；不调用 `Planner.advance`，不按工具顺序结算；
* 阶段迁移只看「当前 artifact 字节的 exit checks 是否通过」；
* 失败返回 typed result（`status=failed` + `stage` + `reason`），不抛通用
  proposal-schema 错误，也不让父 agent 无限重试。

## 2. 模块

| 文件 | 职责 |
| --- | --- |
| `src/flowagent/capabilities/manifest.py` | `CapabilityManifest` / `StageSpec` / `CheckSpec` / `BudgetSpec`：纯数据 |
| `src/flowagent/capabilities/artifacts.py` | `ArtifactStore`：revision / sha256 / bytes / path，纯 scene 检测 helper |
| `src/flowagent/capabilities/checks.py` | Check 协议 + `CheckRegistry`：按 artifact hash 缓存；复用 `analyze_motion` / `merge_quality` |
| `src/flowagent/capabilities/runtime.py` | `CapabilityRuntime` + `ActionGate`：阶段循环、动作门、检查、迁移、typed result |
| `src/flowagent/capabilities/result.py` | `CapabilityResult` + `to_dict()` |
| `src/flowagent/capabilities/motion.py` | `motion.render` 的 manifest 与装配 |
| `src/flowagent/runtime/capability.py` | `HarnessCapabilityBridge`：阶段包到真实 `Harness.propose/execute`，以及最终验收 |
| `viewer/console_run_driver.py` | `motion-render` 的生产路由、回退、终态与 project typed result |

复用现有实现（未重写）：`tools/general/motion_quality.py::analyze_motion` /
`merge_quality`、`tools/general/motion_scaffold.py::ensure_motion_scaffold`、
`viewer/motion_frames.py::MotionProbe`（由宿主注入 `probe` 对象）、
以及 `WorkspaceSandbox` 的 read / edit / write / git-commit 执行路径。

## 3. Manifest 示例

```python
from flowagent.capabilities import motion_manifest

m = motion_manifest()
print([s.name for s in m.stages])
# ['draft', 'structure', 'motion', 'delivery']
```

```python
StageSpec(
    name="draft",
    allowed_tools=("edit_file",),
    exit_checks=("scene.nonempty",),
    budget=BudgetSpec(calls=2, tokens=60_000, repairs=3),
    on_fail="retry",
    action_rules=("scene_edit_only",),
)
```

`action_rules` 是声明式数据，由 `ActionGate` 解释：

| rule | 含义 |
| --- | --- |
| `scene_edit_only` | 只接受 `<!-- SECTION: scene -->` 锚点或 scene 内已有 SVG 元素；纯 CSS 编辑返回 `STAGE_ACTION_DENIED`，文件不落盘 |
| `any_edit` | scene / styles 编辑均可 |
| `motion_check_allowed` | 允许 `motion_check` 观测动作 |
| `result_write_only` | `write_file` 只能写 `FCODE_RESULT.md` |
| `motion_check_required` | 写 result 前必须有**当前 artifact hash** 的 `motion_check` pass；任何后续编辑都会改变 hash，使旧 pass 自动失效 |

四个阶段：

| 阶段 | allowed tools | exit checks | 预算（动作 / repair / tokens） |
| --- | --- | --- | --- |
| `draft` | scene 编辑 | `scene.nonempty` | 2 actions / 3 repairs / 60k |
| `structure` | scene/style 编辑 | `hooks.all && static.pass` | 4 actions / 2 repairs / 120k |
| `motion` | 编辑 + `motion_check` | `probe.pass && coverage.complete` | 4 actions / 2 repairs / 150k |
| `delivery` | edit + result 写入 + `motion_check`/verify | `motion_check && verify.pass && result.written` | 3 actions / 1 repair / 60k |

`structure` 每次编辑后自动跑静态检查（`analyze_motion`），`status=fail`
不前进；`motion` 阶段 `unverified` / coverage 不完整都不算 pass。

### 输入契约与结构化检查

阶段包的 `constraints` 是宿主提供的输入契约，不是模型提示词的一部分：

- `raw_user_prompt` 保留用户原始目标；
- `scene_descriptor` 提供主体、载具、构图与 `verify_terms`；
- `verifier_contract` 列出 identity terms、hooks、关键帧名与禁用项。

`last_check` 的 summary 现在包含 `required_fixes`、`coverage` 与每个 check 的
bounded 明细；`verify.pass` 失败会返回 `VERIFICATION_FAILED`，并把声明验证的
实际输出放进 fix，而不是笼统的 `MOTION_CHECK_REQUIRED`。delivery 阶段
`on_fail=retry`，最多 3 个动作：验证失败时可以修 `index.html`、让当前 hash
重新获得 motion pass，再重新验证；全局预算仍然最终裁决。

### 预算语义

- `calls` 是**通过 gate 并执行的阶段动作数**，包括 `motion_check` / `verify`
  这类观测动作；
- `valid_actions` 只统计真正改变 artifact hash 的动作；
- `repairs` 是被 gate 拒绝的提议（如 `EDIT_TOO_LARGE` / `WRONG_ANCHOR`）；
  被拒动作消耗 repair 和全局 tokens/cost，但不消耗 `calls`，也不直接触发阶段失败；
- 每个拒绝原因另有独立上限：`EDIT_TOO_LARGE` 最多 3 次，其它 shape/锚点错误 1 次；
- `model_calls` 按 provider dispatch 统计，包含 harness 内部重试，单独计费；
- **stage budget 是护栏，不是质量门槛**：阶段 `calls` / tokens / cost 不再单独
  把 run 判成失败。只要 artifact 仍在改善且全局预算未耗尽，runtime 会继续修。
  全局预算（如果宿主传入）仍是硬上限。

### 质量循环

runtime 现在用综合质量排序选择 best revision，而不是只看 composition：

```text
rank = (composition_ok, coverage.complete, geometry.pass, status.pass, score)
```

每个有效编辑后都会和当前 best candidate 比较。只有更好才更新 best；没有变好
则累计 `no_progress`。连续 3 个有效编辑没有超过 best candidate 时，返回
`QUALITY_STALLED`。失败时如果 best candidate 仍然比当前 artifact 好，会先恢复
best revision 再返回，避免把较差的字节交给父 agent。

### 运动学测量

`analyze_motion` 从 `.wheel` 的 keyframes 读取实际旋转角位移，而不是假定每个周期
都是 360°：`rotate(180deg)` 与 `rotate(360deg)` 会得到不同轮速；缺少可测角位移时
直接 fail，不用 360° 兜底。`rotate: ...` 属性、`from`/`to`/百分比关键帧和
`translateX` 的起止位移都按同一规则解析。`wheel_radius` 优先取 `.wheel` 组内的
circle，避免场景里更大的装饰圆改变轮速。这个测量仍是静态 keyframes 事实；真实
渲染矩阵测量是后续独立切片。

### 构图不变量与回归守卫

`probe_quality` 除了脚/腿几何，还检查 `composition`：

- `#scene` 的 bounding box 与所有 `.wheel` 的 bounding box；
- 至少两个轮子；
- 轮子以及可选的 `.bicycle` / `.pelican` 必须在 scene 内（允许 2px 贴边），
  否则返回 `WHEEL_COUNT` / `WHEEL_OFFSCREEN` / `PART_OFFSCREEN`，probe 直接 fail；
- coverage 增加 `composition`，缺失测量是 `unverified`，不是 pass。

runtime 保存最后一次 composition-safe 的 revision。后续编辑如果把轮子/主体移出
scene，会记录 `artifact_rolled_back`，恢复 best revision，并发出
`COMPOSITION_REGRESSION` repair ticket；模型必须在恢复后的 best 上继续修腿/脚，
不能靠“牺牲构图”满足局部指标。

## 4. 生命周期

```python
stage = manifest.initial_stage(artifact)          # 空 scene → draft
while stage:
    state = artifact.snapshot()                   # sha256 / bytes / content
    packet = runtime.pack_state(goal, stage, state, last_check, remaining_budget)
    action = proposer.propose(packet, tools=stage.allowed_tools)
    # Work 接入时，上面两步经 HarnessCapabilityBridge 进入真实 propose/execute；
    # 授权字段不逐轮修改，只收窄本次 provider 可见的 tool schema。
    runtime.gate.require(action, stage, state)
    revision = executor(action) if executor else artifact.apply(action)
    check = checks.run(stage.exit_checks, revision)   # 按 hash 缓存
    journal.record(stage, action, revision, check)
    if check.passed:
        stage = manifest.next(stage)
    elif budget_exhausted(stage) or no_artifact_delta >= 2:
        return CapabilityResult.failed(stage, reason)
```

* 阶段 actions/tokens/cost/repairs 预算耗尽、连续 2 次无 artifact delta、或
  proposer 用尽 → typed `failed`。`reason` 是控制面失败码：
  `NO_TOOL_CALL` / `EMPTY_CONTENT` / `INVALID_TOOL_CALL` / `ACTION_REJECTED` /
  `ARTIFACT_CHECK_FAILED` / `BUDGET_EXHAUSTED_REASONING` /
  `BUDGET_EXHAUSTED_ACTION` / `ACTION_REPAIR_EXHAUSTED_NO_VALID` /
  `ACTION_REPAIR_EXHAUSTED_PARTIAL` / `QUALITY_STALLED`。旧的 `NO_DRAFT` / `STATIC_CHECK_FAILED` /
  `PROBE_NOT_PASS` / `DELIVERY_BLOCKED` 只作为 `legacy_reason` 兼容别名。
* `model_calls` 按 provider dispatch 计，包含 harness 内部重试；`calls_used`
  保留为它的兼容别名。`valid_actions` 只统计改变 artifact hash 的动作；
  `repairs` 统计被 gate 拒绝的提议。token 与 µUSD 为整数。
* 观测类动作（`motion_check` / `verify`）不改变 artifact，`changed=False`，
  但会消耗阶段 `calls` 预算与 `model_calls`。阶段 `calls` / tokens / cost 只是
  护栏，不单独终态；质量停止条件是 no-progress，全局预算仍是硬上限。
* gate 拒绝返回有界 `repair_ticket`（code / violation / constraint /
  remaining_repairs / artifact_sha256），下一轮 packet 的 `repair` 字段携带它；
  repair 用尽才失败，模型不能靠重复同一超限动作无限重试。

## 5. Typed result

父 agent 只读 `to_dict()`，内部 journal 不进入对话：

```json
{
  "kind": "motion.render",
  "status": "succeeded",
  "stage": "delivery",
  "artifact": {"path": "index.html", "sha256": "...", "bytes": 12345},
  "quality": {
    "status": "pass",
    "score": 100,
    "coverage": {"required": ["text_invariants"], "covered": [], "complete": true}
  },
  "required_fixes": [],
  "budget": {
    "calls_used": 7,
    "model_calls": 7,
    "attempts": 7,
    "valid_actions": 5,
    "repairs": 2,
    "tokens_used": 180000,
    "cost_microusd": 50000
  },
  "reason": "",
  "child_run_id": "cap-motion.render-..."
}
```

`quality` 是有界摘要：`issues` / `required_fixes` 各截前 5 条，不含 frames、
contact sheet 或浏览器对象。所有金额/计数均为整数，`canonical_json` 可入链。

## 6. 离线运行（scripted proposer + fake probe）

```python
from flowagent.capabilities import build_motion_capability
from flowagent.tools.general.sandbox import WorkspaceSandbox

runtime = build_motion_capability(
    WorkspaceSandbox(workspace_root),
    proposer=ScriptedProposer(script),
    probe=lambda path: fake_probe_pass_dict(),   # 无需浏览器
)
result = runtime.run("draw the scene")
payload = result.to_dict()      # 交给父 agent
```

宿主接真实帧探针时，把 `probe` 换成 `viewer/motion_frames.py::MotionProbe`
实例（暴露 `probe(path) -> dict`）即可；注册表同时接受 dict / callable /
`.probe()` 对象三种形态。

## 7. Work/Console 接入与回退

`RunDriver` 仅在以下条件全部满足时选择 capability 路径：

* `fast=True`、general domain、存在 sandbox、`fastProfile=motion-render`；
* 不是 crash 或 pending-recovery 场景。

`spec.capabilityRuntime=false/"off"/"legacy"` 或 `FCODE_CAPABILITY_RUNTIME=0` 可回退旧
Loop。`GENERAL_TOOLS` 没有注册 `run_capability` 或 `motion.render`，模型不能自行启动
嵌套执行；路由仍是 host 决策。

delivery 先进入持久 `verifying` 状态，再执行最终 host `MotionProbe`（采样时间
150/450/750/1050ms），并复用 `Loop._verify_obligation()` 执行声明的验证命令。
最终 probe 或声明验证失败都不能产生 typed success。成功/失败的 bounded
`CapabilityResult` 写入事件和 project delivery，父对话不接收内部 journal。

真实模型 + 真实 Playwright + 独立验证的鹈鹕评估：

```bash
python3 scripts/evaluate_work_pelican.py --output /path/outside-viewer-runs
```

脚本读取 host 已保存的 DeepSeek 配置，默认硬限制为 10 calls / 300k tokens /
200,000 µUSD（`--max-calls` / `--max-total-tokens` / `--max-cost-microusd`
可显式覆盖），并输出脱敏 `report.json`、workspace 和 motion frames。`report.json`
同时包含 capability journal 和 provider audit，便于在失败样本里直接看到
proposer error / raw request shape，而不是只留下一个模糊的终态码。默认输出目录是
`work/evaluations/pelican-<timestamp>/`；不要把失败样本只留在易失的 `/tmp`，否则
无法在之后复核测量争议。单次在线样本只用于
回归观察，不构成统计意义上的模型对比。

## 8. 后续切片

* 通用 agent tool：`run_capability(kind, goal, constraints, budget)`，仍需 host 路由与授权；
* UI：阶段卡片、预览、coverage、失败原因，trace 退到详情；
* 将取消/拒绝后的 observer round 收尾、统一 execution id/deadline 纳入共享内核；
* 之后抽象 `code.change` / `doc.write`。
