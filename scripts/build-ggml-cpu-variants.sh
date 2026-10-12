#!/usr/bin/env bash
#
# build-ggml-cpu-variants.sh —— 编出「静态多变体」的 ggml CPU 后端并做门禁。
#
# 为什么是两遍
# ------------
# 变体的符号前缀清单不是写死的，而是**从编出来的基线归档里用 nm 反推**的
# （见 scripts/vt-cpu-variants.py 的说明）。所以要先把「只有基线变体、且不加前缀」
# 的那一份编出来当符号捐献者，生成 vt_ns_<tag>.h 之后再重配一次，把前缀以 -include
# 注入各变体的每个 TU。第二遍只重编 ggml-cpu 那几个目标，ggml-base 不动。
#
# ⚠️ 第一遍**只能有一个变体**：多变体又不加前缀，链接期就是 duplicate symbol。
#
# 产物
# ----
#   <build>/ggml/src/libggml-cpu-<tag>.a     每个变体一份
#   <nsdir>/vt_ns_<tag>.h                    该变体的符号前缀定义
#   <nsdir>/vt_cpu_variants.h                运行时调度表
#   <nsdir>/vt_cpu_variants.json             门禁机读报告
#
# 门禁（任一不过就非零退出，除非 --keep-going）
# -------------------------------------------
#   1. 任意两个变体归档的「已定义全局符号」交集为空
#      —— 否则 App 链接期报 duplicate symbol
#   2. 未改名形态的 ggml_backend_cpu_reg 只许由 dispatch 提供，变体里一个都不许有
#   3. 基线变体归档里 **0 条** sdot/udot/smmla/ummla
#      —— 「能在 iPad mini 4（A8，无 dotprod/i8mm）上跑」的静态判据：指令都不存在，
#         就不存在「跑到一半 SIGILL」的可能
#   4. 声明了 DOTPROD/MATMUL_INT8 的变体里**必须**出现对应指令
#      —— 反过来的判据。少了它，一个「编出来但根本没提速」的变体也会静默通过
#
# 用法
#   bash scripts/build-ggml-cpu-variants.sh --platform macos
#   bash scripts/build-ggml-cpu-variants.sh --platform iphonesimulator
#   bash scripts/build-ggml-cpu-variants.sh --platform iphoneos --out ios/third_party/llama/lib/iphoneos
#

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python3}"

PLATFORM="macos"
VENDOR="$ROOT/ios/third_party"
SRC="$VENDOR/src/llama.cpp"
BUILD=""
OUT=""
NSDIR=""
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
# 变体表：tag[:FEAT] 用 | 连接。基线恒排第一（下标 0 是永久兜底）。
VARIANT_SPEC="armv8a|dotprod:DOTPROD|i8mm:DOTPROD,MATMUL_INT8"
KEEP_GOING=0

while [ $# -gt 0 ]; do
  case "$1" in
    --platform)   PLATFORM="$2"; shift 2 ;;
    --src)        SRC="$2"; shift 2 ;;
    --build)      BUILD="$2"; shift 2 ;;
    --out)        OUT="$2"; shift 2 ;;
    --nsdir)      NSDIR="$2"; shift 2 ;;
    --variants)   VARIANT_SPEC="$2"; shift 2 ;;
    --jobs)       JOBS="$2"; shift 2 ;;
    --keep-going) KEEP_GOING=1; shift ;;
    -h|--help)    sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[1;36m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }

resolve_cmake() {
  if command -v cmake >/dev/null 2>&1; then command -v cmake; return 0; fi
  local c
  for c in "$ROOT/.tools/cmakewheel/cmake/data/bin/cmake" \
           "$HOME/.workbuddy/binaries/python/envs/default/bin/cmake" \
           "$ROOT/.tools/cmake/CMake.app/Contents/bin/cmake" \
           /Applications/CMake.app/Contents/bin/cmake \
           /opt/homebrew/bin/cmake /usr/local/bin/cmake; do
    [ -x "$c" ] && { echo "$c"; return 0; }
  done
  c="$(find "$ROOT/.tools" -type f -name cmake -perm -u+x 2>/dev/null | head -n1)"
  [ -n "$c" ] && { echo "$c"; return 0; }
  return 1
}

