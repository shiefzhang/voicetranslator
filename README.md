# 离线语音翻译 Android

原生 Android Java UI + sherpa-onnx SenseVoice / Silero VAD + llama.cpp Qwen2.5。
支持中文、日语、韩语、英语、粤语。

**v0.2.12 起：转写与专用翻译内置，通用翻译按需导入 —— 中英互译与中日互译不装也能直接翻。**

| 模型包 | 是否内置 | 体积 | 理由 |
| --- | --- | --- | --- |
| 转写 SenseVoice | ✅ 内置 | 229 MiB | 没有它整个 App 都跑不起来，必须开箱可用 |
| 专用翻译 OPUS-MT | ✅ 内置 | 683 MiB | 决定「智能选择」档能不能用上；含日译中两跳接线 |
| 通用翻译 Qwen2.5 1.5B Q4_K_M | ❌ 由你导入 | 1.04 GiB | 三者里最大，内置会让安装包翻倍 |

（v0.2.6～v0.2.8 曾把三份全打进包里，安装包约 1.8 GB；v0.2.9 一度全都不内置。
现在取中间：内置的两份合计 912 MiB，装完就能转写、并能用专用引擎翻中英日的四个方向，
通用翻译导入一次后即可完全离线使用。）

⚠️ 「通用翻译要自己导入」**不等于「每次都要先导入」**（v0.2.12）：设置成「智能选择」时，
只要这个语向在专用包的接线里（中译英 / 英译中 / 中译日 / 日译中），App 就**不会**拦着你去导入 —— 直接开始翻译即可。
只有专用引擎覆盖不到的语向（中韩、日语、粤语等）才必须先导入通用翻译模型包，界面会明说缺的是哪一份。
判定读的是**专用包内 `routes.txt`**（端侧真正的语向数据源），不是写死的三个语向，所以换一份自制包也能正确判断。

## 安装

1. 安装 `dist/android/VoiceTranslator-0.2.12-arm64-debug.apk`（Android 8.0+，64 位 ARM，开发测试签名）。
2. 装好后**转写与专用翻译已经可用**。想翻中英日之间的任一方向，直接回翻译页点开始即可，不必先导入任何东西。
3. 要翻**其他语向**（中韩、粤语等）时，在设置页「导入模型包」里导入**通用翻译**那一份 `.vtmodel`；
   导入过程中会显示进度条（读取与校验两段）。缺它时非专用语向会提示还没就绪、并说清缺的是哪一份。
4. 设置主语言、翻译语言、停顿时间和界面风格。
5. 返回翻译页，允许麦克风权限，点击开始翻译。
6. 再次点击停止；已录入的句子继续处理。切页或进入后台会停止录音。

要换模型：设置页点「更换」再导一份即可顶替旧的，不必先移除；点「移除」可以收回那份包占用的空间。
（两端的模型包契约完全一致，同一份 `.vtmodel` 在 Android 和 iOS 上都能导入。）

