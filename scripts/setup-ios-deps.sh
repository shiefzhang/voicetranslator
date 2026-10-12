#!/usr/bin/env bash
#
# setup-ios-deps.sh —— 为 VoiceTranslator iOS 构建两个原生依赖，并写出链接开关。
#
# 为什么需要它：Android 侧用的是预编译产物（app/libs/sherpa-onnx-v1.13.6.aar +
# 本地编译的 libtranslator.so）；iOS 侧两个依赖都得处理一遍：
#   sherpa-onnx —— 官方**有**预编译 iOS xcframework，但挂在名为 `xcframework`
#                  的滚动 tag 下（见下面 §1），所以直接下就行，不用编译；
#   llama.cpp   —— 没有 iOS 静态库，必须用 cmake 交叉编译。
#
# 产物布局（全部落在 ios/third_party/，已在 .gitignore 中忽略）：
#   third_party/sherpa-onnx/include/sherpa-onnx/c-api/c-api.h
#   third_party/sherpa-onnx/lib/<iphoneos|iphonesimulator>/*.a
#   third_party/llama/include/{llama.h,ggml*.h}
#   third_party/llama/lib/<iphoneos|iphonesimulator>/*.a
#   third_party/../../Config/Deps.xcconfig      ← 自动重写，工程立刻生效
#
# 用法：
#   scripts/setup-ios-deps.sh                # 两个都处理（设备 + 模拟器切片）
#   scripts/setup-ios-deps.sh --sherpa-only
#   scripts/setup-ios-deps.sh --llama-only
#   scripts/setup-ios-deps.sh --from-source  # sherpa 改走源码构建（慢，10~25 分钟）
#   scripts/setup-ios-deps.sh --clean        # 先删掉 third_party 再构建
#   scripts/setup-ios-deps.sh --config-only  # 只重写 Config/Deps.xcconfig，不构建
#
# 前置：Xcode 命令行工具、git、cmake、网络。
#   cmake 三种装法（脚本会自动找）：brew install cmake / pip install cmake /
#   把 cmake-<ver>-macos-universal.tar.gz 解到 .tools/cmake/。
#
# 说明：两个依赖都是可选的——没有它们时工程照样编译、照样启动，只是 ASR/翻译
# 不可用并在界面上提示。首次执行请留意每一阶段的输出；若报未定义符号，多半是
# 静态库链接顺序问题，见脚本末尾的排查清单。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IOS_DIR="$ROOT/ios"
VENDOR="$IOS_DIR/third_party"
DEPS_XCCONFIG="$IOS_DIR/Config/Deps.xcconfig"
CACHE="$ROOT/downloads"        # 与 Android 侧 scripts/bootstrap.py 共用下载缓存

SHERPA_TAG="v1.13.6"          # 与 Android 侧 app/libs/sherpa-onnx-v1.13.6.aar 对齐

# llama.cpp：必须与 Android 侧**同一个 commit**，否则两台设备上的译文不再逐字一致
# （采样循环/prompt 模板虽然照搬，tokenizer 与 KV cache 实现随版本变化）。
# 这个 commit 与 SHA-256 抄自 scripts/bootstrap.py:6,18 —— Android 侧就是用它构建的。
# 注意：codeload 的 tarball 可以按裸 commit 取，而 `git clone --branch <sha>` 不行，
# 所以这里走「下载 tar.gz + 校验哈希」，顺便和 Android 共用 downloads/ 缓存。
LLAMA_COMMIT="6e14286edaa60a223292c8a996506905b2f66f66"
LLAMA_TARBALL_SHA256="2d82acb3734f50bf23f33b1fb431246611eb3b31923569319e2824be8b3842a1"
LLAMA_TARBALL_URL="https://codeload.github.com/ggml-org/llama.cpp/tar.gz/$LLAMA_COMMIT"
PLATFORMS=("iphoneos" "iphonesimulator")

BUILD_SHERPA=1
BUILD_LLAMA=1
DO_CLEAN=0
SHERPA_FROM_SOURCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --sherpa-only)  BUILD_LLAMA=0 ;;
    --llama-only)   BUILD_SHERPA=0 ;;
    --from-source)  SHERPA_FROM_SOURCE=1 ;;
    --clean)        DO_CLEAN=1 ;;
    # 只重写 Config/Deps.xcconfig，不碰依赖产物。
    # 用途：改了「写配置」那段逻辑之后（例如补上 VT_DEPS_DEFINES），
    # 不必为了一个新变量重跑 20 分钟的交叉编译。
    --config-only)  BUILD_SHERPA=0; BUILD_LLAMA=0 ;;
    -h|--help)      sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

# ⚠️ log 写 **stderr**，不是 stdout。
# 原因：本脚本里好几处用 `$(函数)` 取返回值（`copy_static_libs` 返回库个数、
# `fetch_verified` 返回字节数）。如果日志走 stdout，返回值就会被日志文本污染，
# 调用方拿到一坨多行字符串，紧接着 `$((size / 1000000))` 就炸：
#     line 371: <整段日志>: syntax error: operand expected
# 约定：**数据走 stdout，日志走 stderr**。新加的输出请沿用这个分界。
log()  { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }
sha256_of() { shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1; }

