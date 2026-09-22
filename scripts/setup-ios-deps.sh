#!/usr/bin/env bash
#
# setup-ios-deps.sh —— 为 VoiceTranslator iOS 构建两个原生依赖，并写出链接开关。
#
# 为什么需要它：Android 侧用的是预编译产物（app/libs/sherpa-onnx-v1.13.6.aar +
# 本地编译的 libtranslator.so），而 **sherpa-onnx 官方不发布 iOS XCFramework**，
# llama.cpp 也没有可直接下载的 iOS 静态库。两者都只能在本机交叉编译。
#
# 产物布局（全部落在 ios/third_party/，已在 .gitignore 中忽略）：
#   third_party/sherpa-onnx/include/sherpa-onnx/c-api/c-api.h
#   third_party/sherpa-onnx/lib/<iphoneos|iphonesimulator>/*.a
#   third_party/llama/include/{llama.h,ggml*.h}
#   third_party/llama/lib/<iphoneos|iphonesimulator>/*.a
#   third_party/../../Config/Deps.xcconfig      ← 自动重写，工程立刻生效
#
# 用法：
#   scripts/setup-ios-deps.sh                # 两个都构建（设备 + 模拟器切片）
#   scripts/setup-ios-deps.sh --sherpa-only
#   scripts/setup-ios-deps.sh --llama-only
#   scripts/setup-ios-deps.sh --clean        # 先删掉 third_party 再构建
#
# 前置：Xcode 命令行工具、cmake（brew install cmake）、git、网络。
#
# ⚠️ 说明：本脚本在本项目的开发沙箱里**没有跑通过完整流程**（沙箱无外网、
# 且两个仓库体积大、构建耗时以十分钟计）。移植代码本身不依赖它——没有原生库
# 时工程照样编译、照样启动，只是 ASR/翻译不可用并在界面上提示。
# 首次执行请留意每一阶段的输出，若某个符号未定义，多半是静态库链接顺序问题，
# 见脚本末尾 "链接顺序" 的说明。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IOS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/ios"
VENDOR="$IOS_DIR/third_party"
DEPS_XCCONFIG="$IOS_DIR/Config/Deps.xcconfig"

SHERPA_TAG="v1.13.6"          # 与 Android 侧 app/libs/sherpa-onnx-v1.13.6.aar 对齐
LLAMA_REF="b4616"             # llama.cpp 的 tag；升版时同步改这里
PLATFORMS=("iphoneos" "iphonesimulator")
SLICE_FOR_PLATFORM_iphoneos="ios-arm64"
SLICE_FOR_PLATFORM_iphonesimulator="ios-arm64_x86_64-simulator"

BUILD_SHERPA=1
BUILD_LLAMA=1
DO_CLEAN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --sherpa-only) BUILD_LLAMA=0 ;;
    --llama-only)  BUILD_SHERPA=0 ;;
    --clean)       DO_CLEAN=1 ;;
    -h|--help)     sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

for tool in git cmake xcodebuild; do
  command -v "$tool" >/dev/null 2>&1 || die "缺少 $tool（cmake 可 brew install cmake）"
done

[ "$DO_CLEAN" = "1" ] && { log "清理 $VENDOR"; rm -rf "$VENDOR"; }
mkdir -p "$VENDOR"

copy_libs() {  # copy_libs <源目录> <目标目录>
  local from="$1" to="$2" count=0
  mkdir -p "$to"
  # 兼容两种布局：静态库直接在切片目录下，或在切片根的 lib/ 子目录下。
  while IFS= read -r -d '' archive; do
    cp "$archive" "$to/"
    count=$((count + 1))
  done < <(find "$from" -maxdepth 2 -name '*.a' -print0)
  [ "$count" -gt 0 ] || warn "在 $from 下没找到 .a 静态库"
  echo "$count"
}

