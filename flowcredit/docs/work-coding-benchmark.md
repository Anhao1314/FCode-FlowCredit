# Work Coding Benchmark（W0）

> 状态：W0 骨架。本文描述 `work-coding-benchmark/v1` 的任务集、运行方式、
> 隐藏测试边界和指标分母。它和 `general-agent-benchmark/v1` 分开报告：
> 后者证明通用任务/评测器自洽，本集专门测 **Work coding loop**。
> 最后核对：2026-11-28。

## 1. 它回答什么问题

`work-coding-benchmark/v1` 回答：

> Work Agent 在一个受治理沙箱里，能不能把一个真实 coding 任务修到隐藏测试通过？

它不是 Claude Code 对比榜，也不是模型通用智商分数。它测的是 FCode 自己的可改进面：

- 任务定位：能否从初始仓库找到相关文件；
- 最小修改：能否做局部 edit，而不是重写测试或整仓库；
- 验证闭环：能否运行公共检查并让隐藏测试通过；
- 禁区纪律：是否修改 tests/ 或越出工作区；
- 失败语义：plan/tool/verifier 失败能否结构化落盘。

## 2. 运行

```bash
cd flowcredit
PYTHONPATH=src:viewer python -m flowagent.evaluation --suite coding --list
PYTHONPATH=src:viewer python -m flowagent.evaluation --suite coding
PYTHONPATH=src:viewer python -m flowagent.evaluation --suite coding \
  --task single-file-off-by-one --task multi-file-student-rate
PYTHONPATH=src:viewer python -m unittest tests.test_work_coding_benchmark -q
```

默认输出：

```text
work/work-coding-benchmark/latest
```

每次运行替换目标目录；每个任务有独立 workspace、canary、SQLite 事件链、
patch、verifier.json、budget.json 和 result.json。

## 3. 执行类别

W0 只有 `OFFLINE_SCRIPTED`：

- 每个任务自带参考修复动作；
- `ScriptedProvider` 经生产 Loop / Harness / registry / budget / receipt 驱动；
- 参考修复必须通过宿主持有的隐藏测试；
- `ONLINE_MODEL=NOT_RUN`；
- 真实模型通过率固定报告为 `NOT_MEASURED`。

在线/真实模型运行属于后续阶段；必须等操作员显式授权或有本地模型。  
不要把 scripted self-check 解读成模型 coding 成功率。

## 4. 任务集（12 项）

| 类别 | 数量 | 覆盖 |
| --- | ---: | --- |
| `bug_single_file` | 3 | off-by-one、除零、clamp 参数顺序 |
| `feature_multi_file` | 2 | 配置常量 + 调用点、数据结构 + CLI |
| `regression` | 2 | 空集合类型约定、JS 缓存失效 |
| `refactor` | 2 | 抽取重复税计算、抽取权限判断 |
| `frontend` | 2 | 计数器上限、email 校验边界 |
| `test_repair` | 1 | 修复实现组织，禁止改测试 |

每个任务都有：

- 初始 fixture，含至少一个会失败的公共检查；
- 明确 objective；
- 参考修复动作；
- 禁止修改 `tests/`；
- `only_workspace` 与 `no_new_dependencies`；
- host-held hidden test；
- task/hidden SHA-256；
- rationale。

## 5. 隐藏测试边界

隐藏测试是本集的关键契约：

- hidden test bytes 只存在于 `coding_tasks.py`；
- `coding_taskset()` 返回 public definition，会 strip `hidden_test.source`；
- 导出的 `taskset.json` 只含 hidden test 的 SHA-256 和字节数，不含源码；
- evaluator 把 hidden test 写到 `<run>/hidden/test.py` 或 `<run>/hidden/test.cjs`；
- 该路径在工作区外，模型不可见；
- Python 运行时把 workspace 加进 `PYTHONPATH`；
- JS 隐藏测试用 CommonJS（`.cjs`），通过 `WORKSPACE` require workspace 文件；
- 超时、非零退出、缺 runner 都按失败记录，不重试、不伪造通过。

## 6. Verifier

`verify_coding_task` 先跑通用 `verify_general_task`：

- runtime 终态；
- workspace 文件包含/不包含；
- canary；
- forbidden action scan；
- 事件链；
- 重复/越权副作用计数。

然后追加 coding 专用检查：

| 检查 | 含义 |
| --- | --- |
| `hidden_tests` | 隐藏测试进程 exit code 为 0 |
| `changed_files` | 从 initial/final snapshot 计算的真实变化 |
| `patch_minimal` | changed files 是否落在参考修复触达路径内（报告项，不单独判成功） |
| `patch` | unified diff，落盘供人工复核 |

所有 checks 全部通过才算 verifier passed。模型自述、`REPORT.md` 里的文字、
runtime status 都不能替代 hidden tests。

## 7. 指标分母

复用 `evaluation/metrics.py` 的显式分母口径，并新增：

| 指标 | 分母 |
| --- | --- |
| `goal_success_rate` | 全部 run |
| `independent_verifier_pass_rate` | 全部 run |
| `hidden_test_pass_rate` | 全部 run |
| `patch_minimal_rate` | 全部 run |
| `invalid_action_rate` | 发生 forbidden/被拒动作的 run 占比 |
| `human_intervention_count` | 绝对计数 |
| `unauthorized_side_effect_count` / `duplicate_side_effect_count` | 绝对计数 |
| calls / reads / tokens / cost | 合计 |
| `latency_ms.p50/p95` | 有事件时间戳的 run |

`--repeat N` 保留每次原始 run；单次 scripted self-check 不构成统计结论。

## 8. 当前 W0 基线

Scripted reference 的职责是证明评测器自洽，不是模型能力：

```text
runs: 12
visible/case expectations: 12/12
hidden tests passed: 12/12
online model: NOT_RUN
真实模型通过率: NOT_MEASURED
```

## 9. 后续阶段

| 阶段 | 目标 |
| --- | --- |
| W1 | Coding Context Pack：候选文件、符号、相关测试、约定、验证命令 |
| W2 | Task Contract：deliverables / constraints / forbidden paths / definition of done |
| W3 | Repair Loop：解析测试失败，生成结构化 repair ticket |
| W4 | `code.change` capability：阶段化 coding 能力壳 |
| W5 | Data flywheel：真实 run 失败样本回流 benchmark |

## 10. 非目标

- 不做通用 leaderboard；
- 不比较不同厂商模型；
- 不使用付费在线模型；
- 不把 scripted self-check 当成模型成功率；
- 不允许模型改测试来让 verifier 通过；
- 不把 hidden test bytes 写入 workspace、prompt 或导出任务集。