> 📦 **通用翻译包从哪来** —— 它没有内置，去
> [Releases](https://github.com/shiefzhang/voicetranslator/releases) 下这一份：
> [`qwen25-1.5b-q4km-zh-ja-ko-en-yue.vtmodel`](https://github.com/shiefzhang/voicetranslator/releases/latest/download/qwen25-1.5b-q4km-zh-ja-ko-en-yue.vtmodel)
> （**1.04 GiB**，推荐档 Q4_K_M，语向 zh/ja/ko/en/yue；同目录还有 `.sha256` 可核对）。
> **转写与专用翻译已经内置，不需要下载任何东西。**
> 直链规律：`https://github.com/shiefzhang/voicetranslator/releases/latest/download/<文件名>`。

设置页还有一个「翻译引擎」项，默认「智能选择」：**中英互译与中日互译走专用引擎**（更轻量，通常一秒内出结果），其余语向（含韩语、粤语）走通用模型；改成「统一模型」则全部走通用模型。改动在**下次开始翻译时**生效。专用引擎按需装载 —— 只有本次真正用到的语向才占内存，不拖慢启动。专用引擎覆盖到的语向**不需要通用翻译模型包**（见上文）；覆盖不到、而通用包又没导入时，会明确提示该导入哪一份，而不是报一句「加载模型失败」。

还有一个「翻译朗读」项，用**系统语音**读出译文（不联网），默认「自动朗读」：

| 档位 | 行为 |
| --- | --- |
| 关闭 | 不朗读，译文行尾也不显示喇叭 |
| 点按朗读 | 译文每行末尾显示喇叭，点哪句读哪句，再点一次停止 |
| 自动朗读（默认） | 新译文一到就自动读，同时保留行尾喇叭可随时停止或回听 |

**朗读期间会自动暂停录音、读完自动恢复** —— 外放的声音否则会被麦克风重新采进去。所以自动朗读时，朗读那几秒说的话不会被录到（这是该行为的必然代价，设置页说明里写明了）。语音用的是设备自带的 TTS，日/韩/粤的语音数据不少机器需要另行下载，缺失时会提示而不是静默失败。

建议先在 8GB RAM 以上手机试用。**Android 侧内置包会占两处**：`assets/` 里的 `.vtmodel` 是流式资源，
首启必须解包到内部存储才能随机读，所以同一份内容在 APK 内和内部存储各占一份
（转写 229 MiB + 专用翻译 548 MiB，解包后合计约 1.5 GiB）；再加上通用翻译 1.04 GiB，
**请预留至少 4GB 空间**。iOS 侧没有这个代价 —— bundle 目录本身可随机读，直接只读使用。
这是试用起点，尚非真机性能保证。对话和音频不落盘，退出进程后文本消失；需要保存时复制文本。

## 构建与模型包

需要 Python 3.12+、JDK 17/21、Android SDK 35、NDK 27.0.12077973、CMake 3.31.5。首次准备和 Gradle 依赖下载需要联网。

```powershell
python scripts/bootstrap.py
python scripts/prepare_models.py --kind all
python scripts/model_pack.py verify models/sensevoice-zh-ja-ko-en.vtmodel
python scripts/model_pack.py verify models/qwen25-1.5b-q4km-zh-ja-ko-en-yue.vtmodel
powershell -ExecutionPolicy Bypass -File scripts/build-android.ps1 -JavaHome D:/android-studio/jbr -SdkPath D:/Android/Sdk
python -m unittest discover -s scripts/tests -v
```

`bootstrap.py` 固定原生源码和依赖校验和；`prepare_models.py` 下载已量化权重，加入许可并生成模型包，产物直接落 `models/`（`model-tiers.py` 只扫这一处）。默认模型无需重新转换。网络中断支持 `.part` 续传；哈希不匹配时应排查来源，不要跳过校验。

**只想用、不想自己打包**：现成的模型包在
[Releases](https://github.com/shiefzhang/voicetranslator/releases) 上（含 `.sha256`），下载后按 §安装 导入即可，
上面的构建步骤可以整段跳过。两端契约一致，同一份 `.vtmodel` 通用。

**翻译档位统一为 Qwen2.5 1.5B Q4_K_M**（id `qwen25-1.5b-q4km-zh-ja-ko-en-yue-v1`）。全仓库不再有 3B 档位：`prepare_models.py` 下载的是 1.5B Q4_K_M，`model-tiers.py::RECOMMENDED` 钉的也是它，`verify_model_languages.py` 的默认参数同样指向它 —— 由 `scripts/tests/test_model_pack.py` 断言这三处不会漂移。

### 谁内置、谁导入（v0.2.10 起）

「哪些包随 App 分发」是**一条写死的规则**，不是「取体积最小的那些」。
规则只有一处真源 —— `scripts/model-tiers.py` 顶部的两张表：

```python
RECOMMENDED  = {"asr": …, "translation": …, "opus": …}   # 建议用户装哪些
BUNDLED      = {"asr": …, "opus": …}                     # 随 App 分发哪些
NEVER_BUNDLED = ("translation",)                          # 刻意永不打进包里
```

约束是 `BUNDLED ⊆ RECOMMENDED` 且 `BUNDLED ∩ NEVER_BUNDLED = ∅`。

⚠️ **为什么必须写死、而不是「自动选最小的」**：`RECOMMENDED` 里 `translation` 的推荐档是
**Q4_K_M**，而 `models/` 里 **Q3 更小**。任何「取最小」的隐式策略都会让推荐档静默漂移，
而输出看起来是「成功」。内置集合同理 —— 漂掉的结果是**安装包凭空大 1 GiB，没有任何报错**。

两端产物由同一条命令写出（**它是唯一往 App 目录写东西的地方**）：

```bash
python scripts/model-tiers.py --emit     # 写 ios/BundledModels/ + app/.../assets/bundled-models/
python scripts/model-tiers.py --verify   # 不扫 models/，只核对两边产物与表是否一致
```

`--verify` 是 `build-ios.sh` 里的那道闸：内置集合漂掉**不会编译失败**，只能靠它拦。
它不读 `models/`，所以在干净克隆上也能跑（产物目录在 `.gitignore` 里）。

要点：

- **iOS 用 folder reference，不是 group**。group 会把目录压平到 bundle 根 ——
  `asr/manifest.json` 与 `opus/manifest.json` 撞名，后一个静默覆盖前一个，构建期完全看不出来。
  判据是 `find X.app/BundledModels -name manifest.json` 每个 kind 恰好一条。
- **iOS 不复制内置包**：bundle 目录可随机读，`bundled()` 直接返回 bundle 里的路径，零额外用户空间。
- **Android 必须首启解包**：assets 只能流式读，所以 `MainActivity.prepareBundled()` 在启动时
  把两份 `.vtmodel` 解到内部存储。**它失败不影响启动**（只记日志），否则一个包解不开就会让 App 打不开。
- **`index.json` 必须是顶层数组**：iOS 的 `bundledIndex` 就是这么解的，
  改成对象会让 App 静默读到空索引、三行全显示「未就绪」。
- **`selected = installed ?? bundled` 是两端唯一的取包入口**。任何调用点直接调 `installed`，
  内置那一份在**那个点**就永远看不到 —— 症状是「设置成智能选择但一直在用通用模型」，不报错，只是变慢。

设置页三行分别显示「内置 / 已导入 / 未就绪」；「移除」只在「已导入」时出现
（内置的那份删不掉，删了也没意义）。

选哪一档由 `scripts/model-tiers.py` 给出：

```bash
python scripts/model-tiers.py                                    # 列档位 + 体检每个包能否被导入
python scripts/model-tiers.py --json dist/models/catalog.json    # 机器可读的档位目录
python scripts/model-tiers.py --strict                           # 任意候选体检不过就退出 1
```

推荐档位写在脚本顶部的 `RECOMMENDED` 里，**刻意不是「取体积最小」**：`translation` 的推荐档是
**Q4_K_M**，而 `models/` 里 **Q3 更小**，写成「取最小」会让推荐静默漂档、而输出看着像「成功」
（依据见「量化档位与Qwen3.5端侧实测报告.html」）。所以推荐档位是显式的，且会打印依据。

体检三条判据，每条都对应一个真出现过的失败：

| 判据 | 不满足会怎样 |
| --- | --- |
| `manifest.json` 是**第一个** zip entry | Android 用顺序读器（`VtStoredZip`）解包，会报「缺少有效 manifest.json」，用户会以为文件下错了 |
| 带 ZIP64 记录（包尾有 `PK\x06\x07`） | 中央目录偏移越过 2 GiB 后 Android 报 `invalid CEN header`。⚠️ 包**小于** 2 GiB 也可能踩到（差的是一个中央目录的长度） |
| 清单里有 `licenses/` 文件 | 两端在解包前就拒绝（「缺少许可文件」） |

`scripts/tests/test_bundled_models.py` 盯着「内置的恰好是 `{asr, opus}`」这件事，共 22 条判据。
跨四层 —— 脚本表 / 两端源码 / 两端产物 / 工程文件 —— 而这几层里**只有工程文件那层会编译失败**。
最阴的坏法是「多内置了一份」：一切正常工作，安装包凭空大 1 GiB，没有任何报错。

每条判据都对着一个具体坏法，而且**都见过它变红**（反向验证见下）：

| 判据 | 不满足的后果 |
| --- | --- |
| `BUNDLED ⊆ RECOMMENDED`、与 `NEVER_BUNDLED` 不相交 | 「内置哪一档」两处各说一套 |
| 两端产物的 kind 集合**恰好**等于 `BUNDLED` | 客户端多装 / 少装一份 |
| `translation` 不出现在任何一端 | 安装包凭空大 1 GiB |
| 工程里 `BundledModels` 出现在 **Resources 阶段** | 静默不打包，`.app` 里一个文件都没有 |
| 是 `folder` reference 而非 `assetcatalog`/`group` | 目录被压平 → `manifest.json` 撞名覆盖 |
| `index.json` 恰好列出 `BUNDLED`，且是顶层数组 | 读到空索引 → 三行全「未就绪」 |
| `selected = installed ?? bundled` 是唯一入口 | 内置那份永远用不到（专用引擎静默失效） |

⚠️ **产物类判据在两个目录不存在时 skip 而不是 fail**：它们都在 `.gitignore` 里（生成物，约 1.6 GB），
干净克隆上本来就没有。构建机上这几条必须真跑。

**没见它红过的判据不算判据** —— 这些判据都用「故意写坏的输入」逐条验过（17 次注入，
每次只让目标判据变红，逐字节还原后全绿）。过程中揪出的两个真问题记在这里，因为它们会重犯：

- 工程文件的判据**不能按字面量数**。`BundledModels` 这个字符串在工程里出现四次，把
  **真正决定打包的 Resources 阶段条目**删掉，`assertIn("BundledModels", text)` 照样通过。
  同理 `assertIn("--verify", text)` 会被注释里的 `--verify` 骗过。
- **快照式的「还原是否干净」是自指的**。开局树里就有垃圾时，快照会把它记成基线、
  还原后报「完全一致」—— 这份垃圾于是被永久洗白。所以还原核对之前必须先**独立**断言产物本来就对。

## v0.2.12：专用引擎覆盖的语向不必先导入通用包 + 导入进度条

两块改动各有一条守卫，都见过它们变红（15 次注入，逐字节还原后全绿）：

| 守卫 | 盯的坏法 |
| --- | --- |
| `scripts/tests/test_translation_shortcut.py`（15 条） | 覆盖判定改成「有没有专用包」而不是「包内 routes.txt 里有没有这个语向」；门禁退回「永远要求通用包」；设置页与 `start()` 各写一套判定 |
| `scripts/tests/test_import_progress.py`（14 条） | 进度倒退 / 到不了 100% / 每次字节都刷 UI；只在文件之间跳（1 GiB 的 `model.gguf` 独占一整段）；漏掉一条解包路径；失败路径谎报 100% |

要点：

- **判定读包内 `routes.txt`**（一行 `语向<TAB>跳1,跳2`）。两个看着像但用不得的替身：
  设置页那句「中英互译与中日互译」是**策略**不是能力；manifest 的 `routes` 字段是**跳数**
  （两跳语向如 `ja-zh` 在里面根本不出现）。
- **`start()` 与设置页提示走同一个 `opusCovers`**，否则必然出现「设置页说能用、点开始报缺包」。
- **允许 → 必须报错**：专用引擎临时用不上（包被换掉 / ORT 不可用）而通用包又没导入时，
  要给一条说得清「去导入哪一份」的错误，不能报成「找不到 model.gguf」。
- **进度分两段**：读取（0–30%）+ 解包校验（30–100%），拼成一条单调递增的 0…1。
  **整数化本身就是节流**（一次导入最多 101 次回调 + 几次文案变更）。
- **总量拿不到时走不确定态**，不假装 0%。磁盘上的进度探针
  （`ios/VoiceTranslator/Models/ImportProgress.swift`）刻意只依赖标准库，
  测试直接把它拼上探针用 `xcrun swift` 跑**真实源码** —— 「进度倒退」和「到不了 100%」
  这种毛病，看代码是看不出来的。

TranslateGemma 4B 的官方权重受 Gemma 条款约束。先在 Hugging Face 接受条款并执行 `hf auth login`，安装 llama.cpp 转换依赖后，可一次生成不含视觉投影器的 Q3_K_M 与 Q4_K_M 包：

```powershell
python -m pip install huggingface_hub -r third_party/llama.cpp/requirements.txt
python scripts/prepare_translategemma.py
python scripts/model_pack.py verify models/translategemma-4b-text-q3_k_m.vtmodel
python scripts/model_pack.py verify models/translategemma-4b-text-q4_k_m.vtmodel
```

转换固定到官方提交 `10042cb0e6e7fdce748996a71dc3dc432a4e0c89`，需要约 20GB 临时磁盘空间；默认在两个量化包生成后删除中间 F16 GGUF。脚本不传 `--mmproj`，因此输出仅包含文字模型。

### 超过 2 GB 的模型包（ZIP64）

`.vtmodel` 是 **STORED（无压缩）** 的 ZIP，模型多大包就多大，所以 TranslateGemma 4B 这一档必然越过 **2 GiB**。2 GiB 是**中央目录偏移的 32 位有符号悬崖**：越过去之后普通 ZIP 的 EOCD 偏移会被读成负数，Android 的 `ZipFile` 会报 `invalid CEN header (bad signature)`。

因此打包侧**无条件**写 ZIP64（`model_pack.py::build` 把 `zipfile.ZIP64_LIMIT` 压到 0，让每个条目都带 ZIP64 扩展字段、EOCD 走 8 字节偏移），并在 `verify()` 里断言这件事。判据是 EOCD 里的 `PK\x06\x07` 定位器存在：

```bash
python scripts/model_pack.py verify models/translategemma-4b-text-q3_k_m.vtmodel   # 会打印 ZIP64 判定
python scripts/model_pack.py repack old.vtmodel new.vtmodel                        # 把早于该修复的旧包重打成 ZIP64
python scripts/check-zip-reader.py                                                 # 合成 >2 GiB 的包，验两端读器
```

⚠️ **2026-09-30 之前打的包可能没有 ZIP64。** 实测 `models/translategemma-4b-text-q3_k_m.vtmodel` 的中央目录偏移是 `2,098,494,372`，**距 `2^31` 只剩 49 MB** —— 当下不报错纯属余量还没用完，换任何一档更大量化就会翻车。用 `repack` 重打即可，文件内容与 SHA-256 不变。

Android 侧不靠「超过 2 GiB 就换路径」这种体积猜测来决定读法：`ModelPackages.planFor()` 直接读包尾的 EOCD，**按中央目录偏移与 ZIP64 哨兵值**判定，必要时回落流式路径，并在 `ZipException` 上二次兜底。iOS 侧 `VTZipArchive` 是自研的随机访问 ZIP64 读器，不经过 `ZipInputStream`。

TranslateGemma 是可选的“高质量模型”，不会替换随应用准备的默认 Qwen2.5 1.5B Q4_K_M 包。应用会根据包内 `engine` 自动选用 Gemma 的专用翻译提示词；在设置中导入任一 TranslateGemma 包即可切换，重新导入 Qwen 包即可恢复默认。Q3_K_M 更省内存，Q4_K_M 的量化损失更低；两者均需要比默认模型更多的存储和运行内存。

自定义模型包：准备含权重、词表和 licenses 子目录的文件夹，修改 `model-manifests/` 对应元信息，然后执行：

```powershell
python scripts/model_pack.py build model-work/asr model-manifests/asr.json dist/models/custom-asr.vtmodel
```

包格式是 ZIP，包含 schemaVersion=1 的 manifest.json 和逐文件 SHA-256。Android 与 iOS 都校验路径、文件类型、大小、声明清单和语种范围（**v0.2.4 起为中日韩英粤五语**，此前为中日韩英四语；模型包的 `languages` 必须与 App 的语种表**完全一致**，多一个或少一个都会被拒收）。SHA-256 用于完整性检查，不代表发布者身份认证；仅导入可信来源模型。

## 可选转换

```powershell
python -m pip install -r third_party/llama.cpp/requirements.txt
python scripts/convert_models.py qwen --hf-dir path/to/Qwen2.5-1.5B-Instruct --quantize-bin path/to/llama-quantize.exe --output-dir model-work/custom-qwen
python -m pip install onnx onnxruntime
python scripts/convert_models.py sensevoice-int8 --fp32 path/to/sherpa-sensevoice-fp32.onnx --output model-work/custom-asr/model.int8.onnx
```

Qwen 转换要求本地完整 Hugging Face 权重及对应 tokenizer。SenseVoice 只接收携带 sherpa 前端元数据的兼容 FP32 ONNX；不能直接输入任意 checkpoint。`scripts/vendor/sensevoice-export-onnx.py` 为上游导出参考。转换入口已提供，但本次交付使用上游已转换权重，并未重新运行完整 FP16 → 量化转换链；自制模型需另做推理验证。

## 验证范围与限制

`dist/verification/` 保存测试报告与模拟器截图。桌面转写测试不是手机性能数据；模拟器推理也不能替代真机麦克风、延迟、发热、耗电测试。默认 Qwen2.5 1.5B Q4_K_M 包经过 12 个方向的文字体系检查，但该小样本不等于翻译质量评测。

中英互译（zh↔en）与中日互译（zh↔ja）现在由 **OPUS-MT 专用引擎**承担，其余语向仍走 Qwen —— 这是设置页「翻译引擎」项的默认档（「智能选择」）；改成「统一模型」则全部走 Qwen。专用引擎的门禁数据、评测脚本与逐方向准入门槛见 [`evaluation/README.md`](evaluation/README.md)。`ja→zh` **走英文 pivot 两跳**（`ja-en` → `en-zh`）：官方 `ja-*` 没有任何以中文为目标的包，门禁实测它是最弱的一向（关键错误率 75% vs Qwen 50%）—— 但它 P50 延迟仅 69 ms（Qwen 1405 ms，快约 20×），所以仍交给专用引擎，用速度换质量。任何一层判定不满足都会静默回落到 Qwen，该选项不会让翻译不可用。

当前是前台连续录音试用版：最大分句 15 秒，等待处理达到上限时停止录音并处理已接收内容；不提供锁屏持续录音。草稿是定期重解码，不是真正流式 ASR。长篇对话、嘈杂环境、人名数字及否定句需人工核对。

模型许可随模型包附带。应用引擎及第三方依赖见各上游仓库许可；商用发布前需维护完整第三方声明、正式签名和目标商店要求。