# ---------------------------------------------------------------------------
# 1. sherpa-onnx（Silero VAD + SenseVoice ASR）
# ---------------------------------------------------------------------------
build_sherpa() {
  log "构建 sherpa-onnx $SHERPA_TAG（这一步最慢，首次约 10~25 分钟）"
  local src="$VENDOR/src/sherpa-onnx"
  if [ ! -d "$src/.git" ]; then
    mkdir -p "$VENDOR/src"
    git clone --depth 1 --branch "$SHERPA_TAG" https://github.com/k2-fsa/sherpa-onnx.git "$src"
  fi

  ( cd "$src" && ./build-ios.sh ) || die "sherpa-onnx 的 build-ios.sh 失败，请查看其输出"

  local xcf
  xcf="$(find "$src" -maxdepth 3 -name 'sherpa-onnx.xcframework' -type d | head -n1)"
  [ -n "$xcf" ] || die "没找到 sherpa-onnx.xcframework，build-ios.sh 的输出布局可能已变化"

  mkdir -p "$VENDOR/sherpa-onnx/include"
  # C API 头：Bridge 里是 #include <sherpa-onnx/c-api/c-api.h>，所以要把
  # 包含 c-api.h 的那一级目录整体拷成 include/sherpa-onnx/。
  local header_dir
  header_dir="$(find "$xcf" -maxdepth 3 -name 'c-api.h' | head -n1 | xargs -I{} dirname {} || true)"
  if [ -n "$header_dir" ]; then
    local top="$header_dir"           # …/Headers/sherpa-onnx/c-api
    top="$(dirname "$(dirname "$header_dir")")"   # → …/Headers
    cp -R "$top/sherpa-onnx" "$VENDOR/sherpa-onnx/include/" 2>/dev/null \
      || cp -R "$(dirname "$header_dir")" "$VENDOR/sherpa-onnx/include/sherpa-onnx" 2>/dev/null \
      || warn "头文件拷贝失败，请手工确认 c-api.h 的层级"
  else
    warn "xcframework 里没有 c-api.h；若你构建时没开 SHERPA_ONNX_ENABLE_C_API，请重新构建"
  fi

  local platform slice
  for platform in "${PLATFORMS[@]}"; do
    slice="$(find "$xcf" -maxdepth 1 -name "${SLICE_FOR_PLATFORM_${platform}:-*}" -type d | head -n1)"
    [ -n "$slice" ] || slice="$(find "$xcf" -maxdepth 1 -type d -name 'ios-*' | grep -i "${platform%s}" | head -n1 || true)"
    if [ -z "$slice" ]; then
      warn "$platform 切片缺失，跳过"
      continue
    fi
    local n
    n="$(copy_libs "$slice" "$VENDOR/sherpa-onnx/lib/$platform" | tail -n1)"
    log "  $platform：$n 个静态库（来自 $(basename "$slice")）"
  done
}

