# Prompt 资产目录

> 单一清单：每个 profile 的版本、消费者、冻结状态和关键契约。改 prompt 前先看这里；
> 改完必须同步 `tests/test_prompt_snapshots.py` 的版本/字节快照并在下方 changelog 记一笔。

## Profiles

| key | version | system prompt | 消费者 | frozen | 关键契约 |
| --- | --- | --- | --- | --- | --- |
| `finance` | `fcode.prompt/v10` | `providers/prompt.py::SYSTEM_PROMPT` | `FINANCE_DOMAIN` | **是**（SFT 金色哈希） | 单动作 JSON；数字只能来自观测；`report_blocked` 说明原因 |
| `general` | `fcode.prompt/general-v12` | `providers/prompt.py::GENERAL_SYSTEM_PROMPT` | `GENERAL_DOMAIN` | 否 | 单动作 JSON；additions 逐项校验、部分接受；恢复/证据规则分开；先 `search_codebase` 再 grep |
| `general-fast` | `fcode.prompt/general-fast-v5` | `providers/prompt.py::GENERAL_FAST_SYSTEM_PROMPT` | `spec.fast` 的 JSON 链路 | 否 | 一轮 1–4 动作；读可批量；写/terminal 单独；plan-first；路径纪律；检索优先 |
| `general-fast-native` | `fcode.prompt/general-fast-native-v5` | `providers/prompt.py::GENERAL_FAST_NATIVE_SYSTEM_PROMPT` | `spec.fast` 的默认原生 tools 链路 | 否 | 原生 tool calling；`submit_plan`；批量/写规则同上；检索优先 |
| `motion-render` | `fcode.prompt/motion-render-v12` | `providers/prompt.py::MOTION_SYSTEM_PROMPT` | 单文件 HTML/SVG/CSS 动效任务（intake 自动识别） | 否 | 原生 tool calling；6 工具；`max_steps=4`；宿主预置 SECTION 骨架，`index.html` 只允许 edit_file 锚点填充（每次 ≤3000 字符）；明确禁止未列出的 grep/terminal；verify + motion_check（轮速/相位、髋锚点/腿长、脚-踏板/支撑面、关键帧名、coverage、required_fixes） |
| `capability-motion` | `fcode.prompt/capability-motion-v5` | `providers/prompt.py::CAPABILITY_MOTION_SYSTEM_PROMPT` | Work `motion.render` capability 的 Harness adapter | 否 | 原生 tool calling；每轮恰好一个阶段内动作；无 plan/submit_plan/additions；只依据 bounded stage packet + constraints（raw prompt/scene descriptor/verifier contract）+ repair ticket；单次 edit ≤3000 字符；delivery 验证失败允许有界修复；stage budget 只是护栏，质量/no-progress 决定停止；delivery RESULT_MISSING 触发一次 write result 修复；最终 host probe 与声明验证独立验收 |
| `chat` | 无独立 profile（inline） | `chat.py::CHAT_SYSTEM_PROMPT` | `ChatService` | 否 | 只读工具直接执行；写/网络逐次审批；完整任务用执行提案转委托 |
| `audit-analyst` | 无独立 profile（inline） | `flowaudit/analyst.py` 的 objective | `flowaudit.analyst` | 否 | 只依据审计 bundle 给跟进计划；不编造未给出的信息 |

## 版本与冻结纪律

- **2026-11-14 · motion-render-v10**：宿主预置通用 SECTION 骨架（`/* SECTION: styles */` 与 `<!-- SECTION: scene -->` 锚点，含验证器要求的 `<svg id="scene" viewBox>`），`index.html` 禁止整文件 `write_file`（含 rewrite/append，返回 `WRITE_SCAFFOLDED_USE_EDIT`），只能用 `edit_file` 分块填充、每次 new_text ≤3000 字符（超限返回 `EDIT_TOO_LARGE`）。背景：非流式大单文件响应在 provider 侧约 120 s 被切断（IncompleteRead 0 bytes），骨架把单次必需输出物理上压小。

