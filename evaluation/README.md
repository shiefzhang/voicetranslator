# 中英翻译 PoC 数据集

`translation_poc.py` 保证 Qwen 与 OPUS-MT 使用完全相同的输入。内置
`zh_en_asr_sample.jsonl` 只有 10 条，用于检查流程，不能用于模型选型。

正式评测应替换为真实设备产生的 ASR 文本，每个方向至少 200 条，覆盖短口语、
否定、数字、金额、时间、地址、人名、噪声和常见识别错误。每行字段：

- `source`：未经人工修正的 ASR 输出；
- `reference`：人工参考译文；
- `critical_terms`：关键概念，每个内层数组表示可接受的同义写法；
- `manual`：由报告预留，盲评后填写 `pass` 或错误类型。

运行：

```powershell
.tools\model-venv\Scripts\python.exe -m pip install -r scripts\requirements-translation-poc.txt
.tools\model-venv\Scripts\python.exe scripts\translation_poc.py --dataset evaluation\正式数据.jsonl
```

报告写入 `dist/verification/translation-poc.json`。中→英、英→中分别判定：
关键错误率相对 Qwen 降低至少 20%，或 P95 稳态延迟降低至少 35%，才产生
`promote_opus: true`。预验证报告和未完成人工盲评的报告不得触发 App 默认引擎切换。
