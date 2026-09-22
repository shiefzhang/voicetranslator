# VoiceTranslator iOS

Android 版 `voicetranslator` v0.2.2 的 iOS 移植，按 [iOS 移植详细设计书](../iOS移植详细设计书.html) 的分层实现：
**纯逻辑 1:1 移植 → 原生依赖换成 ObjC++ 桥 → 平台适配（音频 / 界面 / 存储）**。

目标：iOS 15.0+ · arm64 · 完全离线 · 音频与对话文本只留在内存。

---

## 1. 当前状态

| 项目 | 状态 |
| --- | --- |
| 源码移植（20 个 Swift + 4 个桥接文件） | ✅ 完成 |
| Swift 层类型检查 | ✅ 0 error / 0 warning |
| ObjC++ 桥 `-Wall` 语法检查 | ✅ 干净 |
| Debug 构建（`iphonesimulator` arm64） | ✅ `** BUILD SUCCEEDED **` |
| Release 构建 | ✅ `** BUILD SUCCEEDED **` |
| **真机编译**（`iphoneos` arm64，未签名） | ✅ `** BUILD SUCCEEDED **`，产出 arm64 单架构 `.app` |
| **真机安装运行** | ⚠️ 可以装、可以启动，但**需要你先填 Team ID**（见 §3.5）；且原生库未接入前翻译功能不可用 |
| **原生推理库（sherpa-onnx / llama.cpp）** | ❌ **未接入**，需你在本机跑 `scripts/setup-ios-deps.sh` |
| App 图标 / Asset Catalog | ❌ 未做（沿用 Android 的图标待选稿，未定版） |
| 单元测试 | ❌ 未写（需要真机 + 模型包） |

> **关键前提**：现在这个工程**能编译、能启动、能看界面、能导入模型包并跑完 ZIP64 + manifest + SHA-256 校验**，
> 但语音识别和翻译在原生库就位前不可用——界面会明确提示
> 「…原生库未集成，请先运行 scripts/setup-ios-deps.sh」。
> 这不是降级实现，而是刻意用 `#if __has_include(...)` 做的**可选依赖**：
> 缺库时走 `isAvailable == NO` 分支，保证工程永远处于可编译状态。

---

## 2. 目录结构

```
ios/
├── VoiceTranslator.xcodeproj/           # 由模板生成，见 §3.4
├── Config/
│   ├── Base.xcconfig                    # 工程级公共设置（-lz、头搜索路径、$VT_DEPS_* 拼装）
│   └── Deps.xcconfig                    # 原生依赖开关，setup-ios-deps.sh 覆盖，初始为空
├── Info.plist                           # 麦克风权限、.vtmodel 类型声明、文件共享开关
├── third_party/                         # 依赖产物（.gitignore 中）
└── VoiceTranslator/
    ├── App/
    │   ├── VoiceTranslatorApp.swift     # @main；scenePhase → 退到后台即停录
    │   ├── AppModel.swift               # 状态容器 + 偏好（≈ MainActivity 字段 + SharedPreferences）
    │   └── Theme.swift                  # 三套色板，色值与 MainActivity.palette() 逐条一致
    ├── Core/
    │   ├── Language.swift               # 语言集合与方向限制
    │   ├── Segmenter.swift              # 停顿分句（预卷 0.3s / 硬上限 15s / 最小有效 0.15s）
    │   ├── PromptBuilder.swift          # Qwen few-shot 与 Gemma role prompt、脚本检查
    │   ├── AudioCapture.swift           # AVAudioEngine → 16k/mono/float32/512 帧
    │   └── Pipeline.swift               # 三线程流水线（vt-session / vt-audio / vt-translation）
    ├── Engines/
    │   ├── EngineTypes.swift            # VadEngine / AsrEngine / TranslationEngine 协议 + 错误
    │   ├── SherpaEngines.swift          # VTVad / VTAsr 的 Swift 封装
    │   └── LlamaTranslationEngine.swift # 翻译引擎：错语种重试一次（Qwen）
    ├── Models/
    │   └── ModelPackages.swift          # .vtmodel 导入：ZIP64 + manifest + SHA-256 + 原子激活
    ├── Views/                           # RootView / TranslationView / SettingsView
    └── Bridge/                          # Objective-C++（唯一与 Android 的 cpp/ 对应的部分）
        ├── VoiceTranslator-Bridging-Header.h
        ├── VTLlama.{h,mm}               # llama.cpp：tokenize/decode/贪心采样/prompt cache
        ├── VTSherpa.{h,mm}              # sherpa-onnx C API：Silero VAD + SenseVoice ASR
        └── VTZip.{h,mm}                 # 流式 ZIP/ZIP64 解包（zlib inflate + CommonCrypto SHA-256）
```

