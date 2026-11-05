# DeepSeek API 接入（API-first LLM 层）

DeepSeek 是当前首先启用的外部 LLM 层：不做本地模型推理，先把 API 调用、
预算、审计和验收打通。DeepSeek 走 `providers/deepseek.py`，必须遵守 FCode
的外部调用纪律：密钥只从环境变量读、价格快照由 Host 提供、调用前按上界预留
预算、超时/断连不重试、失败不回退 mock。

## 一、两种 API 模式

| 模式 | 类 | 输出 | 定位 |
| --- | --- | --- | --- |
| Packet 选择 | `DeepSeekPolicy` | `{"choice":"aN"}` | **默认 API 模式**；Host 编译候选，模型只选择 |
| 完整提案 | `DeepSeekProvider` | `flowagent.plan/v1` JSON | 付费对照实验，不是默认路径 |

两种模式都只提议：Host 仍然 gate、预算、执行、复算；DeepSeek 不执行工具，
也不能把任何数字写进结果。

## 二、Host 配置

统一入口会先读显式 CLI 参数，再读环境变量，最后使用安全默认值。价格和价格
日期没有默认值：猜一个过期价格会把预算上限变成虚假承诺。

| 环境变量 | 作用 | 默认 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | API key；只进进程内存，不写事件/日志/报告 | 必填 |
| `DEEPSEEK_BASE_URL` | OpenAI 兼容 base URL；必须 `https://` | `https://api.deepseek.com/v1` |
| `DEEPSEEK_MODEL` | 模型名 | `deepseek-chat` |
| `DEEPSEEK_INPUT_PRICE_MICROUSD_PER_MTOK` | 输入价格，µUSD / 1M tokens | 必填 |
| `DEEPSEEK_OUTPUT_PRICE_MICROUSD_PER_MTOK` | 输出价格，µUSD / 1M tokens | 必填 |
| `DEEPSEEK_CACHE_HIT_PRICE_MICROUSD_PER_MTOK` | 缓存命中输入价格 | 可选 |
| `DEEPSEEK_PRICE_AS_OF` | Host 读取价格的日期 | 必填 |
| `DEEPSEEK_PRICE_SOURCE` | 价格来源说明 | `host` |
| `DEEPSEEK_TIMEOUT_SECONDS` | 传输超时 | `120` |
| `DEEPSEEK_MAX_TOKENS` | 每次调用输出 token 上限 | packet `64`，proposal `256` |

价格单位是 **µUSD / 1M tokens**。例如输入 0.27 美元/百万、输出 1.10
美元/百万，写成 `270000` / `1100000`（1 美元 = 1,000,000 µUSD）。请按自己
账单页当天的数字填写，不要照抄示例。

```bash
export DEEPSEEK_API_KEY=...        # 不要写进仓库，不要发给任何人
export DEEPSEEK_INPUT_PRICE_MICROUSD_PER_MTOK=<输入价>
export DEEPSEEK_OUTPUT_PRICE_MICROUSD_PER_MTOK=<输出价>
export DEEPSEEK_PRICE_AS_OF=<读取价格日期>
```

## 三、统一入口：`flowagent.demo.deepseek`

### 1. 预检：不联网、不花钱

```bash
cd flowcredit
PYTHONPATH=src python3.12 -m flowagent.demo.deepseek \
  --workdir /tmp/fcode-ds-preflight \
  --preflight-only
```

预检只做：

- key 所指环境变量存在；
- 输入/输出价格和价格日期存在；
- `PriceSnapshot` 合法；
- base URL 是 HTTPS；
- model / timeout / max tokens 合法；
- 计算配置上界的预留 token 与成本。

输出中的 key 永远是脱敏形式：

```json
{
  "ok": true,
  "preflight": true,
  "networkCalled": false,
  "config": {
    "provider": "deepseek",
    "apiKey": "env:DEEPSEEK_API_KEY",
    "baseURL": "https://api.deepseek.com/v1",
    "model": "deepseek-chat"
  },
  "reservation": {
    "basis": "configured-upper-bound",
    "tokens": 4160,
    "costMicrousd": 1176
  }
}
```

