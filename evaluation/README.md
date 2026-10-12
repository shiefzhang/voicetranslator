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

## 评测生产 prompt 本身（不是换模型，而是调 prompt）

换模型走上面的 `translation_poc.py` / `check-nmt-quality.py`；**改 prompt 结构**
（few-shot 组数、system 措辞）走 `scripts/check-translation-quality.py`：
它渲染的是 `translator.cpp` 的**生产 prompt**，按包内 manifest 的 `engine` 分派
（Qwen 是 ChatML + few-shot，Gemma 是 `<bos><start_of_turn>` 单轮 —— 两族完全不同，
详见该脚本 docstring）。

```bash
# 同用例、两个 shots 档位各跑一遍，逐条相减 prompt ms + 比 chrF
python3 scripts/check-translation-quality.py models/<包>.vtmodel \
    --dataset evaluation/zh_en_ja_sample.jsonl --compare-shots 3,1 \
    --threads 2 --report dist/verification/shots-compare-3-1.json
```

> ⚠️ **门槛仍然适用：内置数据集（10 / 20 条）低于「每方向 ≥200 条」的正式要求，
> 所以它的结论只能用来「否决」一个改动，不能用来「采纳」一个改动。**
> 实例：裁 few-shot 3→1 在 20 条口径下看似无损，实测细节见
> `.workbuddy/memory/ENGINEERING-NOTES.md` 附录 M.6。