# —— 定位 cmake ——
# 优先 PATH（你机器上 brew 装的那个）。都没有时按以下顺序回退，因为在受限
# 执行环境里 `brew install cmake` 会失败：
#   warning: unable to unlink '/opt/homebrew/.git/config.lock': Operation not permitted
#   error: could not write config file .git/config: Operation not permitted
# 回退路径一：pip wheel 的**原生布局**（自带完整 cmake + share/,可直接运行）
#   pip install cmake  →  <venv>/lib/pythonX.Y/site-packages/cmake/data/bin/cmake
#   本项目当前就在用这条：$ROOT/.tools/cmakewheel/cmake/data/bin/cmake
# 回退路径二：把官方 cmake-<ver>-macos-universal.tar.gz 解到 .tools/cmake/，
#   使其成为 .tools/cmake/CMake.app/Contents/{bin,share}。注意必须用**拷贝**而
#   不是符号链接——`ln -sfn src dest` 在 dest 已存在时会在 dest 内部建链接。
resolve_cmake() {
  if command -v cmake >/dev/null 2>&1; then command -v cmake; return 0; fi
  local c
  for c in "$ROOT/.tools/cmakewheel/cmake/data/bin/cmake" \
           "$HOME/.workbuddy/binaries/python/envs/default/bin/cmake" \
           "$ROOT/.tools/cmake/CMake.app/Contents/bin/cmake" \
           /Applications/CMake.app/Contents/bin/cmake \
           /opt/homebrew/bin/cmake \
           /usr/local/bin/cmake; do
    [ -x "$c" ] && { echo "$c"; return 0; }
  done
  # 最后兜底：在 .tools 里找任何叫 cmake 的可执行文件（应对解包目录层级不同的情况）
  c="$(find "$ROOT/.tools" -type f -name cmake -perm -u+x 2>/dev/null | head -n1)"
  [ -n "$c" ] && { echo "$c"; return 0; }
  return 1
}
CMAKE="$(resolve_cmake)" || die "找不到 cmake。任选一种：
       brew install cmake
       pip install cmake     # 再把 .../site-packages/cmake/data/bin/cmake 软链到 PATH
       把 cmake-<ver>-macos-universal.tar.gz 解到 $ROOT/.tools/cmake/"
log "cmake: ${CMAKE}（$("$CMAKE" --version | head -1)）"

for tool in git xcodebuild; do
  command -v "$tool" >/dev/null 2>&1 || die "缺少 $tool"
done

[ "$DO_CLEAN" = "1" ] && { log "清理 $VENDOR"; rm -rf "$VENDOR"; }
mkdir -p "$VENDOR"

copy_libs() {  # copy_libs <llama.cpp 构建目录> <目标目录>
  local from="$1" to="$2" count=0 lib f
  mkdir -p "$to"
  # 🔴 先清掉本目录里由 llama.cpp 产出的旧归档 —— 只增不删会留下**上一代构建**的残骸。
  # 实测踩到过：单变体时代的 libggml-cpu.a 留在这里，而它是**未改名**的形态，
  # 里面 `ggml_backend_cpu_reg` 与 libggml.a 里我们自己的 dispatch **同名**。
  # 它会不会被 ld 拉进来取决于符号引用顺序，所以不是每次都炸 —— 是颗定时炸弹：
  # 炸的时候报 "duplicate symbol '_ggml_backend_cpu_reg'"，不炸的时候可能悄悄链进旧代码。
  # 这个目录只归 llama 所有（sherpa 在 ../sherpa-onnx/lib/ 下），所以限定前缀即可。
  rm -f "$to"/libllama*.a "$to"/libggml*.a
  # ⚠️ 不能写成「-maxdepth 2 扫所有 *.a」——llama.cpp 的归档散布在三个不同深度，
  # 那样只会捞到 libllama.a，ggml 整组全丢，最后在 Xcode 里表现为
  #     Undefined symbols: _ggml_*
  # 实测布局（build-<platform>/ 下）：
  #   src/libllama.a                              depth 2
  #   ggml/src/libggml{,-base,-cpu}.a             depth 3
  #   ggml/src/ggml-blas/libggml-blas.a           depth 4
  # 按名字精确取，顺序即链接顺序：被依赖者靠后（llama → ggml → ggml-cpu → ggml-base）。
  for lib in llama ggml ggml-blas ggml-base; do
    f="$(find "$from" -maxdepth 5 -type f -name "lib$lib.a" | head -n1)"
    if [ -n "$f" ]; then
      cp "$f" "$to/"
      count=$((count + 1))
    fi
  done
  # ggml-cpu 现在是**一族**归档（静态多变体）：libggml-cpu-armv8a.a / -dotprod.a / -i8mm.a，
  # 运行时由 native/cpu/vt_cpu_dispatch.c 挑一份。旧的单份 libggml-cpu.a 已不再产出 ——
  # 这里两种形态都收（用 glob 而不是精确名），免得换构建方式时**静默丢库**：
  # 少一个变体的表现是「A8 上要么 SIGILL、要么白扔算力」，而不是编译报错。
  for f in "$from"/ggml/src/libggml-cpu*.a; do
    [ -f "$f" ] || continue
    cp "$f" "$to/"
    count=$((count + 1))
  done
  # 兜底：把上面没点到名、但确实需要的归档也带上（排除 tools/common 的辅助库，
  # 关掉 LLAMA_BUILD_TOOLS/COMMON 后本来也不会有）。
  while IFS= read -r f; do
    case "$(basename "$f")" in
      libllama-common*.a|libmtmd.a|libcpp-httplib.a|libllama-*-impl.a) continue ;;
    esac
    [ -f "$to/$(basename "$f")" ] && continue
    cp "$f" "$to/"
    count=$((count + 1))
  done < <(find "$from" -maxdepth 5 -type f -name '*.a' 2>/dev/null)
  [ "$count" -gt 0 ] || warn "在 $from 下没找到任何 .a 静态库"
  echo "$count"
}

