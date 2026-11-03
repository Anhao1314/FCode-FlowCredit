# FCode 巨型模块重构评估

评估日期：2026-10-27
范围：`viewer/server.py`、`viewer/console_manager.py`、`src/flowagent/chat.py`、`viewer/app.js`。
目标：拆除巨型函数和混杂职责，同时保持 HTTP API、trace／hash chain、SSE、预算语义和 DOM 契约不变。

## 结论

四个点名的巨型函数均已拆除。`server.py`、`console_manager.py` 和 `chat.py` 的入口现在只保留协议守卫、公共生命周期或回答编排，具体执行由独立模块负责。`app.js` 的审查、证据、对话、设置和运行提案等关键视图已拆出，829 行的 `bind()` 已缩为编排函数；主入口仍有 5,903 行，继续承担实时会话、委托输入、审批、设置和全局交互，因此该项为**部分解决**，不能视为前端复杂度已经彻底治理。

| 热点 | 重构前 | 当前 | 状态 |
|---|---:|---:|---|
| `server.py` | 1,139 行；`do_POST` 第 522–1008 行，共 487 行 | 802 行；`do_POST` 第 634–645 行，共 12 行 | 已解决 |
| `console_manager.py` | 1,077 行；`_run_thread` 第 727–1074 行，共 348 行 | 743 行；`_run_thread` 第 737–740 行，共 4 行 | 已解决 |
| `chat.py` | 1,144 行；`_answer` 第 824–1035 行，共 212 行 | 945 行；`_answer` 第 828–837 行，共 10 行 | 已解决 |
| `app.js` | 7,935 行；`bind` 第 7052–7880 行，共 829 行 | 5,903 行；`bind` 第 4761–4772 行，共 12 行 | 部分解决 |

行号以本次评估的工作树为准；后续编辑需要同步更新，不应把这些行号当作长期稳定接口。

## 1. POST 路由与处理器

**状态：已解决。**

- `viewer/server.py:522-532` 的 `Handler.do_POST` 只做 API 路径检查、认证、mutation guard 和分发，不再包含各端点业务逻辑。
- `viewer/console_post.py:28-51` 用静态、对话和运行三组路由表描述 URL 到处理器的映射；`dispatch_post` 位于第 66–78 行。
- `viewer/console_post_chat.py` 负责对话发送、编辑、重生成、提案确认和 Provider 设置。
- `viewer/console_post_runs.py` 负责运行创建、审批、提问、消息、转向、队列、计划、预算、恢复和元数据。
- 认证与同源／请求体守卫仍集中在 `server.py`，没有因拆分形成多套安全策略。

**剩余风险：** 路由分发依赖路径后缀匹配，后续增加端点时应延续现有 API 回归测试，避免相似后缀或非法 ID 被误匹配。

## 2. Console 运行装配与执行阶段

**状态：已解决。**

- `viewer/console_manager.py:722-725` 的 `_run_thread` 仅创建并执行 `RunDriver`。
- `viewer/console_run_driver.py:33-48` 的 `execute` 明确分为装配、任务准备、执行、发布和清理。
- 同文件第 52、222、285、326、347 行分别承接装配、任务准备／恢复、执行、trace 发布和资源清理。
- `viewer/console_run_budget.py` 独立提供运行预算摘要。
- `RunManager` 继续拥有公开生命周期、历史、运行查询和审查记录，避免把持久化历史与单次执行上下文重新揉成一个上帝对象。

**剩余风险：** `RunDriver` 仍通过 `manager` 使用运行时配置和 Provider 构造能力，属于有意保留的协作边界；若继续演进，应优先收窄所需接口，而不是再复制一份管理器状态。

## 3. Chat 回答流水线与 usage 结算

**状态：已解决。**