---

## 3. 构建

### 3.1 命令行

```bash
scripts/build-ios.sh              # Debug · 模拟器 · arm64
scripts/build-ios.sh --release    # Release
scripts/build-ios.sh --device     # 真机（需要 Xcode 里配好签名）
scripts/build-ios.sh --clean      # 先 clean
```

### 3.2 直接开 Xcode

```bash
open ios/VoiceTranslator.xcodeproj     # 选模拟器 ⌘R 即可
```

### 3.3 两个"看起来多余"的开关，原因都写在脚本注释里

| 开关 | 为什么 |
| --- | --- |
| `OTHER_SWIFT_FLAGS = -Xfrontend -disable-sandbox` | Swift 展开 `@State` 这类宏时会拉 `swift-plugin-server` 子进程，**默认再套一层 sandbox-exec**，它要写 `~/.swiftpm/security`；在被沙箱的执行环境里会失败并报 `SwiftUIMacros.StateMacro could not be found`。本项目无第三方宏插件，关掉无副作用；在 Xcode.app 里 ⌘R 不需要它。 |
| `ENABLE_DEBUG_DYLIB = NO` | Xcode 16 起 Debug 把主代码编成 `<App>.debug.dylib`，本工程链接该 dylib 时会报 `cannot link directly with 'SwiftUICore' … not an allowed client of it`。关掉即回到经典单可执行文件链接。 |
| `SWIFT_VERSION = 5.0`（不是 6） | 三线程流水线大量跨线程回调，Swift 6 严格并发会把这些变成**硬错误**。首期先保证可编译可跑；迁移到 Swift 6 是独立工程（见 §7）。 |

### 3.4 工程文件是生成的，不要手改

`VoiceTranslator.xcodeproj/project.pbxproj` 由脚本生成，UUID 用「文件路径 md5」派生：

```bash
python3 scripts/gen-ios-project.py           # 增删源文件后重跑
python3 scripts/gen-ios-project.py --check    # CI：校验是否与源码树同步
```

好处是**增删文件不必手改 UUID**，且 diff 只在真正增删时出现（否则 Xcode 每次打开都会全文件重排）。
`.h` 文件只作为引用展示，不进编译阶段；`.swift` / `.mm` 自动进 Sources。

### 3.5 装到真机上

工程本身已经过 `iphoneos` arm64 编译验证，**能装真机**。只有签名这一关需要你自己的账号：

**第 1 步 · 填 Team ID（唯一必须手动做的事）**

编辑 `ios/Config/Base.xcconfig`，把最后那行注释打开并换成你的值：

```
DEVELOPMENT_TEAM = ABCDE12345      # ← 你的 10 位 Team ID
```

Team ID 在哪看：Xcode → Settings → Accounts → 点你的 Apple ID → 右侧团队列表；或 developer.apple.com → Membership。
个人免费 Apple ID 也有 Team ID，不用付 99 美元。

**第 2 步 · ⌘R**

```bash
open ios/VoiceTranslator.xcodeproj
```

选你的 iPhone（真机第一次连要在手机上「信任此电脑」，并在
设置 → 通用 → VPN与设备管理 里信任你的开发者证书），然后 ⌘R。

**为什么 Team 要写在 xcconfig 而不是 Xcode 里点**

在 Xcode「Signing & Capabilities」面板里选 Team 时，Xcode 会把 `DEVELOPMENT_TEAM`
**写进 project.pbxproj**；而本工程的 pbxproj 是生成物，下次跑 `gen-ios-project.py` 就会被冲掉。
`Base.xcconfig` 生成脚本不碰，所以放这里才稳。