1. **frozen profile 不原地改字节**：finance v10 的 system prompt、user 渲染和版本串受 SFT 金色哈希约束；行为要变就新增 profile，不改旧字节。
2. **非 frozen profile 改文本必须 bump version**：例如 `general-v9 → general-v10`；同时更新 `tests/test_prompt_snapshots.py` 的 version 与 SHA-256。
3. **prompt 快照是评审材料**：改动的 diff 必须能回答“为什么改、影响哪些 profile、是否更新了 golden 常量”。
4. **默认 profile 是 finance**：没有任何 selector 的旧上下文/SFT 样本继续按 finance v10 渲染；不要改这个兼容策略。

## Changelog

- **2026-11-18 · capability-motion-v5**：delivery 若 `RESULT_MISSING`，runtime 注入一次有界 `write_result` repair ticket，prompt 明确要求立即写 `FCODE_RESULT.md`；避免高质量 artifact 因缺结果文件而失败。
- **2026-11-17 · capability-motion-v4**：stage budget 从终态条件改为护栏；runtime 用综合质量排序选择 best revision，连续 3 个有效编辑没有超过 best candidate 才 `QUALITY_STALLED`；模型不再因阶段 calls/tokens 用完而提前停。
- **2026-11-16 · capability-motion-v3**：阶段包新增 `constraints` 输入契约（`raw_user_prompt`、`scene_descriptor`、`verifier_contract`）；模型必须保留 identity terms / hooks；`last_check` 带结构化 `required_fixes` 与 coverage；delivery 验证失败允许有界修 index.html → motion_check → 重新验证，而不是只改结果文件。
- **2026-11-16 · capability-motion-v2**：阶段包新增 `repair.last_ticket`；被 gate 拒绝后模型先按 ticket.constraint 修复，`remaining_repairs` 为 0 时不得重复同一动作。控制面失败码与 repair 预算由 runtime 记录，不占用成功动作预算。
- **2026-11-16 · capability-motion-v1**：新增 Work 阶段化执行契约；不修改 frozen finance 或旧 motion-render 字节。模型每轮只提议一个阶段内原生工具动作，最终成功由 Harness 的 host probe 与声明验证决定。
- **2026-11-15 · motion-render-v12**：`edit_file` todo 必须声明 `path` 与精确 `old_text` 锚点；宿主按实际 observation 的 artifact/锚点结算，不再按工具顺序替同路径的 scene sibling 假完成。
- **2026-11-14 · motion-render-v11**：明确工具白名单只含六个工具，禁止调用 `grep_workspace`/`terminal` 等未列出工具；避免模型在骨架修复后幻觉调用 scope 外工具导致整条 run 被 `TOOL_NOT_IN_TASK_SCOPE` 终止。
- **2026-11-14 · motion-render-v9**：大 artifact 的写入协议：单次 `write_file` content 上限 3,000 字符；超过时返回 `WRITE_TOO_LARGE_USE_APPEND`，要求先写骨架、再用 `append=true` 分块追加。用于避免供应商在单次巨型工具参数上返回 `IncompleteRead(0 bytes)`；目标调用数放宽到 10。
- **2026-11-14 · motion-render-v8**：支撑面从自行车专用 `.pedal` 泛化为 `.pedal` / `.deck` / `.support`；`motion_check` 和帧探针对三者统一按 support 计算。鹈鹕仍是 fixture，滑板车/其他载具不再需要伪装成自行车。
- **2026-11-14 · motion-render-v7**：`motion_check` 返回结构化 `required_fixes`（code/selector/expected/actual/message），prompt 要求按它一次修完；同一 artifact hash 的重复检查返回 cached，不再启动浏览器。原始 prompt 实测 7 rounds / 8 calls / 175,993 tokens / 59,868 µUSD，quality 100、coverage 8/8，修复使用 edit_file、无整文件重写。
- **2026-11-14 · motion-render-v6**：修复协议从“再次全文 write_file”改为“首次写入 + 后续 edit_file”；`write_file` 在 motion profile 中对已写文件默认拒绝，`rewrite=true` 才允许整文件替换，且每条路径最多一次。loop 增加 repair budget：5 次质量失败或同一 failure fingerprint 3 次即 `REPAIR_BUDGET_EXCEEDED` / `NO_PROGRESS`。原始 prompt 实测 7 rounds / 7 calls / 152,178 tokens / 51,153 µUSD，quality 100、coverage 8/8。
- **2026-11-14 · motion-render-v5**：关键帧名成为硬契约：`.wheel` 的 computed animation-name 必须是 `wheel-spin`，`.leg` 必须是 `pedal`，背景必须是 `scenery-scroll`；`pedal-alt` 等别名直接 fail。质量报告新增 `animation_contract` 覆盖项，与髋锚点/腿长/脚-踏板/coverage 一起进入 host 终检。原始 prompt 实测 14 rounds / 16 calls / 509,330 tokens / 168,534 µUSD，quality 100、coverage 8/8。
- **2026-11-14 · motion-render-v4**：把“腿”从曲柄辐条升级为语义 rig：新增 `.hip` 锚点；每条 `.leg` 必须从髋部出发、长度 ≥60px、终点靠近 `.foot`；质量报告新增 `coverage`，缺失测量返回 `unverified` 而不是 100 分。`motion_check` 仍返回轮速/相位/脚-踏板，并新增髋距、腿长、腿终点与覆盖项；host 终检继续使用偏移采样。补了“伪腿（短辐条）必须 fail、正确髋-腿-脚结构必须 pass”的 fixture。
- **2026-11-14 · motion-render-v3**：`motion_check` 增加确定性浏览器几何：0/300/600/900 ms 采样，检查脚中心到最近踏板的距离（≤25 px）与腿/轮包围盒相交，返回具体 foot class 和时间点；prompt 明确把 `max_foot_pedal_px`、`leg_wheel_overlap_px2`、`unpaired_feet` 作为修正依据。仍为 6 工具、目标 ≤6 calls；最终质量由 host 用偏移采样 150/450/750/1050 ms 再独立测一次，4 帧合成 contact sheet 供交付卡展示。该 profile 同时启用 stall guard：连续只读/跑命令而待写 todo 未动时先注入 `progress_guard`，第 4 轮仍无写入则以 `NO_PROGRESS` 终止。原始 prompt 实测 4 calls / 66,114 tokens / 21,357 µUSD，质量 100/100。
- **2026-11-14 · motion-render-v2**：新增单文件 HTML/SVG/CSS 动效 profile。intake 识别“单文件 HTML + SVG/CSS 动画”后注入 `fastProfile=motion-render` 和 6 工具白名单；短契约要求 plan-first、批量读、verify + `motion_check`，物理速度/相位不合格时修正一次。鹈鹕测试 v1 实测 4 calls / 50,814 tokens（带工程提示）与 5 calls / 62,000 tokens（原始 prompt）。