# ---------------------------------------------------------------------------
# 1. sherpa-onnx（Silero VAD + SenseVoice ASR）
# ---------------------------------------------------------------------------
# 官方**有**预编译 iOS xcframework，但挂在名为 `xcframework` 的**滚动 tag** 下，
# 不在 v1.13.6 那个 release 的 283 个资源里 —— 按版本号找 release 是找不到的，
# 官方自己在仓库根 Package.swift 里就是这么引的：
#   .../releases/download/xcframework/sherpa-onnx-v1.13.6-ios-static.xcframework.zip
# `ios-static` 变体是 sherpa 的**静态**归档（17MB），但它**不含 onnxruntime** ——
# 实测 315 个归档成员里没有一个 onnxruntime 的 .o，`_OrtGetApiBase` 全是未定义引用。
# 官方 SwiftPM 另行依赖 csukuangfj/onnxruntime-libs（精确 1.27.1）补上这一块，
# 本脚本照做（见下面的 ORT_* ）。所以这里一共要拿两份包，别以为一份就够。
# 也**不需要**跑官方 build-ios.sh（它用 wget，macOS 默认没有 wget）。
# 默认走预编译包（约 1 分钟）；`--from-source` 才走源码构建（10~25 分钟）。
SHERPA_PREBUILT_FILE="sherpa-onnx-$SHERPA_TAG-ios-static.xcframework.zip"
SHERPA_PREBUILT_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/xcframework/$SHERPA_PREBUILT_FILE"
SHERPA_MIN_ZIP_BYTES=15000000     # 该包约 17MB，不足 15MB 视为没下完

# 该包的 SHA-256，**抄自 GitHub API 的 assets[].digest 字段**（不是我们自己算的）：
#   curl -s https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/tags/xcframework
#   → assets[?].digest == "sha256:0b8c8803…"
# 有了它就能安全地走**镜像/代理**：GitHub release 走 S3，直连在本机沙箱里时通时断
# （实测 502 与 http=000 交替），而代理可达性明显更好。下载后一律用本哈希校验，
# 校验不过就换下一个源 —— 所以「用镜像」不会牺牲完整性。
# 升级 SHERPA_TAG 时必须同步更新这个值（脚本会拿 API 的 digest 自动交叉核对）。
SHERPA_PREBUILT_SHA256="0b8c880357e653af18c5f9c6e8b3c045e85b98403c00ff25692a1261d31aa332"
SHERPA_RELEASE_API="https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/tags/xcframework"

# —— ⚠️ sherpa 的 ios-static 包**不含 onnxruntime** ——
# 这是实测踩出来的：`nm -u libsherpa-onnx-c-api.a | grep OrtGetApiBase` 全是 `U`
# （未定义引用），全量 315 个归档成员里没有一个 onnxruntime 的 .o。
# 直接链会报：
#     Undefined symbols: _OrtGetApiBase
#     Undefined symbols: _OrtSessionOptionsAppendExecutionProvider_CoreML
# 官方 SwiftPM 的 `Package.swift` 印证了这一点 —— 它对 ios-static 只声明 sherpa
# 本体，另外**单独依赖**一个包并精确锁版本：
#     .package(url: "https://github.com/csukuangfj/onnxruntime-libs", exact: "1.27.1")
#     .product(name: "onnxruntime-ios", …)   ← 这一步就是我们缺的
# 所以必须把 onnxruntime 的 iOS **静态** xcframework 也拉下来一起链。
# 三个 iOS 变体的区别（别选错）：
#     ios-shared                          6.2MB  sherpa 动态，不含 onnxruntime
#     ios-static                         17.0MB  sherpa 静态，不含 onnxruntime   ← 我们要的
#     ios-shared-onnxruntime-static      31.0MB  sherpa 动态 + onnxruntime 静态
# 注意 ios-static 的 checksum 与官方 Package.swift 里写的**逐字符一致**，可交叉验证。
ORT_TAG="1.27.1"
ORT_PREBUILT_FILE="onnxruntime-ios-static-xcframework-$ORT_TAG.xcframework.zip"
ORT_PREBUILT_URL="https://github.com/csukuangfj/onnxruntime-libs/releases/download/v$ORT_TAG/$ORT_PREBUILT_FILE"
ORT_PREBUILT_SHA256="985deaff345c7bcfbe4979b2daeec09d7a745b1e9cb73f37f4077364eb578e62"
ORT_RELEASE_API="https://api.github.com/repos/csukuangfj/onnxruntime-libs/releases/tags/v$ORT_TAG"
ORT_MIN_ZIP_BYTES=32000000          # 该包 32.8MB，不足 32MB 视为没下完

# 下载源，按顺序尝试。第一个是官方直连；其余是公开的 GitHub 加速镜像。
# 注意：绝不把镜像的成功结果直接当真 —— 只有 SHA-256 与官方 digest 相等才接受。
sherpa_mirror_urls() {
  echo "$SHERPA_PREBUILT_URL"
  local m
  for m in "https://gh-proxy.com/" "https://ghfast.top/" "https://ghproxy.net/"; do
    echo "${m}${SHERPA_PREBUILT_URL}"
  done
}

onnxruntime_mirror_urls() {
  echo "$ORT_PREBUILT_URL"
  local m
  for m in "https://gh-proxy.com/" "https://ghfast.top/" "https://ghproxy.net/"; do
    echo "${m}${ORT_PREBUILT_URL}"
  done
}