**本次工程为真机做过什么确认**

| 项 | 值 / 结论 |
| --- | --- |
| 目标 SDK / 架构 | `iphoneos` · arm64（`lipo -info` 确认非 fat） |
| `MinimumOSVersion` | 15.0 |
| 部署目标 API 可用性 | ✅ 编译器按 ios15.0 校验，0 warning（无 `#available` 漏洞） |
| 旁加载权限 | 无 entitlement（无推送/无 App Group/无 iCloud）→ 无需特殊 provisioning profile |
| 麦克风 | `NSMicrophoneUsageDescription` 已写，否则真机一录音就崩 |
| 设备家族 | `TARGETED_DEVICE_FAMILY = "1,2"`（iPhone + iPad） |

> 未烧图标不影响安装，只是桌面图标为空白占位。

---

## 4. 接入原生依赖（必做）

Android 侧是"拿来就用"：`app/libs/sherpa-onnx-v1.13.6.aar`（预编译）+ 本地编的 `libtranslator.so`。
iOS 侧**没有现成产物**：

| 依赖 | iOS 分发情况 |
| --- | --- |
| sherpa-onnx | 官方**不发布 iOS XCFramework**，只能用仓库自带 `build-ios.sh` 源码构建 |
| llama.cpp | 无预编译静态库；需 cmake 交叉编译（官方还提供 SPM，本项目按设计书 §6 用静态库） |

```bash
brew install cmake            # 前置
scripts/setup-ios-deps.sh     # 两个都构建（首次约 15~40 分钟）
scripts/setup-ios-deps.sh --sherpa-only
scripts/setup-ios-deps.sh --llama-only
```

脚本会：
1. 把 sherpa-onnx（tag `v1.13.6`，与 AAR 对齐）和 llama.cpp 克隆到 `ios/third_party/src/`；
2. 分别对 `iphoneos` / `iphonesimulator` 交叉编译，产物归到 `third_party/<dep>/lib/<platform>/`；
3. **自动重写 `Config/Deps.xcconfig`**，用 `PLATFORM_NAME` 分发到对应切片。

`Deps.xcconfig` 只负责填三个变量，由 `Base.xcconfig` 统一拼装——
这样"依赖是否就位"不会影响到其它设置的合并顺序：

```
VT_DEPS_INCLUDE_PATHS / VT_DEPS_LIBRARY_PATHS / VT_DEPS_LDFLAGS   ← Deps.xcconfig 填
HEADER_SEARCH_PATHS / OTHER_LDFLAGS = … $VT_DEPS_*                ← Base.xcconfig 用
```

删掉 `Deps.xcconfig` 的内容（或整个删掉）就回到"无原生依赖也可编译"的状态。

> ⚠️ **诚实说明**：`setup-ios-deps.sh` 是在**没有外网、且两个仓库体积大/构建耗时长**的开发沙箱里写的，
> **没有跑通过完整流程**。脚本对产物布局做了防御性搜索（`find` 定位 xcframework 切片、枚举 `*.a`），
> 但仍可能需要按你的实际构建输出微调。若链接报未定义符号，见脚本末尾的排查清单。

---

## 5. 与 Android 的对齐 / 已知差异

### 5.1 逐项对应