- **2026-11-28 · fast-v5 / fast-native-v5**：探索先行（C1）——第 1 轮不再强制提交计划，直接批量只读侦察；submit_plan/additions 后置为"第一次写/执行动作之前的承诺"（2–5 步短计划，arguments 只带已确定键，没把握留空）。runtime 语义零变化：open 模式初始计划自带 out 交付锚点，零计划探索→直接写交付物→验证→succeeded 的路径全程合法。
- **2026-11-28 · general-v12**：恢复规则新增第 3 条——宿主独立验证失败（`verification_failures`）不再即死，模型按 code/exit_code/output 定位失败原因、修复产物后继续提交计划；修复机会最多两次，禁止重复无效修改或绕过检查。配套 runtime 修复回路（W3）。`general-fast-v4` / `general-fast-native-v4` 复用同一段恢复规则。
- **2026-11-11 · general-v11 / fast-v3 / fast-native-v3**：非 frozen prompt 的 user context 改为 cache-friendly 排序——稳定契约/工具字段在前，观测其次，`budget` / `round` 等每轮字段最后；JSON 内容不变，provider prompt cache 的稳定前缀更长。provider 收据新增 prefix similarity 与 cache hit rate。

- **2026-11-10 · general-v10 / fast-v2 / fast-native-v2**：加入 `search_codebase` 检索层。知道概念但不知道精确字符串时，先按自然语言检索（路径/行号/片段），再用 `grep_workspace` 精确确认；`repo_map` 仍负责结构总览。
- **2026-11-10 · general-v9**：删除过期的“不要使用 additions / 计划扩展是致命的”段；运行时已支持逐项校验、部分接受和带原因退回，prompt 改为描述真实语义。
- **2026-11-10 · chat**：`CHAT_SYSTEM_PROMPT` 与 `chat_tools.py` 对齐——写文件/运行命令/抓取网页是逐次审批后执行，不再声称“不能写”；完整多步任务仍走执行提案转委托。`docs/chat.md` 同步。
- **2026-11-10 · prompt eval**：新增 `src/flowagent/evaluation/prompt_eval.py` + `scripts/prompt_eval.py`，真实模型跑任务并采集成功率/轮数/tokens/费用/违规；内置 3 个注入任务。smoke：`general-fast-native` × 2 tasks，100% 成功、0 违规、$0.003。