CMAKE="$(resolve_cmake)" || die "找不到 cmake（.tools/cmakewheel 下也没有）"

[ -f "$SRC/ggml/CMakeLists.txt" ] || die "找不到 llama.cpp 源码：$SRC"
[ -f "$ROOT/native/cpu/vt_cpu_dispatch.c" ] || die "缺少 native/cpu/vt_cpu_dispatch.c"
command -v otool >/dev/null 2>&1 || die "需要 otool（Xcode 命令行工具）做指令集门禁"

case "$PLATFORM" in
  macos)          BUILD="${BUILD:-$ROOT/.tools/cpu-variants/build-macos}" ;;
  iphoneos|iphonesimulator)
                  BUILD="${BUILD:-$SRC/build-$PLATFORM-variants}" ;;
  *) die "不支持的 platform：${PLATFORM}（macos / iphoneos / iphonesimulator）" ;;
esac
NSDIR="${NSDIR:-$ROOT/.tools/cpu-variants/ns-$PLATFORM}"
LOGDIR="$(dirname "$BUILD")"
mkdir -p "$LOGDIR" "$NSDIR"
LOGDIR="$(cd "$LOGDIR" && pwd)"

IFS='|' read -r -a VT_ENTRIES <<< "$VARIANT_SPEC"
VT_TAGS=()
for e in "${VT_ENTRIES[@]}"; do
  VT_TAGS+=("${e%%:*}")
done
BASE_TAG="${VT_TAGS[0]}"
[ "${#VT_TAGS[@]}" -ge 1 ] || die "变体表为空"

log "平台=$PLATFORM   变体=${VT_TAGS[*]}   基线=$BASE_TAG"
log "构建目录=$BUILD"
log "前缀头目录=$NSDIR"

# ---------------------------------------------------------------------------
# 0. 确认补丁在位
# ---------------------------------------------------------------------------
if ! "$PY" "$ROOT/scripts/patch-llama-cpu-variants.py" --llama "$SRC" --check >/dev/null 2>&1; then
  log "补丁未应用或不全 → 执行 --apply"
  "$PY" "$ROOT/scripts/patch-llama-cpu-variants.py" --llama "$SRC" --apply
fi

# ---------------------------------------------------------------------------
# 1. 公共 cmake 参数
# ---------------------------------------------------------------------------
# ⚠️ 与 scripts/setup-ios-deps.sh 的 build_llama 必须保持一致，尤其这两条：
#    · GGML_BLAS=OFF —— cblas_sgemm 头文件标注 iOS 16.4 才引入，符号 weak，
#      iOS 15 上运行期解引用 NULL 直接崩；
#    · GGML_ACCELERATE=ON —— 只调 vDSP_*（iOS 4+），是真提速，保留。
cmake_args=(
  -DCMAKE_BUILD_TYPE=Release
  -DBUILD_SHARED_LIBS=OFF
  -DLLAMA_BUILD_COMMON=OFF
  -DLLAMA_BUILD_TESTS=OFF
  -DLLAMA_BUILD_TOOLS=OFF
  -DLLAMA_BUILD_EXAMPLES=OFF
  -DLLAMA_BUILD_SERVER=OFF
  -DLLAMA_BUILD_APP=OFF
  -DGGML_BUILD_TESTS=OFF
  -DGGML_BUILD_EXAMPLES=OFF
  -DGGML_METAL=OFF
  -DGGML_BLAS=OFF
  -DGGML_ACCELERATE=ON
  -DGGML_NATIVE=OFF
)
case "$PLATFORM" in
  macos)
    cmake_args+=(-DCMAKE_OSX_ARCHITECTURES=arm64)
    ;;
  iphoneos|iphonesimulator)
    cmake_args+=(
      -DCMAKE_SYSTEM_NAME=iOS
      -DCMAKE_OSX_SYSROOT="$PLATFORM"
      -DCMAKE_OSX_ARCHITECTURES=arm64
      -DCMAKE_OSX_DEPLOYMENT_TARGET=15.0
    )
    ;;
esac

