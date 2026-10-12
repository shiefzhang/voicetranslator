#!/usr/bin/env bash
#
# build-ios.sh —— 命令行构建 VoiceTranslator iOS。
#
# 为什么不直接敲 xcodebuild：
#   Swift 编译器在展开 `@State` 这类宏时，会拉一个 swift-plugin-server 子进程，
#   并且**默认用 sandbox-exec 把它关进沙箱**，该进程需要写 ~/.swiftpm/security。
#   在带沙箱的 shell（例如 WorkBuddy 的执行环境、部分 CI）里这一步会被拒绝：
#       [sandbox] 命令被沙箱拦截 … ~/.swiftpm/security (file-write-unlink)
#       error: external macro implementation type 'SwiftUIMacros.StateMacro' could not be found
#   加 `-Xfrontend -disable-sandbox` 让宏插件不再自我沙箱即可。
#   本项目不引入任何第三方宏插件，因此这个开关没有额外的安全影响；
#   你也可以随时去掉它——在 Xcode.app 里直接 ⌘R 是不需要这个开关的。
#
# 用法：
#   scripts/build-ios.sh                    # Debug · 模拟器 · arm64
#   scripts/build-ios.sh --release          # Release
#   scripts/build-ios.sh --device           # 真机（需要签名）
#   scripts/build-ios.sh --clean            # 先 clean
#   scripts/build-ios.sh --no-variant-gate  # 不跑多变体指令集判据（默认会跑）
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IOS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/ios"

CONFIG=Debug
SDK=iphonesimulator
DEST='generic/platform=iOS Simulator'
EXTRAS=()
DO_CLEAN=0
# 多变体指令集判据默认开：它是「基线归档里 0 条 sdot/smmla」这条静态约束的
# **运行时对偶** —— 静态门禁只能证明归档干净，证明不了链进 binary 之后还是干净的。
GATE_VARIANTS=1

while [ $# -gt 0 ]; do
  case "$1" in
    --release) CONFIG=Release ;;
    --debug)   CONFIG=Debug ;;
    --device)  SDK=iphoneos; DEST='generic/platform=iOS' ;;
    --simulator) SDK=iphonesimulator; DEST='generic/platform=iOS Simulator' ;;
    --clean)   DO_CLEAN=1 ;;
    --no-variant-gate) GATE_VARIANTS=0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

# 模拟器只编 arm64：Apple Silicon 上 x86_64 切片没有意义，还会让构建时间翻倍。
if [ "$SDK" = "iphonesimulator" ]; then
  EXTRAS+=(ARCHS=arm64)
fi
# 真机需要签名，交给 Xcode 的自动签名；这里不强行覆盖。
if [ "$SDK" = "iphoneos" ]; then
  EXTRAS+=(CODE_SIGN_STYLE=Automatic)
else
  EXTRAS+=(CODE_SIGNING_ALLOWED=NO)
fi

cd "$IOS_DIR"

# 模型包内置（v0.2.10）：**只有 asr 与 opus 进 bundle**，通用翻译由用户在设置页导入。
#
# 「内置了哪几类」由 scripts/model-tiers.py 顶部的 BUNDLED 表钉住，
# 这里不重复那张表 —— 调 `--verify` 让它自己比对（两端集合、index.json、
# 工程引用、NEVER_BUNDLED 不许出现，全在里面）。
#
# ⚠️ 之所以每次构建都验一遍：这几种坏法里**有三种不会让 xcodebuild 报错** ——
# 目录在但工程没引用 → 静默不打包；缺 index.json → App 以为「没有内置包」；
# 多内置一份 → 一切正常，只是安装包大 1 GB。全都只在真机上表现为「功能莫名不可用」。
echo "==> 检查内置模型包（asr + opus 内置，translation 由用户导入）"
if ! python3 "$SCRIPT_DIR/model-tiers.py" --verify; then
  echo "      修法通常是二选一：" >&2
  echo "        重建内置产物：python3 scripts/model-tiers.py --emit" >&2
  echo "        重新生成工程：python3 scripts/gen-ios-project.py" >&2
  exit 1
fi

# 隐私清单（App Store 上架必需，v1.0.0 起）。
#
# ⚠️ 缺了它的失败形态是「**上传被拒收**」—— 本地构建、安装、运行**全都正常**，
#    直到 App Store Connect 那一步才报错。而且三种坏法本地都没症状：
#    ① 文件被删；② 文件在但工程没引用（不会进 .app）；
#    ③ 只在 Xcode 里加过 —— 下次重跑 gen-ios-project.py 就静默消失。
#    所以四条判据都要在**构建期**过一遍，别留到上传那一步。
#    依据与判据清单见 scripts/verify-privacy-manifest.py 开头的长注释。
echo "==> 检查隐私清单（App Store 上架必需）"
if ! python3 "$SCRIPT_DIR/verify-privacy-manifest.py"; then
  echo "      修法：python3 scripts/gen-ios-project.py" >&2
  exit 1