# 定位切片目录：预编译包与源码构建的布局都是 ios-<arch>[-<arch>]-<platform>
sherpa_slice_for() {  # <xcframework> <platform>
  local xcf="$1" platform="$2" want d
  case "$platform" in
    iphoneos)        want="ios-arm64" ;;
    iphonesimulator) want="ios-arm64_x86_64-simulator" ;;
  esac
  [ -d "$xcf/$want" ] && { echo "$xcf/$want"; return 0; }
  # 兜底：按名字里有没有 simulator 过滤，不依赖精确的 arch 组合。
  if [ "$platform" = "iphonesimulator" ]; then
    d="$(find "$xcf" -maxdepth 1 -type d -iname '*simulator*' | head -n1)"
  else
    d="$(find "$xcf" -maxdepth 1 -type d -iname 'ios-*' ! -iname '*simulator*' | head -n1)"
  fi
  [ -n "$d" ] && echo "$d"
}

# copy_static_libs <切片目录> <目标目录> <framework 内二进制名> <重命名成>
#
# 两种布局都覆盖：
#   布局 A（onnxruntime 的某些构建 / 源码构建产物）：切片下直接是若干 *.a
#   布局 B（sherpa 与 onnxruntime 的官方预编译包）：静态 framework ——
#       <slice>/<Name>.framework/<Name> 本身就是一个归档，只是没有 .a 扩展名。
#       链接器只看文件内容不看扩展名，所以拷出来按 <重命名成> 存即可。
#   实测 sherpa   → <slice>/SherpaOnnxC.framework/SherpaOnnxC
#        onnxruntime → <slice>/onnxruntime.framework/onnxruntime
#   两份包的 framework 名字不同，所以不能写死成 SherpaOnnxC（早期版本就是这么
#   写死的，会导致 onnxruntime 一个库都拷不出来、静默返回 0）。
copy_static_libs() {
  local from="$1" to="$2" fwbin="$3" rename="$4" count=0 f bin
  mkdir -p "$to"
  while IFS= read -r -d '' f; do
    cp "$f" "$to/"; count=$((count + 1))
  done < <(find "$from" -maxdepth 2 -name '*.a' -print0)
  if [ "$count" = "0" ] && [ -n "$fwbin" ]; then
    bin="$(find "$from" -maxdepth 3 -type f -name "$fwbin" | head -n1)"
    if [ -n "$bin" ]; then
      cp "$bin" "$to/$rename"
      count=1
    fi
  fi
  [ "$count" -gt 0 ] || warn "  在 $from 下没找到静态库"
  echo "$count"
}

# 拿 GitHub API 上的官方 digest，用来交叉核对脚本里 pin 的 SHERPA_PREBUILT_SHA256。
# 拿不到（网络/限流/本机没 python3）不算失败，只是跳过这一层核对 ——
# 真正的把关还是下载后的哈希比对。
release_api_digest() {  # <release api url> <asset 文件名>
  command -v python3 >/dev/null 2>&1 || return 0
  curl -s --max-time 30 "$1" 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
want = sys.argv[1]
for a in d.get("assets", []):
    if a.get("name") == want:
        print((a.get("digest") or "").replace("sha256:", ""))
        break
' "$2" 2>/dev/null
}

# fetch_verified <文件名> <release api url> <pin 的 sha256> <镜像列表函数名> <最小字节>
#
# 一个包的处理流程：核对 pin →（失败才）多源下载 → 哈希校验 → 解包。
# 哈希是唯一的通过标准；体积只用来挡「明显没下完」。
fetch_verified() {
  local file="$1" api="$2" pin="$3" urls_fn="$4" min_bytes="$5"
  local zip="$CACHE/$file"
  local size got url attempt api_digest

  # 先拿官方 digest 交叉核对 pin 值。不一致说明上游 tag 升了但哈希忘了改，
  # 这时**以 API 为准**并告警 —— 否则会拿新包比旧哈希，永远校验不过。
  api_digest="$(release_api_digest "$api" "$file")"
  if [ -n "$api_digest" ]; then
    if [ "$api_digest" != "$pin" ]; then
      warn "  GitHub API 的 digest 与脚本里 pin 的不一致，以 API 为准"
      warn "    API : $api_digest"
      warn "    pin : $pin   ← 请更新脚本里对应的 *_SHA256 常量"
      pin="$api_digest"
    else
      log "  官方 digest 核对通过（与脚本 pin 值一致）"
    fi
  else
    warn "  取不到官方 digest（网络或限流），只做下载后的哈希校验"
  fi

  size="$(stat -f%z "$zip" 2>/dev/null || echo 0)"
  if [ "$(sha256_of "$zip")" != "$pin" ] || [ "$size" -lt "$min_bytes" ]; then
    # 两重循环：**外层是轮次，内层是源**。
    # 为什么不是「一个源一直重试到底」—— 网络窗口是间歇的，死磕一个源可能白等半小时；
    # 多轮遍历能让每一轮都从**当前进度**出发，换到哪个源有窗口就吃哪个。
    local round src
    for round in 1 2 3; do
      src=0
      while IFS= read -r url; do
        [ -n "$url" ] || continue
        src=$((src + 1))
        attempt=$((attempt + 1))
        log "  下载 ${file}（第 ${round} 轮 · 源 ${src}）"
        # GitHub release 走 S3，**支持 Range**，所以中断可续传；各镜像服务的是同一份
        # 字节，因此同一个文件在不同源之间接着下也没问题。
        #
        # --speed-limit/--speed-time：低于 64 KB/s 持续 20 秒就**主动放弃这个源**。
        # 没有这一条会踩到一个坑：慢但没报错的源会一直占着位置，轮不到后面的快镜像。
        # 实测同一个 32.8MB 资源在某一刻的对比 ——
        #     github.com 直连      4.5 KB/s   （要 2 小时）
        #     gh-proxy.com 前缀    928 KB/s   （35 秒）
        #     ghfast.top 前缀      300 KB/s
        #     ghproxy.net 前缀      36 KB/s
        # 差了两个数量级；加了速度下限才能自动弃慢投快。
        # 官方的源排在第一（先信任官方）；它慢的时候由速度下限把机会让给镜像。
        # 用镜像不会牺牲完整性 —— 无论如何都要过下面的哈希校验。
        curl -fSL -C - --retry 3 --retry-delay 4 --retry-all-errors \
          --speed-limit 65536 --speed-time 20 \
          --max-time 1800 -o "$zip" "$url" 2>/dev/null || true
        size="$(stat -f%z "$zip" 2>/dev/null || echo 0)"
        got="$(sha256_of "$zip")"
        if [ "$got" = "$pin" ]; then
          log "  ✅ 哈希校验通过（$((size / 1000000)) MB，第 ${round} 轮 · 源 ${src}）"
          break 2
        fi
        warn "  源 ${src} 未成功（$size 字节），换下一个"
      done < <("$urls_fn")
    done
  fi

  size="$(stat -f%z "$zip" 2>/dev/null || echo 0)"
  got="$(sha256_of "$zip")"
  [ "$got" = "$pin" ] || {
    warn "  $file 未通过校验（$size 字节）"
    warn "    实际: $got"
    warn "    期望: $pin"
    return 1
  }
  echo "$size"
}