# ---------------------------------------------------------------------------
# 2. 第一遍：只编基线变体（不加前缀）当符号捐献者
# ---------------------------------------------------------------------------
log "第一遍：编基线变体 ${BASE_TAG}（不加前缀）作符号捐献者"
# ⚠️ 必须**显式清空** VT_CPU_NS_DIR / VT_CPU_DISPATCH_SRC。
# 这两个是 CMake 缓存变量，上一次跑第二遍时写进去了，下次不传就会一直留着 ——
# 于是「符号捐献者」这一遍也带上了 -include，编出来的归档符号已经加过前缀，
# 反推出来的清单就成了 vtv0_xxx → vtv0_vtv0_xxx 这种垃圾，第二遍的前缀全部失效。
# 症状极具误导性：脚本一路「成功」，最后链接期报 581 个 duplicate symbol。
"$CMAKE" -S "$SRC" -B "$BUILD" "${cmake_args[@]}" \
  -DGGML_CPU_VARIANTS_STATIC="$BASE_TAG" \
  -DVT_CPU_NS_DIR= \
  -DVT_CPU_DISPATCH_SRC= \
  >"$LOGDIR/cmake-$PLATFORM-pass1.log" 2>&1 \
  || { tail -30 "$LOGDIR/cmake-$PLATFORM-pass1.log" >&2; die "第一遍 cmake 配置失败"; }

"$CMAKE" --build "$BUILD" --config Release -j "$JOBS" \
  --target "ggml-cpu-$BASE_TAG" ggml-base ggml \
  || die "第一遍编译失败（重跑本脚本即可增量继续）"

BASE_LIB="$BUILD/ggml/src/libggml-cpu-$BASE_TAG.a"
[ -f "$BASE_LIB" ] || die "基线归档没编出来：$BASE_LIB"

# 捐体必须是**未加前缀**的形态。这一条就是上面那个缓存泄漏的探针：
# 少了它，前缀失效会一路静默到链接期才发现，报的还是 581 条 duplicate symbol。
if ! nm -g --defined-only "$BASE_LIB" 2>/dev/null | awk '$2 ~ /^[A-Z]$/ {print $3}' | grep -qx "_ggml_backend_cpu_reg"; then
  die "符号捐献者 $BASE_LIB 里没有未加前缀的 _ggml_backend_cpu_reg ——
      说明这一遍也注入了前缀头（CMake 缓存的 VT_CPU_NS_DIR 没清干净）。
      处理：删掉构建目录后重跑本脚本：
        rm -rf $BUILD"
fi

# ---------------------------------------------------------------------------
# 3. 反推符号清单 → 生成前缀头 + 调度表
# ---------------------------------------------------------------------------
log "反推符号清单并生成前缀头"
mkdir -p "$NSDIR"
gen_args=(gen --baseline "$BASE_LIB" --out "$NSDIR")
for e in "${VT_ENTRIES[@]}"; do gen_args+=(--tag "$e"); done
"$PY" "$ROOT/scripts/vt-cpu-variants.py" "${gen_args[@]}"

# ---------------------------------------------------------------------------
# 4. 第二遍：注入前缀 + 挂调度器，重编
# ---------------------------------------------------------------------------
log "第二遍：注入符号前缀重编（第一遍的变体产物会被覆盖）"
"$CMAKE" -S "$SRC" -B "$BUILD" "${cmake_args[@]}" \
  -DGGML_CPU_VARIANTS_STATIC="$VARIANT_SPEC" \
  -DVT_CPU_NS_DIR="$NSDIR" \
  -DVT_CPU_DISPATCH_SRC="$ROOT/native/cpu/vt_cpu_dispatch.c" \
  >"$LOGDIR/cmake-$PLATFORM-pass2.log" 2>&1 \
  || { tail -30 "$LOGDIR/cmake-$PLATFORM-pass2.log" >&2; die "第二遍 cmake 配置失败"; }

targets=()
for t in "${VT_TAGS[@]}"; do targets+=("ggml-cpu-$t"); done
"$CMAKE" --build "$BUILD" --config Release -j "$JOBS" \
  --target "${targets[@]}" ggml llama \
  || die "第二遍编译失败"

# ---------------------------------------------------------------------------
# 5. 门禁
# ---------------------------------------------------------------------------
fail=0