# ---------------------------------------------------------------------------
# 2. llama.cpp（Qwen2.5 / TranslateGemma GGUF 推理）
# ---------------------------------------------------------------------------
build_llama() {
  log "构建 llama.cpp $LLAMA_REF"
  local src="$VENDOR/src/llama.cpp"
  if [ ! -d "$src/.git" ]; then
    mkdir -p "$VENDOR/src"
    git clone --depth 1 --branch "$LLAMA_REF" https://github.com/ggml-org/llama.cpp.git "$src"
  fi

  local platform sysroot
  for platform in "${PLATFORMS[@]}"; do
    sysroot="$(xcrun --sdk "$platform" --show-sdk-path)"
    local build="$src/build-$platform"
    log "  cmake 配置 $platform（sysroot=$sysroot）"
    cmake -S "$src" -B "$build" \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_SYSTEM_NAME=iOS \
      -DCMAKE_OSX_SYSROOT="$platform" \
      -DCMAKE_OSX_ARCHITECTURES=arm64 \
      -DCMAKE_OSX_DEPLOYMENT_TARGET=15.0 \
      -DBUILD_SHARED_LIBS=OFF \
      -DLLAMA_BUILD_TESTS=OFF \
      -DLLAMA_BUILD_EXAMPLES=OFF \
      -DLLAMA_BUILD_SERVER=OFF \
      -DGGML_METAL=OFF \
      -DGGML_ACCELERATE=ON \
      >/dev/null
    cmake --build "$build" --config Release -j "$(sysctl -n hw.ncpu)"
    local n
    n="$(copy_libs "$build" "$VENDOR/llama/lib/$platform" | tail -n1)"
    log "  $platform：$n 个静态库"
  done

  mkdir -p "$VENDOR/llama/include"
  cp "$src/include/llama.h" "$VENDOR/llama/include/" 2>/dev/null || warn "缺 llama.h"
  cp "$src/ggml/include/"*.h "$VENDOR/llama/include/" 2>/dev/null || true
  # ggml 的部分头文件只有实现文件里才需要，一并带上以防编译期找不到。
  find "$src/ggml" -maxdepth 3 -name 'ggml*.h' -exec cp {} "$VENDOR/llama/include/" \; 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 3. 生成 Config/Deps.xcconfig
# ---------------------------------------------------------------------------
emit_archives() {  # emit_archives <目录> <相对 ios/ 的前缀>
  local dir="$1" prefix="$2"
  [ -d "$dir" ] || return 0
  # 链接顺序：被依赖者放后面。llama 依赖 ggml-*，ggml 依赖 ggml-base。
  local order=(llama ggml ggml-metal ggml-blas ggml-cpu ggml-base)
  local name
  for name in "${order[@]}"; do
    local f="$dir/lib$name.a"
    [ -f "$f" ] && printf ' $(SRCROOT)/%s/lib%s.a' "$prefix" "$name"
  done
  # 其余库补在后面（例如 sherpa 的一堆 fst/onnxruntime）。
  local f
  for f in "$dir"/*.a; do
    [ -e "$f" ] || continue
    local base; base="$(basename "$f" .a)"
    case " ${order[*]} " in *" ${base#lib} "*) continue ;; esac
    printf ' $(SRCROOT)/%s/%s' "$prefix" "$(basename "$f")"
  done
}

write_deps_xcconfig() {
  log "写出 Config/Deps.xcconfig"
  {
    echo "// 由 scripts/setup-ios-deps.sh 于 $(date '+%Y-%m-%d %H:%M:%S') 自动生成，请勿手工维护。"
    echo "// 重新构建依赖后本文件会被覆盖；删掉它即可回到「无原生依赖」的可编译状态。"
    echo
    local inc=""
    [ -d "$VENDOR/sherpa-onnx/include" ] && inc="$inc \"\$(SRCROOT)/third_party/sherpa-onnx/include\""
    [ -d "$VENDOR/llama/include" ]        && inc="$inc \"\$(SRCROOT)/third_party/llama/include\""
    echo "VT_DEPS_INCLUDE_PATHS =$inc"
    echo
    local platform
    for platform in "${PLATFORMS[@]}"; do
      local libs=""
      libs="$libs$(emit_archives "$VENDOR/sherpa-onnx/lib/$platform" "third_party/sherpa-onnx/lib/$platform")"
      libs="$libs$(emit_archives "$VENDOR/llama/lib/$platform"        "third_party/llama/lib/$platform")"
      if [ -n "$libs" ]; then
        # 用 PLATFORM_NAME（iphoneos / iphonesimulator）分发，xcconfig 里可直接做变量套变量。
        echo "VT_DEPS_LDFLAGS_$platform =$libs"
      fi
    done
    echo "VT_DEPS_LDFLAGS = \$(VT_DEPS_LDFLAGS_\$(PLATFORM_NAME))"
    echo
    echo "// 静态库里有 ObjC 分类/重复符号时通常需要这些开关（llama.cpp 一般不需要，留着无副作用）。"
    echo "// 若链接报 \"duplicate symbol\"，先试注释掉下面两行。"
    echo "// OTHER_LDFLAGS = \$(inherited) -Wl,-no_compact_unwind"
  } > "$DEPS_XCCONFIG"
  sed -n '1,60p' "$DEPS_XCCONFIG"
}

[ "$BUILD_SHERPA" = "1" ] && build_sherpa
[ "$BUILD_LLAMA" = "1" ] && build_llama
write_deps_xcconfig

cat <<'EOF'

完成。下一步：
  1. 在 Xcode 里打开 ios/VoiceTranslator.xcodeproj（Deps.xcconfig 已被重新生成）；
  2. 若刚才是在 Xcode 之外构建的，Xcode 会自动读到新的 xcconfig，必要时 Clean Build Folder；
  3. 回到 App 的「设置 → 转写模型包 / 翻译模型包」导入 .vtmodel 后即可离线使用。

排查链接错误：
  - "Undefined symbols: _llama_*" → 库没被链上，检查 Deps.xcconfig 里的路径是否存在；
  - "Undefined symbols: _ggml_*"   → 静态库顺序问题，把 ggml-base 往后挪；
  - "duplicate symbol"             → 见 Deps.xcconfig 末尾的注释；
  - 模拟器用 arm64 切片；x86_64 模拟器需要额外构建，一般不必。
EOF
