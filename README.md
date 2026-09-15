# 离线语音翻译 Android

原生 Android Java UI + sherpa-onnx SenseVoice / Silero VAD + llama.cpp Qwen2.5。
支持中文、日语、韩语、英语。模型独立导入，不打进 APK。

## 安装

1. 安装 `dist/android/VoiceTranslator-0.1.0-arm64-debug.apk`（Android 8.0+，64 位 ARM，开发测试签名）。
2. 把 `dist/models/` 中两个 `.vtmodel` 文件复制到手机。
3. 在设置中分别选择转写模型包和翻译模型包，等待校验完成。
4. 设置主语言、翻译语言、停顿时间和界面风格，返回翻译页，允许麦克风权限，点击开始翻译。
5. 再次点击停止；已录入的句子继续处理。切页或进入后台会停止录音。

建议先在 8GB RAM 以上手机试用，并预留至少 5GB 空间用于原包、导入临时副本及已安装模型；这是试用起点，尚非真机性能保证。对话和音频不落盘，退出进程后文本消失；需要保存时复制文本。

## 构建与模型包

需要 Python 3.12+、JDK 17/21、Android SDK 35、NDK 27.0.12077973、CMake 3.31.5。首次准备和 Gradle 依赖下载需要联网。

```powershell
python scripts/bootstrap.py
python scripts/prepare_models.py --kind all
python scripts/model_pack.py verify dist/models/sensevoice-zh-ja-ko-en.vtmodel
python scripts/model_pack.py verify dist/models/qwen25-3b-zh-ja-ko-en.vtmodel
powershell -ExecutionPolicy Bypass -File scripts/build-android.ps1 -JavaHome D:/android-studio/jbr -SdkPath D:/Android/Sdk
python -m unittest discover -s scripts/tests -v
```

`bootstrap.py` 固定原生源码和依赖校验和；`prepare_models.py` 下载已量化权重，加入许可并生成模型包。默认模型无需重新转换。网络中断支持 `.part` 续传；哈希不匹配时应排查来源，不要跳过校验。

TranslateGemma 4B 的官方权重受 Gemma 条款约束。先在 Hugging Face 接受条款并执行 `hf auth login`，安装 llama.cpp 转换依赖后，可一次生成不含视觉投影器的 Q3_K_M 与 Q4_K_M 包：

```powershell
python -m pip install huggingface_hub -r third_party/llama.cpp/requirements.txt
python scripts/prepare_translategemma.py
python scripts/model_pack.py verify dist/models/translategemma-4b-text-q3_k_m.vtmodel
python scripts/model_pack.py verify dist/models/translategemma-4b-text-q4_k_m.vtmodel
```

转换固定到官方提交 `10042cb0e6e7fdce748996a71dc3dc432a4e0c89`，需要约 20GB 临时磁盘空间；默认在两个量化包生成后删除中间 F16 GGUF。脚本不传 `--mmproj`，因此输出仅包含文字模型。

TranslateGemma 是可选的“高质量模型”，不会替换随应用准备的默认 Qwen 2.5 3B 包。应用会根据包内 `engine` 自动选用 Gemma 的专用翻译提示词；在设置中导入任一 TranslateGemma 包即可切换，重新导入 Qwen 包即可恢复默认。Q3_K_M 更省内存，Q4_K_M 的量化损失更低；两者均需要比默认模型更多的存储和运行内存。

自定义模型包：准备含权重、词表和 licenses 子目录的文件夹，修改 `model-manifests/` 对应元信息，然后执行：

```powershell
python scripts/model_pack.py build model-work/asr model-manifests/asr.json dist/models/custom-asr.vtmodel
```

包格式是 ZIP，包含 schemaVersion=1 的 manifest.json 和逐文件 SHA-256。Android 校验路径、文件类型、大小、声明清单和四语范围。SHA-256 用于完整性检查，不代表发布者身份认证；仅导入可信来源模型。

## 可选转换

```powershell
python -m pip install -r third_party/llama.cpp/requirements.txt
python scripts/convert_models.py qwen --hf-dir path/to/Qwen2.5-3B-Instruct --quantize-bin path/to/llama-quantize.exe --output-dir model-work/custom-qwen
python -m pip install onnx onnxruntime
python scripts/convert_models.py sensevoice-int8 --fp32 path/to/sherpa-sensevoice-fp32.onnx --output model-work/custom-asr/model.int8.onnx
```

Qwen 转换要求本地完整 Hugging Face 权重及对应 tokenizer。SenseVoice 只接收携带 sherpa 前端元数据的兼容 FP32 ONNX；不能直接输入任意 checkpoint。`scripts/vendor/sensevoice-export-onnx.py` 为上游导出参考。转换入口已提供，但本次交付使用上游已转换权重，并未重新运行完整 FP16 → 量化转换链；自制模型需另做推理验证。

## 验证范围与限制

`dist/verification/` 保存测试报告与模拟器截图。桌面转写测试不是手机性能数据；模拟器推理也不能替代真机麦克风、延迟、发热、耗电测试。默认 Qwen 3B 包经过 12 个方向的文字体系检查，但该小样本不等于翻译质量评测。

中↔英 OPUS-MT PoC 不改变 App 默认引擎。评测脚本、数据格式和逐方向准入门槛见 [`evaluation/README.md`](evaluation/README.md)。只有正式数据集与人工盲评完成且达到门槛后，才进入 Android 运行时接入阶段；Qwen 始终保留为兜底。

当前是前台连续录音试用版：最大分句 15 秒，等待处理达到上限时停止录音并处理已接收内容；不提供锁屏持续录音。草稿是定期重解码，不是真正流式 ASR。长篇对话、嘈杂环境、人名数字及否定句需人工核对。

模型许可随模型包附带。应用引擎及第三方依赖见各上游仓库许可；商用发布前需维护完整第三方声明、正式签名和目标商店要求。