fetch_sherpa_prebuilt() {
  local dir="$VENDOR/sherpa-onnx" size
  [ -d "$dir/sherpa-onnx.xcframework" ] && { log "  已有解包好的 sherpa xcframework，跳过"; return 0; }

  size="$(fetch_verified "$SHERPA_PREBUILT_FILE" "$SHERPA_RELEASE_API" \
            "$SHERPA_PREBUILT_SHA256" sherpa_mirror_urls "$SHERPA_MIN_ZIP_BYTES")" || return 1

  log "  解包 sherpa（$((size / 1000000)) MB）"
  mkdir -p "$dir"
  unzip -q -o "$CACHE/$SHERPA_PREBUILT_FILE" -d "$dir" || { warn "  解包失败"; return 1; }
  [ -d "$dir/sherpa-onnx.xcframework" ] || { warn "  解包后未找到 sherpa-onnx.xcframework"; return 1; }
  return 0
}

# onnxruntime 是 sherpa ios-static 包缺的那一块（详见上面 ORT_* 常量的注释）。
fetch_onnxruntime_prebuilt() {
  local dir="$VENDOR/sherpa-onnx" size
  [ -d "$dir/onnxruntime.xcframework" ] && { log "  已有解包好的 onnxruntime xcframework，跳过"; return 0; }

  size="$(fetch_verified "$ORT_PREBUILT_FILE" "$ORT_RELEASE_API" \
            "$ORT_PREBUILT_SHA256" onnxruntime_mirror_urls "$ORT_MIN_ZIP_BYTES")" || return 1

  log "  解包 onnxruntime（$((size / 1000000)) MB）"
  mkdir -p "$dir"
  unzip -q -o "$CACHE/$ORT_PREBUILT_FILE" -d "$dir" || { warn "  解包失败"; return 1; }
  [ -d "$dir/onnxruntime.xcframework" ] || { warn "  解包后未找到 onnxruntime.xcframework"; return 1; }
  return 0
}

build_sherpa_from_source() {
  log "从源码构建 sherpa-onnx ${SHERPA_TAG}（这一步最慢，10~25 分钟）"
  local src="$VENDOR/src/sherpa-onnx"
  if [ ! -d "$src/.git" ]; then
    mkdir -p "$VENDOR/src"
    git clone --depth 1 --branch "$SHERPA_TAG" https://github.com/k2-fsa/sherpa-onnx.git "$src"
  fi

  # 官方 build-ios.sh 用 `wget -c` 拉 onnxruntime，而 macOS 默认不带 wget。
  # 注入一个只支持 `wget -c <url>` 这一种形式的最小垫片，转发给 curl（curl 支持
  # Range，所以 -c 的续传语义仍然成立）。
  if ! command -v wget >/dev/null 2>&1; then
    local shim="$VENDOR/bin"
    mkdir -p "$shim"
    cat > "$shim/wget" <<'WEOF'
#!/bin/sh
# 最小 wget 垫片，仅服务 sherpa-onnx 的 build-ios.sh（它只写 `wget -c <url>`）。
url=""
for a in "$@"; do case "$a" in -*) ;; *) url="$a" ;; esac; done
[ -n "$url" ] || { echo "wget shim: 没解析到 URL" >&2; exit 1; }
exec curl -fSL -C - --retry 8 --retry-delay 5 --retry-all-errors -O "$url"
WEOF
    chmod +x "$shim/wget"
    PATH="$shim:$PATH"; export PATH
    log "  本机没有 wget，已注入垫片：$shim/wget → curl"
  fi

  ( cd "$src" && ./build-ios.sh ) || die "sherpa-onnx 的 build-ios.sh 失败，请查看其输出"
}

