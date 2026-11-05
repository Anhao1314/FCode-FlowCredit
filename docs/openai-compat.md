# OpenAI 兼容端点接入：通用 provider 与四步清单

> 状态：`openai_compat` 引擎已上线；kimi 与 deepseek 是其预设（行为由
> 各自测试锁死）。主机声明新端点**不需要写代码**——四个环境变量加一条
> 声明，菜单即出现该端点，全部治理（门控/预算/收据/unknown 语义）原样
> 生效。

## 架构

```
providers/openai_compat.py   引擎：传输 / 价格快照 / 预留校准 / 收据 / 调用间隔
        ↑ 预设（pinned，测试锁定）
   kimi.py · deepseek.py      各自的 env 名、默认值、请求形状与收据契约
        ↑ 声明（无代码）
FCODE_OPENAI_COMPAT_PROVIDERS=前缀,前缀   LM Studio / Ollama / vLLM / 任意网关
```

预设与通用端点的差异全部是引擎旋钮：`completion_field`
（`max_completion_tokens` vs `max_tokens`）、`temperature_mode`
（omit-unless-pinned / pin-zero / send-configured）、`json_mode`、
`billing`（input-price-v1 全量输入计价 vs cache-split 缓存差价）、
`receipt_detail`、`require_https`（预设云端口强制 https；主机声明的
本地端点允许 `http://127.0.0.1`——声明本身就是主机信任边界，与
mcp.json 同源）。

## 接入新 API：四步清单

以下以 LM Studio 本地服务器为例（Ollama 的 OpenAI shim、vLLM、任何
OpenAI 兼容网关同理）。前缀任取大写字母数字（如 `LMSTUDIO`、`OLLAMA`、
`VOLCANO`）。

### 第 1 步 · Key

```bash
export LMSTUDIO_BASE_URL="http://127.0.0.1:1234/v1"
export LMSTUDIO_API_KEY=""        # 本地免鉴权可留空（请求即不带 Authorization）
export LMSTUDIO_MODEL="qwen3-8b-instruct"
```

云端端点填真实 key。Key 只经环境变量进入，永不落盘、不进 trace；
redacted 配置只显示 `env:LMSTUDIO_API_KEY`。

### 第 2 步 · 价格

```bash
export LMSTUDIO_INPUT_PRICE_MICROUSD_PER_MTOK=0    # 本地免费端点如实配 0
export LMSTUDIO_OUTPUT_PRICE_MICROUSD_PER_MTOK=0
export LMSTUDIO_PRICE_AS_OF="2026-10-25"           # 即使价格是 0，日期也必填
export LMSTUDIO_PRICE_SOURCE="host"
```

价格与日期**没有默认值**：过期猜测会把预算顶变成虚假承诺。免费端点
配 0 而不是省略——预算机制对 0 元端点同样走表（诚实记账，不静默跳过）。

### 第 3 步 · 校准

首跑后 Runtime 对比 `usage.prompt_tokens` 与预留估算；若实际高于估算
（中文文本常如此），校准比率自动落盘（`{PREFIX}_CALIBRATION_PATH` 可
指定，默认内存态）并在后续轮次收紧预留。已跑部分不回滚、不退款。
可选：`{PREFIX}_MAX_COMPLETION_TOKENS`（默认 512）、
`{PREFIX}_REASONING_EFFORT`（low/medium/high，端点支持时才配）、
`{PREFIX}_SUPPORTS_EFFORT=1`（菜单显示思考强度档位）、
`{PREFIX}_TEMPERATURE`（默认 0）。

### 第 4 步 · 间隔

```bash
export LMSTUDIO_MIN_CALL_INTERVAL_SECONDS=0     # 云端有 RPM 限制时按需上调
export FCODE_OPENAI_COMPAT_PROVIDERS="LMSTUDIO" # 声明（多个用逗号分隔）
```

间隔预约表是进程级的，键是 provider + base URL；同一端点的对话与委托运行共享
一张表，不会各自计时后同时越过组织 RPM。

重启 console。模型菜单里出现该端点，预算预览按其价格快照重算
（0 元即模板草案）。此后创建委托选择它即可——门控、审批、收据、
unknown 语义、链验证与任何 provider 完全一致。

## 主机审阅要点

- 声明前缀 = 允许一个外部调用面。确认 base_url 指向你信任的服务
  （本地服务器或你签约的云），与 mcp.json 的审阅同级。
- 云端端点建议在反代层限制可访问模型，key 权限最小化。
- `require_https` 对声明端点默认放开（本地 http 所需）；云端请配
  https base_url，不要经 http 明文传输 key。

## 诚实边界

- 预留校准是**保守上界**而非精确计费：实际计费以供应商账单为准，
  收据恒标 `external_billing: not-verified`（免费端点为 0 元收据）。
- 推理档/温度只是请求参数；端点是否真执行了相应策略不可验证。
- 菜单"可用"仅表示配置完整且（本地探活通过）；运行中端点故障按
  既有 unknown 语义处理（不重试、凭收据对账）。
