#!/usr/bin/env python3
"""实测 llama.cpp 在 ARM 上的提速空间：指令集 × 线程数 × 量化格式。

## 为什么要这个脚本

两端（`app/src/main/cpp/CMakeLists.txt` 与 iOS 的 llama.cpp 静态库）都锁在
`GGML_CPU_ARM_ARCH=armv8-a` —— **基线指令集，没有 dotprod / i8mm / fp16**。
翻译模型又是 `Q3_K_M`（反量化开销最大的一档）。这两件事叠起来，
翻译慢的主因不在"模型不行"，而在**内核跑的是最慢的那条路径**。

问题的麻烦之处在于：这个结论没法靠读代码确认，只能实测。本脚本把
"换个配置能快多少"变成可复现的数字，换模型 / 换量化后重跑一次即可。

## ⚠️ 读数字之前必须知道的两件事

1. **测的是宿主机 CPU，不是手机。** 绝对 t/s 只在宿主机上有意义；
   跨设备可迁移的是**相对加速比**（同一 ARM 架构族内）。
2. **`-march=armv8.2-a+dotprod` 在老设备上是崩溃风险，不是性能选项。**
   dotprod 需要 ARMv8.2-A **且** CPU 实际实现了该扩展 —— 架构版本够不等于指令可用。
   实测下限（`clang -mcpu=<cpu> -dM -E` 查 `__ARM_FEATURE_DOTPROD`）：

   | 有 dotprod | 没有 dotprod |
   |---|---|
   | Apple **A13** 起（iPhone 11 系列 / iPad Air 4 及之后） | Apple **A11、A12、A12X/Z** 及更早 |
   | Cortex-**A75/A55** 起（骁龙 845/710 一代，2017Q4） | Cortex-A73/A53、骁龙 835/660 及更早 |

   ⚠️ A11/A12 官方标称 ARMv8.2-A，但**没有实现 dotprod**（clang 的 `apple-a11`/`apple-a12`
   特性表里 `FeatureDotProd` 缺失，`apple-a13` 才有）—— 这是最容易踩空的一档。
   编进去就是 SIGILL，上生产前必须确认目标机型下限。

## 用法

    python3 scripts/bench-llama-arm.py                    # 解包 models/ 里的档位 + 跑全矩阵
    python3 scripts/bench-llama-arm.py --threads 2,4,6
    python3 scripts/bench-llama-arm.py --quantize Q4_0    # 额外测别的量化格式
    python3 scripts/bench-llama-arm.py --clean            # 清掉 .tools/llama-bench

产物全部落在 `.tools/llama-bench/`（已在 .gitignore）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".tools" / "llama-bench"
MODELS = ROOT / "models"
LLAMA_SRC = ROOT / "ios" / "third_party" / "src" / "llama.cpp"

# 默认矩阵：左边是当前生产配置，右边是建议配置
DEFAULT_ARCHS = ["armv8-a", "armv8.2-a+dotprod+fp16"]
DEFAULT_THREADS = [2, 4]


def die(msg: str) -> None:
    print(f"错误：{msg}", file=sys.stderr)
    raise SystemExit(1)


def find_cmake() -> str:
    """按 setup-ios-deps.sh 的同一顺序找 cmake（PATH 里常常没有）。"""
    found = shutil.which("cmake")
    if found:
        return found
    for cand in (
        ROOT / ".tools" / "cmakewheel" / "cmake" / "data" / "bin" / "cmake",
        Path.home() / ".workbuddy" / "binaries" / "python" / "envs" / "default" / "bin" / "cmake",
        Path("/Applications/CMake.app/Contents/bin/cmake"),
        Path("/opt/homebrew/bin/cmake"),
        Path("/usr/local/bin/cmake"),
    ):
        if cand.is_file():
            return str(cand)
    die("找不到 cmake。装一个：brew install cmake，或 pip install cmake")


def translation_gguf() -> Path:
    """从 models/ 下的翻译模型包（.vtmodel 就是 STORED zip）里解出 model.gguf。

    直接复用生产包，保证测的是**实际发出去的那个文件**，不是另下一个。
    """
    target = WORK / "model.gguf"
    if target.is_file():
        return target
    WORK.mkdir(parents=True, exist_ok=True)
    candidates = []
    for pack in sorted(MODELS.glob("*.vtmodel")):
        try:
            with zipfile.ZipFile(pack) as zf:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                names = set(zf.namelist())
        except Exception:
            continue
        if manifest.get("kind") == "translation" and "model.gguf" in names:
            candidates.append((pack, manifest))
    if not candidates:
        die(f"{MODELS} 下没有可用的翻译模型包（需要 kind=translation 且含 model.gguf）")

    pack, manifest = candidates[0]
    if len(candidates) > 1:
        print(f"  注意：找到 {len(candidates)} 个翻译包，用第一个 {pack.name}")
    print(f"  解出 {pack.name} → {target.relative_to(ROOT)}")
    with zipfile.ZipFile(pack) as zf, zf.open("model.gguf") as src, open(target, "wb") as out:
        shutil.copyfileobj(src, out, 8 << 20)
    print(f"       {manifest.get('id')}  engine={manifest.get('engine')}")
    return target


def build_bench(cmake: str, arch: str, jobs: int) -> Path:
    """每个 arch 一个独立构建目录；已建好就复用。"""
    tag = arch.replace("+", "_")
    build_dir = WORK / f"build-{tag}"
    binary = build_dir / "bin" / "llama-bench"
    if binary.is_file():
        return binary
    print(f"  配置 {arch} …")
    subprocess.run(
        [cmake, "-S", str(LLAMA_SRC), "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release",
         "-DGGML_NATIVE=OFF", f"-DGGML_CPU_ARM_ARCH={arch}",
         "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF", "-DLLAMA_BUILD_TOOLS=ON",
         "-DLLAMA_BUILD_SERVER=OFF", "-DLLAMA_CURL=OFF",
         # 关掉非 CPU 后端，避免把 Mac GPU / BLAS 算进去 —— 手机上没有这些
         "-DGGML_METAL=OFF", "-DGGML_ACCELERATE=OFF", "-DGGML_BLAS=OFF", "-DGGML_OPENMP=OFF"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    print(f"  编译 {arch}（-j{jobs}）…")
    subprocess.run([cmake, "--build", str(build_dir), "--target", "llama-bench", "-j", str(jobs)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    if not binary.is_file():
        die(f"{arch} 编译完却没有 {binary}")
    return binary


def requantize(cmake: str, src: Path, quants: list[str], jobs: int) -> list[Path]:
    """从现有 gguf 二次量化出其他格式，只用于**比速度**。

    注意：从 Q3_K_M 再量化到 Q4_0 是有损的二次量化，质量不可作为选型依据，
    这里只看 kernel 速度。真要选型必须从原始 F16 权重重新量化。

    这不是理论担忧 —— 实测（en→zh，生产 prompt，greedy）：
        Q3_K_M（现役包）  去上海的火车明天早上八点半出发，但我还需要买票。
        Q4_0  （二次量化）从上海的火车明天早上八点半出发，但我还需要买票。  ← 方向译反了
        Q4_K_M（官方量化）明天早上八点半有一班去上海的火车，但我还需要买票。
    "to Shanghai" 被二次量化成 "从上海"，语义完全变了，而且只错这一处 ——
    低概率、难发现、但很致命。**Q4_0 必须从 F16 原始权重重量化，不能图省事二次加工。**
    """
    build_dir = WORK / "build-quant"
    tool = build_dir / "bin" / "llama-quantize"
    if not tool.is_file():
        arch = DEFAULT_ARCHS[-1]
        if not (build_dir / "CMakeCache.txt").is_file():
            subprocess.run(
                [cmake, "-S", str(LLAMA_SRC), "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release",
                 "-DGGML_NATIVE=OFF", f"-DGGML_CPU_ARM_ARCH={arch}",
                 "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF", "-DLLAMA_BUILD_TOOLS=ON",
                 "-DLLAMA_CURL=OFF", "-DGGML_METAL=OFF", "-DGGML_ACCELERATE=OFF",
                 "-DGGML_BLAS=OFF", "-DGGML_OPENMP=OFF"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        print(f"  编译 llama-quantize（-j{jobs}）…")
        subprocess.run([cmake, "--build", str(build_dir), "--target", "llama-quantize",
                        "-j", str(jobs)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    out: list[Path] = []
    for q in quants:
        dst = src.with_name(f"model-{q}.gguf")
        if dst.is_file():
            out.append(dst)
            continue
        print(f"  二次量化 {q} → {dst.name}（仅用于比速度，不代表质量）")
        subprocess.run([str(tool), "--allow-requantize", str(src), str(dst), q],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        out.append(dst)
    return out


def run_one(binary: Path, model: Path, threads: list[int], pp: int, tg: int, reps: int) -> dict:
    """跑一次 llama-bench，返回 {(test, threads): t/s}。"""
    cmd = [str(binary), "-m", str(model), "-p", str(pp), "-n", str(tg),
           "-t", ",".join(str(t) for t in threads), "-r", str(reps), "-o", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"    llama-bench 失败：{proc.stderr.strip()[:200]}")
        return {}
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("    解析 llama-bench 输出失败")
        return {}
    out = {}
    for row in rows:
        # avg_ts 是 token/s；n_prompt>0 的是 prefill，否则是生成
        label = "pp" if row.get("n_prompt", 0) > 0 else "tg"
        out[(label, row.get("n_threads"))] = row.get("avg_ts", 0.0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="实测 llama.cpp 的 ARM 提速空间")
    ap.add_argument("--archs", default=",".join(DEFAULT_ARCHS),
                    help="逗号分隔的 -march 列表（默认：%s）" % ",".join(DEFAULT_ARCHS))
    ap.add_argument("--threads", default=",".join(str(t) for t in DEFAULT_THREADS))
    ap.add_argument("--quantize", default="", metavar="Q4_0,Q4_K_M",
                    help="额外二次量化出这些格式一起测（速度有效，质量无效）")
    ap.add_argument("--pp", type=int, default=512, help="prefill 测试长度（默认 512）")
    ap.add_argument("--tg", type=int, default=128, help="生成测试长度（默认 128）")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--clean", action="store_true", help="删掉 .tools/llama-bench 后退出")
    args = ap.parse_args()

    if args.clean:
        if WORK.exists():
            shutil.rmtree(WORK)
            print(f"已删除 {WORK.relative_to(ROOT)}")
        return 0

    if not LLAMA_SRC.is_dir():
        die(f"找不到 llama.cpp 源码：{LLAMA_SRC}\n"
            "先跑 scripts/setup-ios-deps.sh --llama-only，或 scripts/fetch_llama_cpp.py")
    cmake = find_cmake()
    archs = [a.strip() for a in args.archs.split(",") if a.strip()]
    threads = [int(t) for t in args.threads.split(",") if t.strip()]

    print("① 准备模型")
    base = translation_gguf()
    models = [base]
    if args.quantize:
        print("② 准备额外量化格式")
        models += requantize(cmake, base, [q.strip() for q in args.quantize.split(",") if q.strip()])

    print("③ 编译 llama-bench")
    bins = {arch: build_bench(cmake, arch, args.jobs) for arch in archs}

    print("④ 跑基准（这会占满 CPU，别同时干别的）")
    results: dict[tuple[str, str, int], dict] = {}
    for arch in archs:
        for model in models:
            print(f"  {arch:<28} {model.name}")
            results[(arch, model.name)] = run_one(bins[arch], model, threads, args.pp, args.tg, args.reps)

    # ---------------- 输出：以"当前生产配置"为 1.00x ----------------
    baseline_key = (archs[0], base.name)
    baseline = results.get(baseline_key, {})
    if not baseline:
        die("基线配置没跑出结果，无法算加速比")

    print()
    print(f"相对加速比（基准 = {archs[0]} / {base.name} / {threads[0]} 线程）")
    print()
    print(f"| 配置 | 线程 | prefill t/s | 加速 | 生成 t/s | 加速 |")
    print("|---|---|---|---|---|---|")
    for (arch, mname), data in results.items():
        for t in threads:
            pp = data.get(("pp", t))
            tg = data.get(("tg", t))
            if pp is None and tg is None:
                continue
            b_pp = baseline.get(("pp", threads[0]), 0) or 1
            b_tg = baseline.get(("tg", threads[0]), 0) or 1
            pp_s = f"{pp:.2f}" if pp else "—"
            tg_s = f"{tg:.2f}" if tg else "—"
            pp_r = f"{pp / b_pp:.2f}x" if pp else "—"
            tg_r = f"{tg / b_tg:.2f}x" if tg else "—"
            mark = " ← 当前生产配置" if (arch, mname, t) == (archs[0], base.name, threads[0]) else ""
            print(f"| {arch} / {mname} | {t} | {pp_s} | {pp_r} | {tg_s} | {tg_r} |{mark}")

    print()
    print("提醒：")
    print("  · 绝对值只对本机有效；跨设备看的是相对加速比。")
    print("  · armv8.2-a 及以上需要目标设备真的实现 dotprod，否则 SIGILL。")
    print("    下限：Apple A13 起、Cortex-A75/A55 起。")
    print("    注意 A11/A12 标称 ARMv8.2-A 但无 dotprod，别按架构版本推断。")
    if args.quantize:
        print("  · --quantize 是二次量化，只反映 kernel 速度，不能用来选质量。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