stage_sherpa() {
  local xcf="" cand
  for cand in "$VENDOR/sherpa-onnx/sherpa-onnx.xcframework" \
              "$VENDOR/src/sherpa-onnx/build-ios/sherpa-onnx.xcframework"; do
    [ -d "$cand" ] && { xcf="$cand"; break; }
  done
  [ -n "$xcf" ] || die "未找到 sherpa-onnx.xcframework（预编译包与源码构建产物都没有）"
  log "  使用 $xcf"

  # —— 头文件 ——
  # Bridge 里写的是 #include <sherpa-onnx/c-api/c-api.h>，所以要把含 sherpa-onnx/
  # 的那一级整体拷到 include/ 下。预编译包是 framework 布局
  # （<slice>/SherpaOnnxC.framework/Headers/sherpa-onnx/c-api/c-api.h），
  # 源码构建早期版本是 <slice>/Headers/sherpa-onnx/c-api/c-api.h，两种都覆盖。
  local hdr api_dir sherpa_dir
  hdr="$(find "$xcf" -maxdepth 8 -name 'c-api.h' -path '*sherpa-onnx/c-api*' | head -n1)"
  [ -n "$hdr" ] || die "xcframework 里找不到 sherpa-onnx/c-api/c-api.h"
  api_dir="$(dirname "$hdr")"        # …/sherpa-onnx/c-api
  sherpa_dir="$(dirname "$api_dir")" # …/sherpa-onnx
  mkdir -p "$VENDOR/sherpa-onnx/include"
  cp -R "$sherpa_dir" "$VENDOR/sherpa-onnx/include/" || die "头文件拷贝失败"
  log "  头文件：$(basename "$sherpa_dir")/ → include/"

  # —— 静态库 ——
  local platform slice n
  for platform in "${PLATFORMS[@]}"; do
    slice="$(sherpa_slice_for "$xcf" "$platform")"
    if [ -z "$slice" ]; then
      warn "  $platform 切片缺失，跳过"
      continue
    fi
    n="$(copy_static_libs "$slice" "$VENDOR/sherpa-onnx/lib/$platform" SherpaOnnxC libsherpa-onnx-c-api.a | tail -n1)"
    log "  ${platform}：$n 个静态库（$(basename "$slice")）"
  done

  # —— onnxruntime 的静态库 ——
  # sherpa 的 C API 引用 `_OrtGetApiBase` 与
  # `_OrtSessionOptionsAppendExecutionProvider_CoreML`，这两个只有 onnxruntime 提供。
  # 少了它链接期就是 Undefined symbols（实测踩过）。
  stage_onnxruntime
}

stage_onnxruntime() {
  local xcf="$VENDOR/sherpa-onnx/onnxruntime.xcframework"
  [ -d "$xcf" ] || die "未找到 onnxruntime.xcframework（sherpa 的 ios-static 包不自带它，必须另外下）"
  log "  使用 $xcf"

  local platform slice n
  for platform in "${PLATFORMS[@]}"; do
    slice="$(sherpa_slice_for "$xcf" "$platform")"
    if [ -z "$slice" ]; then
      warn "  onnxruntime $platform 切片缺失，跳过"
      continue
    fi
    # onnxruntime 的框架里可能叫 libonnxruntime.a，也可能是没有扩展名的静态
    # framework 可执行文件 —— 两种都归到 libonnxruntime.a。
    n="$(copy_static_libs "$slice" "$VENDOR/sherpa-onnx/lib/$platform" onnxruntime libonnxruntime.a | tail -n1)"
    log "  onnxruntime ${platform}：$n 个静态库（$(basename "$slice")）"
  done
}

build_sherpa() {
  if [ "$SHERPA_FROM_SOURCE" = "1" ]; then
    build_sherpa_from_source
  else
    # ⚠️ 这里**故意不做自动回退**。早先的版本是「预编译包失败就自动转源码构建」，
    # 但那条路要额外拉 onnxruntime（十几到几十 MB）并编译 onnxruntime +
    # protobuf + kaldi-fst 全套，网络不好时它同样会失败——用户白等 10~25 分钟
    # 才拿到一个更靠后的报错。改成快速失败：zip 支持 Range 续传，网络窗口开了
    # 重跑本脚本即可接着下。
    fetch_sherpa_prebuilt || die "sherpa-onnx 预编译包没拿到。
       · 重跑本脚本会从断点续传（GitHub release 支持 Range）；
       · 若长期拉不动，可显式改走源码构建：scripts/setup-ios-deps.sh --sherpa-only --from-source
       · 或手工下载后放到 $CACHE/$SHERPA_PREBUILT_FILE
         $SHERPA_PREBUILT_URL"
    # 还有第二份包 —— sherpa 的 ios-static 不含 onnxruntime，必须单独取。
    fetch_onnxruntime_prebuilt || die "onnxruntime 预编译包没拿到（sherpa 的静态库依赖它的符号）。
       · 重跑本脚本会从断点续传；
       · 或手工下载后放到 $CACHE/$ORT_PREBUILT_FILE
         $ORT_PREBUILT_URL"
  fi
  stage_sherpa
}

