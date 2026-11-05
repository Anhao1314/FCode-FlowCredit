# FCode SFT 云端训练包（阿里云 T4）

在一台 Ubuntu 22.04 + T4 16GB 的实例上，把 FCode 的哈希链轨迹变成 LoRA 适配器，
再导出为 Ollama 可用的 Q4_K_M GGUF。全程约 ¥100 以内（含冒烟与迭代）。

## 0. 数据已就绪（本机生成）

```
sft/data/train_smoke.jsonl   125 条   （51 条来自真实轨迹 + 74 条生成）
sft/data/train_full.jsonl   2271 条   （51 条来自真实轨迹 + 2220 条生成）
sft/data/choices_lab_*.jsonl  6005 条 choice 样本（train 5223 / val 782，见 4b）
sft/data/stats_*.json        生成统计（prompt_version 与来源计数）
```

重新生成（本机，零依赖）：

```bash
cd flowcredit
PYTHONPATH=src python3 -m flowagent.sft.export \
  --out sft/data/train_full.jsonl \
  --traces "viewer/traces/*.trace.json" "viewer/runs/*/trace.json" \
  --generate 60
```

训练与推理共用同一个渲染器（`flowagent/providers/prompt.py`，`PROMPT_VERSION`
写进每条样本）；`tests/test_sft_export.py` 证明导出的 prompt 与真实运行时逐字节一致。

## 1. 上传到实例

```bash
# 本机执行（替换 <ip>）
scp -r flowcredit/sft/cloud root@<ip>:/root/fcode-sft/
ssh root@<ip> 'mkdir -p /root/fcode-sft/data'
scp flowcredit/sft/data/*.jsonl root@<ip>:/root/fcode-sft/data/
```

## 2. 环境（一次性，约 10 分钟）

```bash
bash /root/fcode-sft/cloud/setup.sh
```

装好 venv、torch（cu12 自带运行时）、LLaMA-Factory、ModelScope，并下载
`Qwen3-4B-Instruct-2507` 权重（ModelScope 内网，几分钟）。

## 3. 冒烟（先做，~¥3）

```bash
bash /root/fcode-sft/cloud/train-smoke.sh
```

验收：loss 在下降、`/root/fcode-sft/saves/smoke` 里有 adapter。链路没通之前别开全量。

## 4. 全量（2.5-4 小时）

```bash
bash /root/fcode-sft/cloud/train-full.sh
```

## 4b. Choice SFT：packet policy head（下一轮的主线）

full-proposal 语料有一个致命缺口：**没有任何一条 assistant 标签选择
`report_blocked`**。所以 open-ended 只会一直读资料，不会如实停止。choice
SFT 从同一批上下文重建 packet，把 192 个阻塞上下文改标成
`report_blocked(insufficient_evidence)`，再做候选置换与负例。

本机生成（不需要 GPU）：

```bash
cd flowcredit
PYTHONPATH=src python3 -m flowagent.sft.choice_lab \
  --data sft/data/train_full.jsonl \
  --variants 3 --val-every 7 --seed 7
```

Gate 1（2026-10-21 实测，`sft/data/choices_lab_stats.json`）：

- 6,005 条样本（train 5,223 / val 782），6,005 个唯一 prompt，无冲突标签；
- `report_blocked` 标签 669 条（11.1%）；
- label 位置分布 1276/1294/1278/1171/986，位置熵 2.316 / 上限 2.322；
- 负例：提前停止 1,787、重复动作 1,444、错误参数 1,397；
- 单候选包 121 个跳过（host 单候选快路径不需要模型）。

上传并训练（冒烟先行）：

```bash
# 本机执行
tar czf - -C flowcredit/sft \
  data/choices_lab_train.jsonl data/choices_lab_val.jsonl \
  cloud/dataset_info.json cloud/qwen3_choice_smoke.yaml \
  cloud/qwen3_choice_lora.yaml cloud/train-choice-smoke.sh cloud/train-choice.sh \
  | ssh fcode-aliyun 'mkdir -p /root/fcode-sft && tar xzf - -C /root/fcode-sft'

# 云端执行
bash /root/fcode-sft/cloud/train-choice-smoke.sh   # ~10 分钟，先看 loss 与 adapter
bash /root/fcode-sft/cloud/train-choice.sh         # 2 epoch，1.5-3 小时
```

导出 policy 模型（不要覆盖 full-proposal 那个）：

```bash
ADAPTER_DIR=/root/fcode-sft/saves/choice-full \
MODEL_NAME=fcode-qwen3-4b-policy \
bash /root/fcode-sft/cloud/merge-and-export.sh
```

注意：`template: qwen3` 的 assistant 目标会带空 `<think></think>` 前缀，
`parse_choice_text` 会剥掉，packet 推理不需要额外配置。

排错：如果你的 LLaMA-Factory 版本不认识 `eval_dataset`，把两个 choice yaml 里的
`eval_dataset: fcode_choice_val` 删掉，改成 `val_size: 0.05`，其余不动。
冒烟跑起来之后再看 `choice-smoke.log`：loss 应该很快降到 0.05 以下，
因为候选集只有几个 id。

## 5. 合并 + 导出 GGUF（10-20 分钟）

```bash
bash /root/fcode-sft/cloud/merge-and-export.sh
```

关键：**必须从 4-bit 训练基座合并**。`merge_4bit.py` 先加载 NF4 基座、合并 LoRA，
再 `dequantize()` 成 fp16；直接合进原始 fp16 基座会改变首 token 分布，模型会退化
成重复的 `</tool_call>`。脚本可重复执行，已存在的 `out/merged` 会跳过。