fi

# 专用翻译引擎（OPUS-MT）的编译期开关：ios/Config/Deps.xcconfig 的 VT_DEPS_DEFINES。
#
# ⚠️ 漏掉这个宏的失败形态是「**慢**」，不是「报错」，所以必须每次构建都验：
#    native/nmt/vt_ort.h 的 OrtApiBaseHandle() 只有定义了 VT_ORT_STATIC 才会调
#    OrtGetApiBase()，否则一路 return nullptr → VTOpusNmt.isAvailable() 恒为 NO
#    → OpusTranslationEngine.init 抛 nativeUnavailable → Pipeline 静默回落成通用模型。
#    工程照样编译、链接、安装、启动，只有「中译日/中英互译不再走专用引擎」这一个
#    可观察后果 —— 用户感知到的就是「每句都慢」，界面上没有任何提示。
#    详见 scripts/verify-opus-linkage.py 开头的长注释。
echo "==> 检查专用翻译引擎的编译期开关"
if ! python3 "$SCRIPT_DIR/verify-opus-linkage.py" --check-config; then
  echo "      修法：bash scripts/setup-ios-deps.sh --config-only" >&2
  echo "            （只重写 Config/Deps.xcconfig，不重跑依赖的交叉编译）" >&2
  exit 1
fi

if [ "$DO_CLEAN" = "1" ]; then
  echo "==> clean"
  xcodebuild -project VoiceTranslator.xcodeproj -scheme VoiceTranslator \
    -configuration "$CONFIG" -derivedDataPath build clean >/dev/null
fi

echo "==> $CONFIG · $SDK"
set -x
xcodebuild \
  -project VoiceTranslator.xcodeproj \
  -scheme VoiceTranslator \
  -configuration "$CONFIG" \
  -sdk "$SDK" \
  -destination "$DEST" \
  -derivedDataPath build \
  "${EXTRAS[@]}" \
  OTHER_SWIFT_FLAGS='-Xfrontend -disable-sandbox' \
  build
set +x

# ---------------------------------------------------------------------------
# 多变体指令集判据（默认开，`--no-variant-gate` 可跳过）
#
# 静态四道门禁（scripts/build-ggml-cpu-variants.sh）只证明**归档**干净：
# 基线归档里 0 条 sdot/udot/smmla/ummla。它证明不了 linker 把哪一份放进最终
# binary、也证明不了 dispatch 层没有意外把基线符号解析走 —— 那要靠对**已链接的
# 可执行文件**逐变体反汇编（scripts/verify-cpu-variants.py --binary）。
#
# 之所以接在这里而不是留成手动步骤：这里能拿到「刚编出来的那个 binary」，
# 路径不用猜；而一旦漏跑，SIGILL（A8 上跑到了 dotprod 指令）只会在真机上出现。
# ---------------------------------------------------------------------------
if [ "$GATE_VARIANTS" = "1" ]; then
  APP_BIN="$IOS_DIR/build/Build/Products/$CONFIG-$SDK/VoiceTranslator.app/VoiceTranslator"
  if [ ! -f "$APP_BIN" ]; then
    echo "警告：找不到构建产物 ${APP_BIN}，跳过多变体判据" >&2
  else
    echo "==> 多变体指令集判据"
    if ! python3 "$SCRIPT_DIR/verify-cpu-variants.py" --binary "$APP_BIN"; then
      echo "错误：多变体指令集判据未通过（见上）。" >&2
      echo "      排查方向：ios/Config/Deps.xcconfig 的归档顺序（必须夹在 libggml.a" >&2
      echo "      与 libggml-base.a 之间），以及 third_party 下是否残留裸 libggml-cpu.a。" >&2
      echo "      确认无误后可临时用 --no-variant-gate 跳过。" >&2
      exit 1
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 专用引擎判据 —— 上面那道 `--check-config` 只证明**配置里写了**，
# 这一道证明**编出来的目标文件里真的生效了**（改 xcconfig 不一定触发重编）。
#
# ⚠️ 判据必须看**编译单元**，不能看最终 binary：libonnxruntime.a 是被整体链进 App 的，
#    所以 `nm VoiceTranslator.app/VoiceTranslator | grep Ort` 永远一片符号 ——
#    **符号在 ≠ 宏在**。宏生效时 VTOpusNmt.o 里必然有未定义引用 `U _OrtGetApiBase`，
#    宏不生效时那个引用根本不会产生。这是唯一能区分的证据。
# ---------------------------------------------------------------------------
echo "==> 判据：专用翻译引擎真的编进来了"
if ! python3 "$SCRIPT_DIR/verify-opus-linkage.py" --build-dir "$IOS_DIR/build"; then
  echo "      改 xcconfig 不会自动触发重编 —— 需要 clean 或 touch 源文件后重编。" >&2
  exit 1
fi
