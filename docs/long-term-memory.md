# L3 长期记忆的三层细分设计

> 状态:**v1 设计定稿(2026-11-02),落地顺序见文末**。
> 上层文档:[memory-design.md](memory-design.md) §三。总原则不变:
> **L3 只收验证过的结论——模型说的不算,重放过的才算。**

## 〇、为什么细分:三种"记住"回答三个不同的问题

| 子层 | 认知原型 | 回答的问题 | 审计产品里的形态 |
| --- | --- | --- | --- |
| 语义记忆 | "知道什么" | 关于这个目标,我们**知道哪些事实**? | 带引证的发现与目标事实 |
| 情景记忆 | "经历过什么" | 我们**什么时候、以什么方式**审计过它?结果如何? | 一次审计 = 一个情景(episode),含回归状态 |
| 程序性记忆 | "会怎么做" | 对**这类**目标,我们学会了**怎么审**? | 从情景蒸馏出的审计配方(recipe) |

三者写入纪律不同(见 §三),混在一起就会互相污染:事实失去引证、
情景失去时间线、配方失去统计。

## 一、情景记忆(episode)——回归记忆的载体

**记录形态**(`episode`):audit_id、target(identity + manifest_sha256)、
时间窗、profile、结果统计(发现按严重度/候选数/覆盖面)、chain_head、
bundle 引用、参与者。

**写入**:会话收尾时由宿主**自动投影**——bundle → CaseIndex 适配器
(`upsert(trace-like, run_id=audit_id)`),零模型判断。情景是"发生过",
不需要谁批准。

**回归状态机**(同靶多情景的核心价值,新会话启动时计算):

```
本情景发现 ∩ 上情景发现 = known      (已知,未修复)
本情景发现 − 上情景发现 = new        (新缺陷)
上情景发现 − 本情景发现 = fixed      (已修复)
上情景 fixed,本情景又出现 = regressed(修复被回退——最危险的信号)
```

**查询**:`episodes_for(target_hash)` 时间线;`recall(query)` 全文检索
(CaseIndex.search 现成)。

## 二、语义记忆(fact)——带引证的目标知识

**记录形态**(`fact`):{subject: target_hash, predicate, object,
citation: {bundle, poc_id, evidence_hash}, as_of}。谓词示例:
`has-finding`(object = AGI 类别@path:line)、`uses-sidecar`、
`exposes-endpoint`。

**写入纪律(严格于情景)**:只有**重放通过并由宿主晋升**的发现才能
成为事实,引证必须指向 poc-evidence;operator 确认的陈述可以入册但
必须带 `actor=operator` 标记。模型的说法永远不是事实。

**存储**:`FactStore` 适配器(`upsert_run(run_id=audit_id, facts)`,
`query(subject=target_hash, predicate=…)`,`history` 天然给出事实的
变迁史)。秘密内容不进语义记忆——和报告同一纪律,只存指针。

## 三、程序性记忆(recipe)——从经验蒸馏的审计配方

**记录形态**(`recipe`):{recipe_id, version, target_profile,
steps: [探针/检查单引用的有序列表], stats: {uses, hits, false_positives},
origin: [episode_id,…], status: draft | active}。

**写入纪律(三层里最严)**:配方由情景**蒸馏**而来——analyst 可以起草
("这三次审计都是先查信任链再查注入,建议固化为配方"),但配方进
`active` 必须 **host 评审**:程序性记忆决定"以后怎么做",错了会系统性地
重复错。草稿与生效分开,统计(命中/误报)持续回流。

**与 L4 的边界**:L3-程序性是**经验长出来的**(可变、带统计、个人化);
L4 是**评审固化的**(git、版本化、社区化)。配方被反复验证后,经人审
"毕业"进 L4——这是两层之间唯一的通道,方向单向。

**存储**:v1 用 JSON(`work/recipes/recipes.json`,追加 + 版本号)——
配方量小、需要统计字段,暂不值得独立 sqlite;若配方多引擎化,再升级。

## 四、层间流动

```
L2 会话收尾 ──自动投影(bundle→episode)──────────→ 情景
已晋升发现 ──引证化(finding→fact)────────────────→ 语义
情景累积 ──analyst 起草 + host 评审(蒸馏)──────→ 程序性(recipe)
程序性反复验证 ──毕业(人审)─────────────────────→ L4 知识库
新会话启动 ←── 时间线 + 目标事实 + 匹配配方 ────── 三层联合查询
```

## 五、落地顺序与验收

| 步 | 内容 | 验收 |
| --- | --- | --- |
| 1 | 情景适配器:bundle→CaseIndex + 回归状态机 | 同靶二次审计:第二次会话启动时状态视图含 fixed/new/known 分类,regressed 可检出 |
| 2 | 语义适配器:晋升发现→FactStore(带引证) | query(subject=target) 返回全部已确认缺陷,每条带 poc 引用;无引证写入被拒 |
| 3 | 程序性:配方存储 + 蒸馏起草 + host 晋升 | draft 不参与会话装载;active 配方被新会话按 profile 匹配加载 |
| 4 | 联合装载:新会话启动注入 时间线+事实+配方 | 三层查询进入 state 视图(L1 可见),secret 内容零出现 |

## 六、隐私与安全纪律(继承并收紧)

- 秘密内容零存储:与报告同一纪律,只存指针与哈希;
- 情景与语义都带 target_hash——**长期记忆本身是敏感资产**(它描述了
  谁的系统的什么弱点),导出与同步必须显式授权;
- 蒸馏(程序性)由 analyst 起草时,草稿同样走 L1 隔离墙——靶子内容
  不能借"配方"渠道污染知识层。