log "门禁 1/4：多变体必须能共存于同一次链接"
# 判据用**链接器自己的裁决**，不是我们的 nm 比对 —— 更硬，也更接近现场。
# `ld -r -all_load` 会把每个归档的每个目标文件都拉进来，等价于 App 链接时
# 「这些库都被用到」的最坏情况，此时若还有 duplicate symbol，门禁 1a 直接失败。
# （实测 libc++ 的 ABI 标记符号会被 ld64 合并，所以 1b 里显式剔除它们并列出名单。）
#
# ⚠️ `-r` 也必须带上 `-target`，否则 cc 会按**宿主**（macOS）目标去链接 iOS 归档：
#       ld: building for 'macOS', but linking in object file (…libggml-cpu-armv8a.a) built for 'iOS'
#    这条报错和符号冲突长得完全不一样，但会被下面的分支一起吞成「门禁失败」，
#    看起来像「多变体方案不成立」—— 其实只是门禁自己的工具链问题。
VT_LD_TARGET=()
case "$PLATFORM" in
  iphoneos)        VT_LD_TARGET=(-target arm64-apple-ios15.0) ;;
  iphonesimulator) VT_LD_TARGET=(-target arm64-apple-ios15.0-simulator) ;;
esac

all_libs=()
for t in "${VT_TAGS[@]}"; do
  a="$BUILD/ggml/src/libggml-cpu-$t.a"
  [ -f "$a" ] && all_libs+=("$a")
done
if [ "${#all_libs[@]}" -eq 0 ]; then
  warn "一个变体归档都没找到"; fail=1
else
  merged="$LOGDIR/merged-variants.o"
  if cc -r -all_load "${all_libs[@]}" ${VT_LD_TARGET[@]+"${VT_LD_TARGET[@]}"} \
       -o "$merged" 2>"$LOGDIR/merged-variants.err"; then
    ok "ld -r -all_load 合并 ${#all_libs[@]} 个变体：退出码 0（$(grep -c 'duplicate symbol' "$LOGDIR/merged-variants.err" || true) 条 duplicate symbol）"
    rm -f "$merged"
  else
    # ⚠️ 必须把「重复符号」和「别的原因链不起来」分开报。
    # ⚠️ 管道要带 `|| true`：本脚本是 set -euo pipefail，grep 一条没匹配上就返回 1，
    #    会**在报错信息打印之前**把整个脚本干掉 —— 门禁 2/3/4 连跑都跑不到。
    dups="$( { grep -o "duplicate symbol '[^']*'" "$LOGDIR/merged-variants.err" || true; } | sort -u | head -20)"
    if [ -n "$dups" ]; then
      warn "ld -r -all_load 合并失败，冲突符号："
      printf '%s\n' "$dups" >&2
    else
      warn "ld -r -all_load 失败，但**没有**任何 duplicate symbol ——"
      warn "这不是符号冲突，多半是目标平台不匹配或工具链问题。链接器原始输出："
      sed -n '1,12p' "$LOGDIR/merged-variants.err" >&2
    fi
    fail=1
  fi
fi

for i in "${!VT_TAGS[@]}"; do
  for j in "${!VT_TAGS[@]}"; do
    [ "$i" -ge "$j" ] && continue
    a="$BUILD/ggml/src/libggml-cpu-${VT_TAGS[$i]}.a"
    b="$BUILD/ggml/src/libggml-cpu-${VT_TAGS[$j]}.a"
    if [ ! -f "$a" ] || [ ! -f "$b" ]; then
      warn "缺归档，跳过 $a vs $b"; fail=1; continue
    fi
    "$PY" "$ROOT/scripts/vt-cpu-variants.py" check --ignore-libcxx "$a" "$b" || fail=1
  done
done