- `src/flowagent/chat.py:824-832` 的 `_answer` 只委托 `AnswerStream.events()`；`send`、`edit`、`regenerate` 仍保留原公开入口。
- `src/flowagent/chat_answer.py:29-53` 定义一次回答的配置解析、预算预留、开始事件、并发槽位、流式消费、完成、异常和取消顺序。
- `src/flowagent/chat_answer.py:123-234` 分离流式消费、完成、流异常和取消收尾。
- `src/flowagent/chat_usage.py:19-53` 集中 token／成本估算、预算预留、usage 归一化和实际成本结算。

**剩余风险：** 当前回归覆盖 Mock、错误和预算路径；真实 Provider 的分块格式和联网可用性仍受外部服务影响，本次没有把所有线上 Provider 都重新做端到端验证。

## 4. 前端视图、状态控制器与事件绑定

**状态：部分解决。**

已拆出的模块：

- `viewer/app/review.js`（410 行）：审查面板、历史判断、提交和 trace 刷新。
- `viewer/app/run-evidence.js`（498 行）：事件、hash chain、工具契约、轮次、计划、预算和工具结果。
- `viewer/app/chat-ui.js`（565 行）：对话线程、消息流、编辑／重生成和模式切换。
- `viewer/app/chat-config.js`（185 行）：Provider 设置与模型目录。
- `viewer/app/chat-runs.js`（300 行）：提案卡、确认运行、运行镜像和 SSE。

`viewer/app.js:4692-4703` 的 `bind()` 现在只调用初始化与各组事件绑定；`bindComposer`、`bindGlobalActions`、`bindNavigation`、`bindEvidence`、`bindMainInteractions`、`bindShell` 分别位于第 4705、4912、5069、5189、5239、5407 行。跨模块可变基本类型通过 getter／setter 访问，避免模块捕获旧值；初始化阶段的跨函数引用采用延迟调用，避免 TDZ 问题。

**未完全解决的部分：** 主入口仍为 5,903 行，并同时承载实时会话、SSE 增量应用、委托 intake、审批、预算编辑、运行操作、设置和命令面板等职责。此次完成的是“拆除点名的巨型绑定函数和高耦合视图”，不是把整个主入口变成薄壳。后续应按实时会话、委托输入、设置／命令面板继续拆，且每次沿用现有 DOM 和 SSE 合约，不做一次性重写。

## 验证结果

| 验证 | 结果 |
|---|---|
| `python3 -m unittest discover -s tests` | 633 项通过 |
| `node --test viewer/tests/*.test.js` | 59／59 通过 |
| 新增前端模块及 `app.js` 的 `node --check` | 通过 |
| 后端相关模块 `py_compile`、`desktop/build.sh` 的 `bash -n` | 通过 |
| `git diff --check` | 通过 |
| `python3 demo.py all` 与 `python3 demo.py --list` | 8 个场景轨迹通过自检；`budget-resume`、`open-ended` 的 `failed` 是预期终态 |
| `bash desktop/build.sh` | 构建成功；健康检查 `ok=true`、`live=true`、`templates=8`，输出 `Desktop bundle verified` |
| 桌面静态资源规则 | `desktop/fcode.spec:13-24,31` 递归打包 `viewer`，包含 `viewer/app/*.js` |
| 内置浏览器冒烟 | 页面与 5 个新模块均加载成功；工作／对话切换、历史运行详情、工具证据面板、Mock 对话流、usage 和执行提案卡正常；`window.__errs` 为空 |

浏览器验证是实际页面与交互冒烟，不等同于覆盖所有浏览器、所有 Provider 和全部用户路径的完整端到端测试。Python 测试过程中出现的测试自身 `ResourceWarning` 和 connector 测试的 `BrokenPipeError` 日志没有改变测试通过结果，但也不应被解释为这些日志已经治理。

## 总体判断

本轮已经把四个巨型函数从“业务实现堆在入口中”改成明确的职责协作，并保持了现有协议与运行证据链。后端三个热点可以关闭；前端热点只能标记为“关键复杂度已拆出、主入口继续收敛”，后续应以可独立验证的模块边界继续处理，而不是用文件数量或函数行数替代行为回归。
