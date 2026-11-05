# 环境配置示例与评测边界

## 离线运行

Runtime、测试和任务基准只需要 Python 3.11+、Git；前端测试另需 Node.js。无需安装第三方 Python 包，无需 API Key。

```bash
cd flowcredit
python -m unittest discover -s tests -q
node --test viewer/tests/*.test.js
python demo.py all
PYTHONPATH=src python -m flowagent.benchmark --output work/runtime-benchmark/latest
```

离线 benchmark 不读取模型凭据，并在应用层拒绝、记录联网尝试。它不是操作系统级网络命名空间或安全沙箱。

## 可选真实模型配置

真实 Provider 的配置见 [Kimi](kimi.md)、[DeepSeek](deepseek.md) 和 [OpenAI 兼容本地 Provider](openai-compat.md)。密钥只通过本机环境或本地未跟踪配置提供，例如 shell 中自行设置 `KIMI_API_KEY`；不要把值写进仓库、命令记录、manifest 或 Trace。

凭据变量存在不等于获得付费评测授权。只有操作者明确授权在线评测并设置成本上限后，才应先运行 2 个任务冒烟，再决定是否运行正式集。没有明确授权时保持：

```text
ONLINE_MODEL=NOT_RUN
online metrics=NOT_MEASURED
online badcases=NOT_MEASURED
```

不得根据离线 scripted 结果生成真实模型通过率、Token、成本、延迟或 Badcase。

## 可选命令白名单扩展

默认命令白名单在 `tools/general/commands.py`。需要扩展时，复制 `config/commands.json.example` 到本机配置位置并由主机审阅；扩展只能新增，不能删除或降级内置规则。`run_command` 始终无 shell，且不构成 OS 沙箱。
