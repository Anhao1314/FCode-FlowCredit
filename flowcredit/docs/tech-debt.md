# 技术债记录

> 每条记录包含:现状、影响范围、现有数据未触发的证据、后续解法候选。
> 记录只描述事实与选项,不含未决实现。

## TD-001:finance profile 的 trust 盲区(P0-2 遗留)

**现状**:`trust` 字段按 prompt_profile 门控输出(`memory/context.py` build 的
`include_trust = self.prompt_profile is not None`);finance 是缺省 profile
(`providers/prompt.py:180` `DEFAULT_PROMPT_PROFILE = FINANCE_PROMPT_PROFILE`;
`domains/finance.py:74-75` 的 `FINANCE_DOMAIN` 不声明 prompt_profile,
`domains/__init__.py:34-35` 明文允许只有它缺席)。因此 finance 运行的所有观测在
prompt 中不带来源可信度标记。

**影响范围**:finance 场景的允许工具清单(`demo/fixtures.py:52-70` `_ALL_TOOLS`)
包含 9 个未在 `tools/trust.py` 登记的 workspace 工具——含网络抓取
`fetch_source`/`collect_source` 与 `read_slice`/`search_corpus` 等读取类。按
`tools/trust.py:29-31` 的 fail-closed 缺省,这些工具的观测会被判定为
`EXTERNAL_UNTRUSTED`,但在 finance profile 的 prompt 中该标记不可见。若未来有人
把 finance 场景真正用于外部内容分析,不可信文本将以"无来源标记"的形态进入模型
上下文——这是唯一的残余盲区路径。

**现有数据未触发的证据**:扫描 `work/demo/*.trace.json`(8 场景)与
`work/runtime-benchmark-test-*`、`artifacts/runtime-benchmark`(共 300 个 JSON)
的 `tool_succeeded` 事件:trust 分布为 `verified_tool` ×46、`external_untrusted` ×5、
缺字段 ×15(缺字段全部来自改动前基准运行)。**finance 的 6 个场景轨迹 100% 为
`verified_tool`**;`external_untrusted` 全部出现在 general-research 场景
(edit_file/run_command/write_file),而该场景使用 general profile,trust 正常输出。

**MCP 路径补查结论(Q2/Q3)**:
- **Q2 判定:否——MCP 工具不会出现在 finance profile 的运行中。** 证据:全仓唯一
  的 MCP 注册表合并点是 `viewer/console_run_driver.py:74-76`,其前置门控在
  `console_run_driver.py:59/72`:`self.general = self.module.key != DOMAIN_KEY`
  (`DOMAIN_KEY = "finance-research"`,`viewer/console_common.py:27`),非 general
  域时 `self.mcp_bridge = None`,MCP specs 不进入 `ToolRegistry`。finance 场景的
  module 均为 `finance-research`(`demo/fixtures.py:100/114/127/140`)。触发条件
  (供未来审阅):如果该门控被解除,finance 模板 + 已配置 `mcp.json` + 非 mock 模型
  的组合即可让 MCP 工具进入 finance 运行。
- **Q3:若 Q2 为"是"**,MCP 工具名(`mcp__{server}__{tool}`,构造于
  `tools/mcp/bridge.py:176-190`)不在 `tools/trust.py:19-25` 的 `_VERIFIED_TOOLS`
  中,会被 `trust_for_tool` 映射为 `EXTERNAL_UNTRUSTED`(`tools/trust.py:29-31`);
  当前因 Q2 不可达,该映射暂不影响 finance 上下文。

**后续解法候选(未选型)**:
1. 以字节安全方式扩展 profile 机制,让 finance 也输出 trust(需先评估 SFT 金色哈希
   依赖;`prompt_profile` 缺席语义是冻结契约)。
2. 在 finance 场景层收紧 `allowed_tools` 至已登记工具集(移除 fetch_source/
   collect_source 等未登记项),从源头消除不可信来源。
3. 在工具注册层引入 fail-closed 校验:"未在信任表登记的工具禁止进入 finance 运行",
   让缺口在注册时暴露而不是在 prompt 里沉默。


## TD-002:SQLite summary 缓存介质净负(P1-1 收尾结论)

**现状**:P1-1 的 `SummaryCache`(五元组 key、与事件链同库不同表、fail-open)
按数据判定为**净负优化**。`runtime/loop.py` 的 `ENABLE_SQLITE_SUMMARY_CACHE`
默认 `False`,缓存代码与测试作为显式可用的资产保留。

**数据**(`work/cache-benchmark/large-payload-report.json`,本机 macOS arm64):
- 90.6KB 大载荷、50 轮:无缓存均值 0.272ms vs 冷缓存 0.378ms(慢 39%);
- 单次 `detailed_summarize` 3.65µs vs 单次 SQLite 命中点查 4.71µs。

**根因**:本仓库 summarizer 按设计早剪枝(dict 列表→首项+count、长文→头尾),
成本几乎不随载荷增长;SQLite 点查为固定成本且高于 summarize 本身——**不存在
任何载荷大小能让缓存转正**,阈值门控的数学前提不成立。

**后续(P1-2 已实施)**:进程内 dict memo 成为首选介质(键与 SQLite 版同为
五元组),顺序 `memo → SQLite(默认关)→ live`;同时 `WorkingMemory.build`
改为行构造增量化,历史行不再逐轮重序列化。同机重测(2026-11-02):

- 50 轮小载荷(read_target_slice + generic,budget 24000):
  P1-2 前无缓存均值 0.133ms / R50 0.249ms(随轮次线性增长);
  SQLite 版均值 0.130ms / R50 0.273ms;P1-2 均值 0.013ms / R50 0.011ms(平坦);
- 90.6KB 大载荷:无缓存均值 0.272ms → 0.019ms;SQLite 冷缓存 0.087ms
  (每轮 result_hash 不同,命中率 0% → 仍净负,维持默认关闭)。

**残留债务**:dict memo 仅存活于单个 `WorkingMemory` 实例,不跨进程;因
summarizer 是五元组纯函数,跨进程复用暂无收益证据,接受现状。若未来某个
工具的 summarizer 变成昂贵操作(如调用模型),需重新评估持久缓存。