| 关注点 | Android 0.2.2 | iOS |
| --- | --- | --- |
| 录音 | `AudioRecord` 16k/mono/float，512 帧，除以 32768 | `AVAudioEngine` + `AVAudioConverter` → 16k/mono/float32，同样按 512 切帧 |
| VAD | `Vad.compute(frame) >= 0.5f`（Kotlin API 逐帧概率） | sherpa C API 只有 `Detected`，用 `threshold=0.5` 等价替代 |
| ASR | SenseVoice + `use_itn` | 同左（`VTAsr`） |
| 翻译 | `translator.cpp`（JNI） | `VTLlama.mm`（ObjC++），**采样循环/prompt 缓存/KV 清理/停止条件一行未改** |
| 断言/采样 | 贪心，无随机种子 | 同左，保证跨平台译文逐字复现 |
| 分句 | 预卷 4800 样本 / 上限 15 s / 最小有效 2400 样本 | 同左（`Segmenter`） |
| 流水线 | 3 线程：采集 / ASR / 翻译（翻译串行独占 llama context） | 同左，用 `Thread` + `NSCondition` 阻塞队列**刻意不用 Swift Concurrency**，以保留 Android 的阻塞/有界队列语义 |
| 模型包 | ZIP64 + manifest + SHA-256 + staging→rename 原子激活 | `VTZipArchive` 流式 ZIP64（zlib + CommonCrypto），其余规则逐条照搬 |
| 存储 | `SharedPreferences("vt")` + 应用私有目录 | `@AppStorage`（键名沿用 `main/other/pause/theme/shareMode`）+ `Application Support/Models/<id>-<uuid>` |
| 屏幕常亮 | 翻译期间 `KEEP_SCREEN_ON` | `UIApplication.isIdleTimerDisabled` |
| 分享 | `ACTION_SEND` + FileProvider | `UIActivityViewController` + 临时 `.txt` |

### 5.2 设计书里明确记录的差异

| 差异 | 原因 |
| --- | --- |
| VAD 用 `Detected` 而非逐帧概率 | sherpa-onnx **C API 不暴露** `compute()` 概率；`Detected` 内部同为"概率 ≥ threshold"。副作用：`min_silence_duration` 的迟滞会拉长停顿判定，因此压到 **0.1 s**（`VTSherpa.mm` 有注释） |
| 不支持后台/锁屏录音 | 设计书 §1 的范围界定；`scenePhase != .active` 即 `stopRecording()` |
| 停录后**已入队**的句子继续跑完 | 与 Android 一致：停的只是采集与 ASR，翻译线程排空后回到空闲 |

### 5.3 本次实现相对设计书的一处收紧

设计书 §5.3 只写了"临时文件放 `Library/Caches/ModelImports`"。实现里额外加了
`ModelPackages.purgeStaleScratch()`：每次导入前清掉上次异常退出残留的 `import-*.zip` / `stage-*`。
因为模型包上限 3 GB，而设计书 §9 的发布测试项里就有"模型导入中杀进程"——
不清理就会在用户设备上永久留下 GB 级垃圾。清理只按这两个前缀匹配，不会碰已激活的包。

---

## 6. 测试与验收（设计书 §9 的 iOS 侧清单）

| 层级 | 项目 | 状态 |
| --- | --- | --- |
| 单元测试 | 语言规则、方向禁用、脚本检查、路径安全、manifest 校验、SHA-256、ZIP64 | ❌ 待写（纯逻辑层无原生依赖，可直接用 XCTest 覆盖） |
| 引擎测试 | 7 种目标语言短句翻译；Qwen/Gemma；部分输出；取消；重复 load/close | ❌ 待原生库就位 |
| UI 测试 | 目标为法/德/俄时左侧禁用；主语言不出现法/德/俄；切换清空确认；VoiceOver | ❌ 待写 |
| 真机测试 | iPhone 12/SE3 以上，连续 10 分钟录音，停顿分句、发热、电量、内存、前台中断恢复 | ❌ 待真机 |
| 发布测试 | 冷启动、无网络、低磁盘、导入中杀进程、损坏包、>2 GB ZIP64 包 | ❌ 待真机 |

---

## 7. 后续待办

1. `scripts/setup-ios-deps.sh` 在本机实跑并固定产物布局；
2. 补 Asset Catalog 与 App 图标（Android 侧图标稿尚未定版）；
3. 单元测试（`Segmenter` / `PromptBuilder` / `ModelPackages` 三块**不需要原生库**，优先级最高）；
4. 真机验证音频链路与显存/内存占用；
5. 迁移到 Swift 6 语言模式 + 严格并发（需要重新设计流水线的 Sendable 边界）；
6. 三方许可清单（sherpa-onnx、llama.cpp、ONNX Runtime、Silero、SenseVoice 的 license 需随包附上）。