`reservation` 是配置上界，不是实际费用。实际调用前，`DeepSeekPolicy.reserve()`
会按当轮渲染出的 packet 再算一次更贴近的上界。

### 2. Packet 主验收

```bash
PYTHONPATH=src python3.12 -m flowagent.demo.deepseek \
  --workdir /tmp/fcode-ds-packet-full \
  --mode packet \
  --scenario full \
  --fresh
```

默认 mode 就是 `packet`。通过条件：

- 状态 `succeeded`；
- trace verified；
- `model_calls + auto_actions` 覆盖全部决策轮；
- 无 repair、无 fallback；
- receipt metadata 带 request id、usage 估算和价格快照。

### 3. 诚实阻断验收

```bash
PYTHONPATH=src python3.12 -m flowagent.demo.deepseek \
  --workdir /tmp/fcode-ds-packet-open \
  --mode packet \
  --scenario open-ended \
  --fresh
```

`open-ended` 的预期终态是：

```text
failed / INSUFFICIENT_EVIDENCE
```

这不是失败验收：只要达到预期终态且 trace verified，CLI exit code 就是 0。
FCode 把“算不出就诚实停下”视为正确行为，不要求模型编一个数字。

### 4. 完整提案对照（可选、更贵）

```bash
PYTHONPATH=src python3.12 -m flowagent.demo.deepseek \
  --workdir /tmp/fcode-ds-proposal-full \
  --mode proposal \
  --scenario full \
  --fresh
```

proposal 模式输出完整 `flowagent.plan/v1`，上下文和输出都更大，仅用于与
packet 模式对照，不作为默认 API 路径。

### 5. 预算 override

所有模式都支持 Host 显式放宽预算：

```bash
--max-calls 16 \
--max-reads 20 \
--max-cost-microusd 40000 \
--max-tokens-budget 40000
```

未给出的维度沿用场景预算。预算在调用发出前扣减，不退款。

## 四、旧入口仍可用

完整提案：

```bash
PYTHONPATH=src python3.12 -m flowagent.demo.api \
  --workdir /tmp/fcode-deepseek-full \
  --scenario full \
  --fresh
```

Packet 选择：

```bash
PYTHONPATH=src python3.12 -m flowagent.demo.packet \
  --workdir /tmp/fcode-deepseek-packet \
  --scenario full \
  --policy deepseek \
  --fresh
```

两个旧入口同样支持第二节的环境变量，也可以继续用显式 CLI 价格参数。为避免
命令行复制错误，新工作优先使用 `flowagent.demo.deepseek`。

## 五、预算与成本语义

`Harness` 在发送前按 provider 声明的上界预留。DeepSeek 会先渲染当轮实际要
发送的 prompt，再按字符保守估算 prompt tokens：

```text
reserve_tokens ≈ ceil(chars(system + user) / 3) + 64 + max_tokens
reserve_cost   = input_price * prompt_estimate + output_price * max_tokens
```

- 预留是上界，不是实际费用；FCode 不在响应返回后“退款”。
- Receipt 记录 provider 返回 usage 后按价格快照折算的成本估算。
- `external_billing` 恒为 `not-verified`：供应商回传的 usage 不是账单真相。
- usage 缺失时 metadata 写 `usage_missing: true`，不假装成本为零。
- 价格快照、request id、prompt version 都进 metadata，便于审计复算。
- Packet 模式通常每轮 500–800 tokens；proposal 模式明显更贵。

## 六、失败语义

| 情况 | 行为 |
| --- | --- |
| 401/403 | `MODEL_AUTH_FAILED`，任务进 `recovery_required`，不自动重试 |
| 429/5xx/超时/断连 | `MODEL_CALL_FAILED`，任务进 `recovery_required`，不自动重试 |
| 返回非 JSON / 空 content | `MODEL_CALL_FAILED` |
| 返回 JSON 但不是合法 plan | `PROPOSAL_MALFORMED`，不重试 |
| packet choice 非法 | `POLICY_INVALID_CHOICE`，不 repair、不重试 |
| 缺 key / 缺价格 / 缺日期 / http URL | 启动前失败，不会发出请求 |