log "门禁 2/4：未改名的 ggml_backend_cpu_reg 只许由 dispatch 提供"
for t in "${VT_TAGS[@]}"; do
  a="$BUILD/ggml/src/libggml-cpu-$t.a"
  [ -f "$a" ] || continue
  defined="$(nm -g --defined-only "$a" 2>/dev/null | awk '$2 ~ /^[A-Z]$/ {print $3}' | sort -u)"
  if printf '%s\n' "$defined" | grep -qx "_ggml_backend_cpu_reg"; then
    warn "$t 导出了**未改名**的 ggml_backend_cpu_reg —— 前缀头没生效"
    fail=1
  elif printf '%s\n' "$defined" | grep -qE "^_vtv[0-9]+_ggml_backend_cpu_reg$"; then
    ok "$t 只有带前缀的入口"
  else
    warn "$t 既没有前缀入口也没有裸入口，归档可疑"
    fail=1
  fi
done

log "门禁 3/4 + 4/4：指令集静态判据（A8 安全 / A15+ 真提速）"
# otool -tvV 的行形如 `0000000000000000<TAB>stp<TAB>x20, x19, ...`，助记符在第 2 列。
count_insn() {  # count_insn <归档> <精确助记符>
  otool -tvV "$1" 2>/dev/null | awk '{print $2}' | grep -cx "$2" || true
}
for t in "${VT_TAGS[@]}"; do
  a="$BUILD/ggml/src/libggml-cpu-$t.a"
  [ -f "$a" ] || continue
  sdot="$(count_insn "$a" sdot)"; udot="$(count_insn "$a" udot)"
  smmla="$(count_insn "$a" smmla)"; ummla="$(count_insn "$a" ummla)"
  dot=$((sdot + udot)); i8mm=$((smmla + ummla))
  feat=""
  for e in "${VT_ENTRIES[@]}"; do
    [ "${e%%:*}" = "$t" ] && feat="$e"
  done
  case "$feat" in
    *DOTPROD*)  [ "$dot"  -gt 0 ] || { warn "$t 声明了 DOTPROD 却一条 sdot/udot 都没有"; fail=1; } ;;
    *)          [ "$dot"  -eq 0 ] || { warn "$t 不含 DOTPROD 却有 $dot 条 sdot/udot —— iPad mini 4 上会 SIGILL"; fail=1; } ;;
  esac
  case "$feat" in
    *MATMUL_INT8*) [ "$i8mm" -gt 0 ] || { warn "$t 声明了 MATMUL_INT8 却一条 smmla/ummla 都没有"; fail=1; } ;;
    *)             [ "$i8mm" -eq 0 ] || { warn "$t 不含 MATMUL_INT8 却有 $i8mm 条 smmla/ummla"; fail=1; } ;;
  esac
  printf '  %-8s sdot/udot=%-5s smmla/ummla=%-5s  声明特性=%s\n' \
    "$t" "$dot" "$i8mm" "${feat#*:}"
done

# 机读报告
"$PY" - "$NSDIR/vt_cpu_variants.json" "$BUILD" "$PLATFORM" "$VARIANT_SPEC" <<'PY'
import json, pathlib, sys
out, build, platform, spec = sys.argv[1:5]
base = pathlib.Path(build) / "ggml" / "src"
libs = {p.name: p.stat().st_size for p in sorted(base.glob("libggml-cpu-*.a"))}
pathlib.Path(out).write_text(json.dumps(
    {"platform": platform, "variantSpec": spec,
     "archivesBytes": libs,
     "totalBytes": sum(libs.values())}, indent=2, ensure_ascii=False))
print(f"  报告 → {out}（变体归档合计 {sum(libs.values()) / 1048576:.1f} MiB）")
PY

if [ "$fail" -ne 0 ]; then
  warn "门禁未通过"
  [ "$KEEP_GOING" -eq 1 ] || exit 1
fi

# ---------------------------------------------------------------------------
# 6. 可选：把产物摆到链接器能找到的位置
# ---------------------------------------------------------------------------
if [ -n "$OUT" ]; then
  mkdir -p "$OUT"
  for t in "${VT_TAGS[@]}"; do
    cp "$BUILD/ggml/src/libggml-cpu-$t.a" "$OUT/"
  done
  cp "$BUILD/ggml/src/libggml.a" "$BUILD/ggml/src/libggml-base.a" "$OUT/" 2>/dev/null || true
  cp "$BUILD/src/libllama.a" "$OUT/" 2>/dev/null || true
  log "归档已复制到 $OUT"
fi

log "完成。变体归档："
ls -la "$BUILD"/ggml/src/libggml-cpu-*.a