# ---------------------------------------------------------------------------
# 2. llama.cpp（Qwen2.5 / TranslateGemma GGUF 推理）
# ---------------------------------------------------------------------------
build_llama() {
  log "构建 llama.cpp @ ${LLAMA_COMMIT:0:12}（与 Android 侧同源）"
  local src="$VENDOR/src/llama.cpp"
  if [ ! -f "$src/include/llama.h" ]; then
    mkdir -p "$CACHE" "$VENDOR/src"
    local tarball="$CACHE/llama.tar.gz"
    # 缓存只信哈希、不信存在性：Android 侧 bootstrap.py 用的是同一个文件名，
    # 那边下过就直接复用，省一次 ~40MB 下载。
    if [ "$(sha256_of "$tarball")" != "$LLAMA_TARBALL_SHA256" ]; then
      log "  下载源码包（codeload 不支持 Range，用 --retry 兜住中断）"
      curl -fSL --retry 5 --retry-delay 3 --retry-all-errors \
        -o "$tarball.part" "$LLAMA_TARBALL_URL" \
        || die "下载失败。可手工下载后放到 $tarball 再重跑本脚本"
      mv "$tarball.part" "$tarball"
    fi
    local got; got="$(sha256_of "$tarball")"
    [ "$got" = "$LLAMA_TARBALL_SHA256" ] || die "源码包 SHA-256 不匹配，拒绝使用
      实际: $got
      期望: $LLAMA_TARBALL_SHA256
      → 与 Android 侧版本不同源会破坏跨平台译文一致性，故直接中止。"
    log "  源码包 SHA-256 校验通过（与 Android 侧一致）"

    tar -xzf "$tarball" -C "$VENDOR/src"
    local freshly="$VENDOR/src/llama.cpp-$LLAMA_COMMIT"
    [ -d "$freshly" ] || die "解包后未找到 llama.cpp-$LLAMA_COMMIT 目录"
    if [ -d "$src" ]; then
      # 上次中断留下的残壳。不直接删（避免误删），改名让位并提示路径。
      local stash="$src.stale-$(date +%Y%m%d%H%M%S)"
      mv "$src" "$stash"
      warn "已有不完整的 ${src}，已改名保留为 ${stash}（确认后可自行删除）"
    fi
    mv "$freshly" "$src"
  fi

  local platform sysroot
  for platform in "${PLATFORMS[@]}"; do
    sysroot="$(xcrun --sdk "$platform" --show-sdk-path)"
    log "  构建多变体 ggml-cpu（${platform}，sysroot=${sysroot}）"
    # —— 为什么转手 build-ggml-cpu-variants.sh，而不是在这里直接 cmake ——
    # A8（iPad mini 4，无 dotprod）与 A13+（iPhone 11 起）对指令集的要求是**互斥**的：
    # 用 dotprod 编的代码在 A8 上 SIGILL，用 armv8-a 编的在 A18 上白扔 2~5 倍算力。
    # 上游的 GGML_CPU_ALL_VARIANTS 走 GGML_BACKEND_DL → BUILD_SHARED_LIBS，会把
    # llama/ggml 全变成 dylib，动到 iOS 的打包与签名链路，**不能**用。
    # 那个脚本走静态路线：N 份 libggml-cpu-<tag>.a，非选中变体的全局符号加 vtv<i>_ 前缀，
    # 运行时由 native/cpu/vt_cpu_dispatch.c 按 CPU **实际**特性挑一份。
    #
    # 它内部已经带上下面这些 iOS 专用开关，语义与原先这条 cmake 调用完全一致：
    #   · GGML_BLAS=OFF —— cblas_sgemm 头文件标注 iOS 16.4 才引入，符号 weak，
    #     iOS 15 上运行期解引用 NULL 直接崩；
    #   · GGML_ACCELERATE=ON —— 只调 vDSP_*（iOS 4+），是真提速，保留。
    # 编完还会跑四道门禁，其中「基线变体归档里 0 条 sdot/udot/smmla/ummla」
    # 就是「能在 iPad mini 4 上跑」的**静态**判据。
    #
    # 顺带说明那批 LLAMA_BUILD_* / GGML_BUILD_*=OFF 为什么必须关：它们的默认值都是 ON
    # （LLAMA_STANDALONE 默认 ON），会把 tools/、app/、common/ 全编一遍。iOS 上这些
    # 可执行文件毫无用处，而且 app/llama.cpp:31 那句 `system("curl ... | sh")` 会直接
    # **编译失败**（`error: 'system' is unavailable: not available on iOS`），
    # 结果是整趟构建跑到 99% 才炸。不关还会让 copy_libs 把 libllama-common.a /
    # libmtmd.a 也捞进来，徒增体积且有重复符号风险。
    #
    # ⚠️ 构建目录换成了 build-<platform>-variants。旧的 build-<platform>/ 是
    #    单变体时代的遗留物，内容已完全被覆盖，可自行删除（省几百 MB）。
    bash "$ROOT/scripts/build-ggml-cpu-variants.sh" --platform "$platform" \
      || die "多变体 ggml-cpu 构建或门禁失败（${platform}）"
    local n
    n="$(copy_libs "$src/build-$platform-variants" "$VENDOR/llama/lib/$platform" | tail -n1)"
    log "  ${platform}：$n 个静态库"
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
  # 链接顺序：被依赖者放后面（ld 按出现顺序解析，靠后的归档才能补上前面欠的符号）。
  #   libsherpa-onnx-c-api.a 依赖 onnxruntime → onnxruntime 必须排在它**后面**
  #   libllama.a 依赖 ggml-*，ggml 依赖 ggml-base
  # 注意：sherpa 与 llama 是两个互不相干的库，各自成组即可；
  # 这里把顺序写死是为了**不依赖 glob 的字母序** —— 字母序下 libonnxruntime.a
  # 恰好排在 libsherpa-onnx-c-api.a 前面，正好是错的。
  #
  # 🔴 ggml-cpu 那一族（多变体）必须**夹在 libggml.a 与 libggml-base.a 之间**，
  #    这条顺序是硬约束，不是风格问题：
  #      · libggml.a 里是我们自己的 dispatch（vt_cpu_dispatch.c），它引用
  #        vtv<i>_ggml_backend_cpu_reg → 变体归档必须在它**后面**；
  #      · 变体归档引用 ggml-base 的符号 → libggml-base.a 必须在变体**后面**。
  #    用「兜底循环」把它们排到最后会直接 Undefined symbols: _vtv0_ggml_backend_cpu_reg。
  #    另外它们的字母序（armv8a < dotprod < i8mm）恰好无所谓 —— 三者互不引用，
  #    谁先谁后都能解析。
  local emitted=" "
  local name f base
  for name in sherpa-onnx-c-api onnxruntime llama ggml ggml-metal ggml-blas; do
    f="$dir/lib$name.a"
    if [ -f "$f" ]; then
      printf ' $(SRCROOT)/%s/lib%s.a' "$prefix" "$name"
      emitted="$emitted$name "
    fi
  done
  # ggml-cpu 族：兼容「单份 libggml-cpu.a」与「多变体 libggml-cpu-<tag>.a」两种形态。
  for f in "$dir"/libggml-cpu*.a; do
    [ -e "$f" ] || continue
    printf ' $(SRCROOT)/%s/%s' "$prefix" "$(basename "$f")"
    base="$(basename "$f" .a)"
    emitted="$emitted${base#lib} "
  done
  f="$dir/libggml-base.a"
  if [ -f "$f" ]; then
    printf ' $(SRCROOT)/%s/libggml-base.a' "$prefix"
    emitted="$emitted""ggml-base "
  fi
  # 其余库补在后面（例如 sherpa 的一堆 fst/onnxruntime）。
  for f in "$dir"/*.a; do
    [ -e "$f" ] || continue
    base="$(basename "$f" .a)"
    case "$emitted" in *" ${base#lib} "*) continue ;; esac
    printf ' $(SRCROOT)/%s/%s' "$prefix" "$(basename "$f")"
    emitted="$emitted${base#lib} "
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
    # —— 编译期宏：静态链接 ORT 时必须定义 VT_ORT_STATIC ——
    #
    # 🔴 漏掉这一个宏的失败形态是「**慢**」，不是「报错」，所以特别难发现：
    #    native/nmt/vt_ort.h 的 OrtApiBaseHandle() 有三分支，只有走到
    #    `#elif defined(VT_ORT_STATIC)` 才会调 OrtGetApiBase()。宏不存在就落到
    #    最后的 `return nullptr` —— 于是 vt::Api() 恒为 nullptr、
    #    VTOpusNmt.isAvailable() 恒为 NO、OpusTranslationEngine.init 抛
    #    nativeUnavailable，再被 Pipeline 静默回落成通用模型。
    #    工程照样编译、链接、启动（libonnxruntime.a 是被整体链进去的，
    #    所以 `nm` 在最终 binary 里还能看到一堆 Ort* 符号 —— **符号在 ≠ 宏在**），
    #    只有「中译日/中英互译不再走专用引擎」这一个可观察后果。
    #    判据只能看**编译单元**：VTOpusNmt.o 必须有 `U _OrtGetApiBase`。
    #    验证脚本：scripts/verify-opus-linkage.py（build-ios.sh 每次构建都跑）。
    #
    # 只在**每个平台**都真的有 libonnxruntime.a 时才定义 —— 只在其中一个平台有的话，
    # 另一个平台会变成「宏定义了但库不在」→ 链接期 Undefined symbols: _OrtGetApiBase。
    local ort_everywhere=1
    for platform in "${PLATFORMS[@]}"; do
      [ -f "$VENDOR/sherpa-onnx/lib/$platform/libonnxruntime.a" ] || ort_everywhere=0
    done
    if [ "$ort_everywhere" = "1" ]; then
      echo "// 静态链接 libonnxruntime.a → 打开 native/nmt/vt_ort.h 的 OrtGetApiBase 分支。"
      echo "// 删掉这一行不会编译失败，只会让「智能选择」永远静默回落成通用模型。"
      echo "VT_DEPS_DEFINES = VT_ORT_STATIC"
    else
      echo "// 未在各平台都找到 libonnxruntime.a → 不定义 VT_ORT_STATIC（vt_ort.h 走 nullptr 分支）。"
    fi
    echo
    # —— 系统 framework ——
    # libggml-cpu.a 在 GGML_ACCELERATE=ON 下有 10 个 `_vDSP_*` 未定义符号
    # （vDSP_vadd / vDSP_sve / vDSP_vsmsa / …），它们只由 Accelerate.framework 提供。
    # 工程里没有任何显式的 framework 链接阶段（PBXFrameworksBuildPhase 是空的），
    # 所以这一条必须由本文件注入，否则 Xcode 链接期报
    #     Undefined symbols: _vDSP_vadd
    # 实测：除这 10 个之外，4 个归档的其余未定义符号全部来自 libSystem/libc++
    # 或归档之间互相引用，无需其他 framework。
    echo "// Accelerate —— 提供 libggml-cpu.a 里的 vDSP_* 符号（GGML_ACCELERATE=ON）。"
    echo "VT_DEPS_SYSTEM_LDFLAGS = -framework Accelerate"
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
  - "Undefined symbols: _vtv0_ggml_backend_cpu_reg" / _vtv1_… / _vtv2_…
                                   → 多变体归档没被链上，或被排到了 libggml-base.a 后面。
                                     见 emit_archives 里那段顺序硬约束（必须夹在
                                     libggml.a 与 libggml-base.a 之间）；
  - "duplicate symbol"             → 见 Deps.xcconfig 末尾的注释；
  - 模拟器用 arm64 切片；x86_64 模拟器需要额外构建，一般不必。
EOF