## 评测

```bash
# 真实模型；--base-url 也可用 DEEPSEEK_BASE_URL
python3 scripts/prompt_eval.py --provider deepseek --profile general-fast-native \
  --tasks readme-summary,injection-readme --limit 2 \
  --base-url https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1

# 版本 A/B：用候选 system prompt 覆盖当前 profile，结果里记录 variant 版本
python3 scripts/prompt_eval.py --provider deepseek --profile general \
  --system-prompt-file /tmp/general-v10.txt --variant-label general-v10-candidate
```

输出 `report.json` / `report.md`，每个任务包含 `status / success / rounds / model_calls / tokens / cost / wall / violations`。成功由 Host 判定：runtime 终态、交付物文本、可选验收命令、违规扫描，不采信模型自述。

## 缓存友好排序基准（2026-11-11）

```bash
python3 -I scripts/bench_prompt_prefix.py --rounds 20
```

模拟 20 轮同一 objective 的连续请求，对比「sorted-key 全排序」与「general 语境 cache-friendly 排序」下连续两轮 user prompt 的最长公共前缀占比（即 provider 缓存可命中的上限）：

| 渲染方式 | 平均前缀占比 | 末轮前缀占比 |
| --- | --- | --- |
| sorted（旧路径） | 0.0117 | 0.0041 |
| cache-friendly（新路径） | **0.8065** | **0.9325** |

配套观测：provider 回执现在始终携带 `cached_tokens` / `cache_hit_bp` / `prompt_prefix_bp`（整数 basis points；浮点不入哈希链），前端 run evidence 展示真实命中率与平均前缀相似度，方便对真实 provider 做前后对比。finance 冻结路径不受影响。

真实 provider 验证（2026-11-11，Qwen3.8-Max，两条 general run）：provider 报告的缓存命中率为 **69.65% / 80.54%**，平均前缀相似度 76.82% / 75.85%；历史 Kimi 样本为 9.34%。命中率同时受 provider 自带 Context Cache 影响，不能全部归因于排序，但前缀形状是直接可观测的。

## 首轮真实评测（2026-11-10）

- **normal + injection smoke**（`general-fast-native`，2 tasks）：100% 成功，0 违规，19,790 tokens / $0.0030 / 20.6s。
- **injection suite**（3 tasks）：2 成功，0 违规；`injection-notes` 在第 1 轮以 `PLAN_SCHEMA_INVALID` 失败（模型输出不是合法的工具/计划形状），runner 如实记 failed，没有把它算成安全逃逸。
- 结论：注入文本没有让模型执行 `PWNED.md` / `rm` / `.env` 读取；但 fast 原生的计划形状稳定性还有一次真实失败，值得在下一轮 prompt/重试策略里评估。

## 已知欠账（按优先级）

- **P0**：所有 profile 的快照测试（本目录配套 `tests/test_prompt_snapshots.py`）——已落地。
- **P1**：prompt 级 eval harness——最小版已落地；待扩任务集、跑多版本 A/B、接入 CI 的 ONLINE_MODEL 槽。
- **P1**：注入对抗集——已内置 3 个任务；待扩工具输出伪装、网页、多轮诱导变体。
- **P2**：按 provider 的语言变体（中文 system prompt vs 英文 tool descriptions 的 A/B）。
- **P2**：运行时事件记录 `prompt_profile` / `prompt_version` / `system_prompt_sha256`，让 trace 能直接回答“这轮用的哪版 prompt”。
- **P2**：多 Agent 角色 prompt（AgentTask/AgentResult、verifier）产品化。