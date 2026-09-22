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
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IOS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/ios"

CONFIG=Debug
SDK=iphonesimulator
DEST='generic/platform=iOS Simulator'
EXTRAS=()
DO_CLEAN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --release) CONFIG=Release ;;
    --debug)   CONFIG=Debug ;;
    --device)  SDK=iphoneos; DEST='generic/platform=iOS' ;;
    --simulator) SDK=iphonesimulator; DEST='generic/platform=iOS Simulator' ;;
    --clean)   DO_CLEAN=1 ;;
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