产物：`/root/fcode-sft/out/${MODEL_NAME}-Q4_K_M.gguf`（~2.5 GB）+
`Modelfile-${MODEL_NAME}`。默认合并 `saves/full`、命名 `fcode-qwen3-4b`；
导出 policy 模型时用 `ADAPTER_DIR` / `MODEL_NAME` 覆盖（见 4b）。
`Modelfile` 不写 `SYSTEM`：FCode 每次请求都会发送自己的 system prompt，双重
system 会改变训练分布。

转换器的依赖装在独立的 `gguf-venv`（继承主 venv 的包，但不覆盖），不会再出现
converter 的 `torch==2.11` 把训练环境的 cu121 torch 替换掉的问题。

## 6. 回本机，装进 Ollama

```bash
scp root@<ip>:/root/fcode-sft/out/fcode-qwen3-4b-Q4_K_M.gguf .
scp root@<ip>:/root/fcode-sft/out/Modelfile-fcode-qwen3-4b .
ollama create fcode-qwen3-4b -f Modelfile-fcode-qwen3-4b
```

然后释放实例（不是停止）。

## 7. 验收 = 运行时本身

训练指标不是验收标准。这一版 adapter 训练的是**完整提案**（`fcode.prompt/v10`
里的 `steps/tool/arguments`），所以先用 full-proposal lab 验收：

```bash
cd flowcredit
PYTHONPATH=src python3 -m flowagent.demo.local \
  --workdir /tmp/fcode-local-full --scenario full --fresh \
  --endpoint http://127.0.0.1:11434/v1 --model fcode-qwen3-4b --structured

PYTHONPATH=src python3 -m flowagent.demo.local \
  --workdir /tmp/fcode-local-open --scenario open-ended --fresh \
  --endpoint http://127.0.0.1:11434/v1 --model fcode-qwen3-4b --structured
```

`--structured` 走 Ollama 的 `json_schema` 约束解码，避免少一个引号就让整轮变成
unknown；端点不支持结构化输出时去掉该参数，并接受更高的格式失败率。

通过条件：

- `full` 到 `succeeded`，最终 `change_bp = -250`；
- `open-ended` 到 `failed / INSUFFICIENT_EVIDENCE`，不编数字；
- `verified = true`，链验证通过；
- 预算内完成，没有 `model_unknown` / `PROPOSAL_MALFORMED`。

### 2026-10-21 实测结果（Qwen3-4B LoRA + Q4_K_M）

> framework.md 的状态行引用这一节（含那条**否定结论**：packet 1/2，暂不默认启用）；改这里的结论时请同步 [../../docs/framework.md](../../docs/framework.md)。

- 训练：558 步 / 2 epoch，`train_loss=0.1862`，`eval_loss=0.0995`。
- 导出：必须从 4-bit 基座合并；fp16 合并会输出重复 `</tool_call>`。
- `full`（full-proposal）：**通过**。9 轮、49 事件、`succeeded`、链验证通过。
- `open-ended`（full-proposal）：**未通过**。不加约束时第 3 轮 JSON 少一个引号，
  运行时报 `MODEL_OUTCOME_UNKNOWN`；加 `--structured` 后不再畸形，但模型反复
  重试 `reconcile_basis`，12 轮后 `failed / ROUND_LIMIT`，不是
  `INSUFFICIENT_EVIDENCE`。结论：会诚实失败，但不会及时 `report_blocked`。
- packet 验收：base 与 `fcode-qwen3-4b` 都是 **1/2**；`open-ended` 正确
  `INSUFFICIENT_EVIDENCE`，`full` 过早阻塞。选择合法、无 repair、链验证通过，
  但 full 决策过于保守。

因此**暂不把控制台默认切到 packet 模式**。下一步优先级：补 choice SFT
（每个决策点的候选与正确 choice），或在 host loop 里加“同一动作无状态变化
即要求 report_blocked”的进度守卫。`--structured` 只是格式约束，不解决策略问题。

packet 模式是另一条验收线，要求模型只输出 `{"choice": "aN"}`。full-proposal
模型（`fcode-qwen3-4b`）在 packet 上是 1/2：open-ended 正确，full 过早阻塞。
choice SFT 训练完成后用 `fcode-qwen3-4b-policy` 验收：

```bash
PYTHONPATH=src python3 -m flowagent.sft.accept \
  --workdir /tmp/fcode-accept-policy \
  --endpoint http://127.0.0.1:11434/v1 \
  --model fcode-qwen3-4b-policy
```

`repaired` 会作为警告显示，不静默忽略；报告同时记录 model calls、tokens、
延迟与 packet 体积。

## 常见问题

- **模板报错**：LLaMA-Factory 版本较老时 `template: qwen3` 可能不存在，改用 `template: qwen`。
- **显存 OOM**：保持 `per_device_train_batch_size: 1`，把 `cutoff_len` 从 2048 降到 1536；
  仍不行则 `gradient_accumulation_steps` 加倍、`lora_rank` 降到 8。
- **模型 ID 不存在**：把 `MODEL_ID` 换成 `Qwen/Qwen3-4B`（thinking 版需在推理端
  `/no_think`；本数据集不含思考链，2507 指令版更匹配）。
- **下载慢**：权重走 ModelScope；pip 已用阿里云镜像；GitHub 只用于 llama.cpp，
  若被限速可换 gitee 镜像。