外部调用没有“免费重试”：超时可能已经计费，重试会重复付费。也不会回退到
mock；把外部失败伪装成本地成功会破坏审计承诺。

## 七、安全边界

- API key 只在进程环境变量里，不写事件、不写日志、不进报告。
- Base URL 必须是 `https://`；不允许明文 HTTP。
- 只把合成/演示数据发给 DeepSeek；不要发客户资料。
- DeepSeek 的返回只是提议；数字仍由 Runtime 复算，报告仍由证据链生成。
- 控制台仍默认 offline-mock；本阶段只打通 CLI/lab API 层，不改 viewer 主链路。
- 接控制台前必须先在真实 key 下跑通 preflight、packet full、packet open-ended，
  并把结果记录在本文件。

## 八、当前验证与限制

2026-10-21 已用假端点跑通 `DeepSeekProvider` full 场景：9 轮、49 事件、
`succeeded`、链验证通过；packet policy 的请求/成本/非法 choice 测试通过。

2026-10-24 新增 API-first 配置层与统一 CLI：

- `providers/deepseek_config.py`：环境变量 + CLI override + 脱敏 preflight；
- `demo.deepseek`：`--preflight-only`、packet 默认模式、proposal 对照；
- `demo.packet` 的 exit code 改为“达到场景预期终态且 trace verified”；
- `demo.api` / `demo.packet` 均支持环境变量价格配置；
- packet receipt 保留 request id、usage 估算、价格快照与 `external_billing`；
- 离线测试覆盖：不联网 preflight、key 不泄露、缺价格启动前失败、
  open-ended 预期失败 exit 0。

### 2026-10-24 真实 DeepSeek live 验收

价格快照由 Host 确认：输入 `270000`、输出 `1100000` µUSD/1M tokens，
`as_of=2026-10-24`，来源 `host`；未配置缓存命中价，预留按输入价保守计算。

Preflight：

```text
ok=true
networkCalled=false
reservation tokens=4160
reservation cost=1176 µUSD
```

Packet full 用 `fcode.packet-prompt/v2` 连续 3 次全部通过：

| 指标 | run 1 | run 2 | run 3 |
| --- | ---: | ---: | ---: |
| status | succeeded | succeeded | succeeded |
| change_bp | -250 | -250 | -250 |
| model calls | 8 | 8 | 8 |
| auto actions | 1 | 1 | 1 |
| repaired | 0 | 0 | 0 |
| budget tokens | 5982 | 5980 | 5982 |
| budget cost µUSD | 2037 | 2037 | 2037 |
| trace verified | true | true | true |

Packet open-ended 连续 3 次全部达到预期诚实阻断：

| 指标 | run 1 | run 2 | run 3 |
| --- | ---: | ---: | ---: |
| status | failed | failed | failed |
| failure | INSUFFICIENT_EVIDENCE | INSUFFICIENT_EVIDENCE | INSUFFICIENT_EVIDENCE |
| model calls | 7 | 7 | 7 |
| repaired | 0 | 0 | 0 |
| budget tokens | 5368 | 5368 | 5368 |
| budget cost µUSD | 1819 | 1819 | 1819 |
| trace verified | true | true | true |

最终 trace 审计：

- full：8 条外部 receipt 全部有 request id 与价格快照，无 `usage_missing`；
- open-ended：7 条外部 receipt 全部有 request id 与价格快照，无 `usage_missing`；
- 两份 trace 均未出现 `sk-...` 形式的 key；
- `external_billing` 恒为 `not-verified`，供应商 usage 只作估算，不当账单。

v1 prompt 曾暴露一次顺序不稳定：full 场景第 8 轮在 `reconcile_basis` 与
`calculate_margin` 同时可用时先选了计算，触发 basis blocker。v2 加入明确依赖
规则后，上述 3 次 full 与 3 次 open-ended 均稳定。此问题没有用重试或 fallback
掩盖。

### 仍需注意

- live proposal 模式本次未重新计费运行；此前的假端点验证仍适用；
- 预留成本是预算上界，receipt 中的成本是按价格快照折算的 usage 估算；
- 控制台仍未接入 DeepSeek，下一步才是 host 配置、`/api/health` 与 UI 切换。
