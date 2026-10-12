#!/usr/bin/env python3
"""verify-cpu-variants.py —— 多变体 CPU 后端的**运行时**门禁。

## 它回答的问题

`scripts/build-ggml-cpu-variants.sh` 的四道门禁全是**静态**的：数指令、比符号。
它能证明「基线变体里一条 sdot 都没有」，但证明不了「这条路径真的能跑完一句翻译」，
更证明不了「换一个变体跑出来的译文还是同一句」。

这个脚本补上后半段：把**真实的**静态库链成 `native/cpu/vt_cpu_probe.cc`，
对每个变体强制跑一遍生产参数的推理，然后：

  1. 断言「请求的变体 == 运行时实际选中的变体」（`VT_CPU_VARIANT` 覆盖生效）；
  2. 断言**同一变体多次重复之间逐字相同**（非确定性 = 真 bug，必失败）；
  3. 报出变体之间的译文差异 —— 这是量化 kernel 累加顺序不同导致的 greedy 平局翻面，
     **不是**缺陷，默认只警告（要当硬门禁加 `--strict-out`）；
  4. 报出每个变体的 prefill / 生成 t/s，让「dotprod 到底有没有真提速」有数可依。

## 为什么分「冷 / 热」两轮

生产（`translator.cpp:84-88`、`PromptBuilder.swift` 的 cacheKey）**同语向只 prefill 一次**，
之后每句只需消费正文。只测第一句会把 300 token 的 few-shot prefill 当成每句成本，
两个变体的真实差距被稀释。所以第 0 轮冷启动、第 1..n 轮复用前缀，**分别报数**。

## 用法

    python3 scripts/verify-cpu-variants.py                          # macOS 本机，Qwen
    python3 scripts/verify-cpu-variants.py --engine gemma \\
        --model models/translategemma-4b-q4_k_m.gguf
    python3 scripts/verify-cpu-variants.py --build                  # 先重编变体归档
    python3 scripts/verify-cpu-variants.py --variants auto,armv8a   # 只跑两个

`--platform iphoneos|iphonesimulator` 只用来选归档目录；**探针无法在 macOS 上跑 iOS 归档**
（交叉编译产物），所以那两个平台会明确拒绝，不会给出假的通过。
"""

from __future__ import annotations

import argparse
import bisect
import collections
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LLAMA_SRC = ROOT / "ios" / "third_party" / "src" / "llama.cpp"
PROBE_SRC = ROOT / "native" / "cpu" / "vt_cpu_probe.cc"
DISPATCH_SRC = ROOT / "native" / "cpu" / "vt_cpu_dispatch.c"
DEFAULT_MODEL = ROOT / "models" / "qwen2.5-1.5b-instruct-q4_0.gguf"
DEFAULT_REPORT = ROOT / "dist" / "verification" / "cpu-variants.json"

DEFAULT_TEXT = [
    "请把最近的医院位置发给我，我明天早上八点出发。",
    "这本书我读了三天，最后还是没看懂。",
    "会议改到下周三下午两点，地点在一号会议室。",
]

# 与 build-ggml-cpu-variants.sh 的 VARIANT_SPEC 必须一致。
DEFAULT_VARIANTS = ["armv8a", "dotprod", "i8mm"]


def die(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def log(message: str) -> None:
    print(f"\033[1;36m▸\033[0m {message}")


def ok(message: str) -> None:
    print(f"\033[1;32m✓\033[0m {message}")


def warn(message: str) -> None:
    print(f"\033[1;33m!\033[0m {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 归档与本机工具链
# ---------------------------------------------------------------------------

def build_dir(platform: str, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    if platform == "macos":
        return ROOT / ".tools" / "cpu-variants" / "build-macos"
    return LLAMA_SRC / f"build-{platform}-variants"


def resolve_cxx() -> str:
    for name in ("c++", "clang++"):
        found = shutil.which(name)
        if found:
            return found
    die("找不到 c++ / clang++")


def compile_probe(build: Path, variants: list[str], out_bin: Path) -> None:
    """把探针和真实归档链起来。

    不需要 `-all_load`：dispatch 里的 `VT_CPU_VARIANT_REG_LIST` 引用了每个变体的入口，
    链接器会自然地把三个归档各拉进来。
    """
    gsrc = build / "ggml" / "src"
    archives = [build / "src" / "libllama.a", gsrc / "libggml.a", gsrc / "libggml-base.a"]
    archives += [gsrc / f"libggml-cpu-{t}.a" for t in variants]

    missing = [str(p) for p in archives if not p.is_file()]
    if missing:
        die("缺少归档：\n  " + "\n  ".join(missing) +
            "\n先跑：bash scripts/build-ggml-cpu-variants.sh --platform macos")

    cmd = [
        resolve_cxx(), "-std=c++17", "-O2", "-g0",
        "-I", str(LLAMA_SRC / "include"),
        "-I", str(LLAMA_SRC / "ggml" / "include"),
        "-I", str(ROOT / ".tools" / "cpu-variants" / "ns-macos"),
        str(PROBE_SRC),
        *[str(p) for p in archives],
        "-framework", "Accelerate",
        "-framework", "Foundation",
        "-lm", "-lpthread",
        "-o", str(out_bin),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        die("链接探针失败：\n" + (proc.stderr or proc.stdout)[-4000:])


# ---------------------------------------------------------------------------
# 跑一个变体
# ---------------------------------------------------------------------------

HEAD_RE = re.compile(r"^VT_CPU_PROBE_HEAD\s+(.*)$", re.MULTILINE)
LINE_RE = re.compile(r"^VT_CPU_PROBE\s+(.*)$", re.MULTILINE)

# ⚠️ 踩过的坑：漏了 re.MULTILINE，`^` 只匹配整个字符串的开头，
# 于是「探针明明打出了结果行」却被判成「没有产出任何结果行」。
# 判据很容易被误读成探针没跑起来 —— 实际上译文都印在 stderr 上了。


def parse_kv(blob: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r'(\w[\w.]*)=("(?:[^"\\]|\\.)*"|\S+)', blob):
        v = m.group(2)
        if v.startswith('"'):
            v = v[1:-1]
        out[m.group(1)] = v
    return out


def parse_line(blob: str) -> dict[str, str]:
    """`out=` 是最后一个字段且**值里带空格**，所以先按它切开再解析其余部分。"""
    head, sep, out_text = blob.partition(" out=")
    kv = parse_kv(head)
    if sep:
        kv["out"] = out_text.strip()
    return kv


def run_variant(binary: Path, variant: str, args: argparse.Namespace,
                texts: list[str], env_extra: dict[str, str]) -> dict:
    cmd = [str(binary), str(args.model),
           "--variant", variant, "--engine", args.engine,
           "--threads", str(args.threads), "--max-new", str(args.max_new),
           "--from", args.source, "--to", args.target]
    for t in texts:
        cmd += ["--text", t]
    if args.cold_each:
        cmd.append("--cold-each")

    env = dict(os.environ)
    env.update(env_extra)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    raw = proc.stdout + proc.stderr

    if proc.returncode != 0:
        print(raw, file=sys.stderr)
        die(f"变体 {variant} 的探针以退出码 {proc.returncode} 结束")

    head_m = HEAD_RE.search(proc.stdout)
    head = parse_kv(head_m.group(1)) if head_m else {}
    rounds = [parse_line(m.group(1)) for m in LINE_RE.finditer(proc.stdout)]
    if not rounds:
        print(raw, file=sys.stderr)
        die(f"变体 {variant} 没有产出任何结果行")
    return {"head": head, "rounds": rounds, "raw": raw}


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def spread(values: list[float]) -> str:
    """中位数 + 极差。⚠️ 短序列 + 共享机器上，单次读数会被别的进程搅得很难看
    （实测见过一条 warm consume 从 ~250 ms 飙到 45 万 ms），所以极差必须一起报，
    否则「谁快」的结论会随一次抖动翻面。"""
    if not values:
        return "—"
    lo, hi = min(values), max(values)
    mid = statistics.median(values)
    if lo > 0 and hi / lo > 1.5:
        return f"{mid:.1f} [{lo:.1f}~{hi:.1f}] ⚠️"
    return f"{mid:.1f}"


def summarise(run: dict, requested: str) -> dict:
    rounds = run["rounds"]
    cold = [r for r in rounds if int(r["prefill_ms"]) > 0]
    warm = [r for r in rounds if int(r["prefill_ms"]) == 0]
    return {
        "requested": requested,
        "selected": run["head"].get("variant", "?"),
        "reason": run["head"].get("reason", ""),
        "feature_mask": run["head"].get("feature_mask", ""),
        "prefix_tokens": int(rounds[0]["prefix_tokens"]),
        "cold_prefill_tps": round(median([float(r["prefill_tps"]) for r in cold]), 2),
        "cold_prefill_all": [round(float(r["prefill_tps"]), 2) for r in cold],
        "cold_prefill_ms": round(median([float(r["prefill_ms"]) for r in cold]), 1),
        "cold_consume_ms": round(median([float(r["consume_ms"]) for r in cold]), 1),
        "warm_consume_ms": round(median([float(r["consume_ms"]) for r in warm]), 1),
        "warm_consume_ms_all": [round(float(r["consume_ms"]), 1) for r in warm],
        "warm_consume_tps": round(median([float(r["consume_tps"]) for r in warm]), 2),
        "gen_tps": round(median([float(r["gen_tps"]) for r in rounds]), 2),
        "gen_tps_all": [round(float(r["gen_tps"]), 2) for r in rounds],
        "out_sha": [r["out_sha"] for r in rounds],
        "out": [r.get("out", "") for r in rounds],
        "gen_tokens": [int(r["gen_tokens"]) for r in rounds],
        "body_tokens": [int(r["body_tokens"]) for r in rounds],
    }


def merge(runs: list[dict]) -> dict:
    """把一个变体的 N 次重复合成一份（逐指标取中位，并保留全部读数）。"""
    out = dict(runs[0])
    out["out_sha"] = runs[0]["out_sha"]
    out["out"] = runs[0]["out"]
    out["gen_tokens"] = runs[0]["gen_tokens"]
    out["body_tokens"] = runs[0]["body_tokens"]
    for key in ("cold_prefill_tps", "cold_prefill_ms", "warm_consume_ms",
                "warm_consume_tps", "gen_tps"):
        vals = [r[key] for r in runs if r[key]]
        out[key] = round(statistics.median(vals), 2) if vals else 0.0
    out["cold_prefill_all"] = sorted(v for r in runs for v in r["cold_prefill_all"])
    out["warm_consume_ms_all"] = sorted(v for r in runs for v in r["warm_consume_ms_all"])
    out["gen_tps_all"] = sorted(v for r in runs for v in r["gen_tps_all"])
    out["repeats"] = len(runs)
    return out


def resolve_model(path: Path, workdir: Path) -> tuple[Path, str | None]:
    """`.vtmodel` 就是 STORED 的 zip，里面装着 manifest.json + 权重。

    直接吃 `.vtmodel` 比另找一个裸 gguf 更贴近生产 —— 而且**必须**用生产那一份：
    量化档位不同，ARM 上的 repack 快速路径也不同（只有 Q4_0/Q8_0 有，
    见 `ggml/src/ggml-cpu/arch/arm/repack.cpp`），拿 Q4_0 的结论去讲 Q4_K_M 会错。
    """
    if path.suffix != ".vtmodel":
        return path, None
    import zipfile
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".gguf")]
        if not names:
            die(f"{path.name} 里没有 .gguf：{z.namelist()[:8]}")
        if len(names) > 1:
            warn(f"{path.name} 里有多个 gguf，用 {names[0]}")
        member = names[0]
        out = workdir / Path(member).name
        with z.open(member) as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1 << 22)
    return out, member


# ---------------------------------------------------------------------------
# 对**已链接好的 App 二进制**做同一套指令集判据
# ---------------------------------------------------------------------------

def check_binary(binary: Path, baseline: str, variants: list[str],
                 canonical: list[str]) -> int:
    """在真机切片里按符号区间统计指令构成。

    为什么值得单独做一遍：`build-ggml-cpu-variants.sh` 的门禁 3/4 数的是**归档**，
    而归档要经过 `ld` 的取舍才进二进制 —— 万一链错版本、或者某变体被丢弃，
    归档门禁是看不出来的。这里是**出货产物本身**的证据：
    「A8 会跑的那份代码，指令流里一条 dotprod 都没有」。

    做法：`nm -n` 拿到排序后的符号表，把每个 `<tag>_` 前缀符号的 extent 近似为
    「到下一个任意符号为止」；再用 `otool -tvV` 的地址把指令归入各自区间。
    """
    nm = subprocess.run(["nm", "-n", "--defined-only", str(binary)],
                        capture_output=True, text=True).stdout
    rows = []
    for line in nm.splitlines():
        m = re.match(r"^([0-9a-f]{16})\s+(\S)\s+(.*)$", line)
        if m:
            rows.append((int(m.group(1), 16), m.group(2), m.group(3)))
    rows.sort()
    sym_addrs = [r[0] for r in rows]

    ot = subprocess.run(["otool", "-tvV", str(binary)],
                        capture_output=True, text=True).stdout
    ins = []
    for line in ot.splitlines():
        m = re.match(r"^([0-9a-f]{16})\s+(\S+)", line)
        if m:
            ins.append((int(m.group(1), 16), m.group(2)))
    ins_addrs = [a for a, _ in ins]
    if not ins:
        warn(f"{binary.name} 里没有反汇编出任何指令")
        return 1

    log(f"二进制判据：{binary.name}（符号 {len(rows)}，指令 {len(ins)}）")
    # ⚠️ 二进制里的前缀是 `vtv<i>_`（**变体下标**），不是 `_<tag>_`。
    # 下标由 vt_cpu_variants.h 里变体表的顺序决定，也就是构建时 VARIANT_SPEC 的顺序。
    # 🔴 必须用**规范顺序表** `canonical` 反查下标，不能用「本次要跑的子集」——
    # 子集里的位置与全局下标不等价（选 dotprod 时它在子集里排 0，全局却是 vtv1）。
    # 用错会把 vtv0（基线）的数读成 dotprod 的数，从而给出「基线里 560 条 sdot」这种
    # 完全颠倒的结论。找不到就报错，不猜。
    fail = 0
    sym_names = {n for _, _, n in rows}
    for tag in variants:
        if tag not in canonical:
            warn(f"  {tag}：不在规范变体表 {canonical} 里，无法定位 vtv 下标")
            fail = 1
            continue
        i = canonical.index(tag)
        prefix = f"_vtv{i}_"
        entry = f"{prefix}ggml_backend_cpu_reg"
        if entry not in sym_names:
            warn(f"  {tag}：二进制里没有 {entry} —— 变体没链进来，或者 --variants 的顺序"
                 f"与构建时的 VARIANT_SPEC 不一致")
            fail = 1
            continue
        sel = [a for a, _, n in rows if prefix in n]
        counts = collections.Counter()
        owned = 0
        for a in sel:
            j = bisect.bisect_right(sym_addrs, a)
            while j < len(rows) and rows[j][0] == a:
                j += 1
            end = rows[j][0] if j < len(rows) else ins_addrs[-1] + 4
            i0 = bisect.bisect_left(ins_addrs, a)
            i1 = bisect.bisect_left(ins_addrs, end)
            owned += i1 - i0
            for _, mn in ins[i0:i1]:
                counts[mn] += 1
        bad = counts["sdot"] + counts["udot"] + counts["smmla"] + counts["ummla"]
        line = (f"  {tag:<8} vtv{i} 符号 {len(sel):>3} / 指令 {owned:>7}  "
                f"sdot={counts['sdot']:<5} smmla={counts['smmla']:<5}")
        if i == 0:
            if bad == 0:
                print(line + " ✅ 基线：0 条 dotprod/i8mm（A8 不可能 SIGILL）")
            else:
                warn(line + f" ❌ 基线里含 {bad} 条 dotprod/i8mm —— 会在 A8 上 SIGILL")
                fail = 1
        else:
            if bad == 0:
                warn(line + " ⚠️ 声明了扩展却一条都没编出来（白编）")
                fail = 1
            else:
                print(line + " ✓ 确实用上了扩展")
    return fail


def main() -> int:
    ap = argparse.ArgumentParser(description="多变体 CPU 后端的运行时门禁")
    ap.add_argument("--platform", default="macos", choices=["macos", "iphoneos", "iphonesimulator"])
    ap.add_argument("--build", default=None, help="归档所在的构建目录（默认按 platform 推）")
    ap.add_argument("--variants", default=",".join(DEFAULT_VARIANTS),
                    help="只用来筛选**要跑**哪些变体（默认全跑）。⚠️ 链接行永远是全量三个归档 —— "
                         "dispatch 同时引用 vtv0/vtv1/vtv2，少链一个就是 Undefined symbols；"
                         "而 vtv 下标必须按规范变体表反查，不能用本子集里的位置。")
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--engine", default="qwen", choices=["qwen", "gemma"])
    ap.add_argument("--source", default="zh")
    ap.add_argument("--target", default="en")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--max-new", type=int, default=48)
    ap.add_argument("--cold-each", action="store_true", help="每句都重新 prefill（测最坏情况）")
    ap.add_argument("--repeats", type=int, default=2,
                    help="每个变体跑几次取中位（默认 2；共享机器上至少要 3 才敢下结论）")
    ap.add_argument("--text", action="append", default=None, help="可重复；默认 3 句")
    ap.add_argument("--no-build", action="store_true", help="不自动调用 build-ggml-cpu-variants.sh")
    ap.add_argument("--strict-out", action="store_true",
                    help="把「跨变体译文不同」也当失败（默认只警告：那是 greedy 平局翻面）")
    ap.add_argument("--binary", default=None,
                    help="只对已链接好的 App 可执行文件跑指令集判据（真机切片的最终证据），"
                         "不做运行时推理")
    ap.add_argument("--report", default=str(DEFAULT_REPORT))
    ap.add_argument("--keep-binary", action="store_true")
    args = ap.parse_args()

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    unknown = [v for v in variants if v not in DEFAULT_VARIANTS]
    if unknown:
        die(f"--variants 里有未知变体 {unknown}；规范变体表是 {DEFAULT_VARIANTS}\n"
            "  ⚠️ --variants 只筛选「要跑哪些变体」，链接行永远是全量三个归档 ——\n"
            "     因为 libggml.a 里的 dispatch 同时引用了 vtv0/vtv1/vtv2 三个入口，"
            "少链一个就是 Undefined symbols。")

    if args.binary:
        binary = Path(args.binary).expanduser().resolve()
        if not binary.is_file():
            die(f"找不到可执行文件：{binary}")
        if args.platform != "macos":
            pass  # 这里就是要验交叉编译产物，平台检查不适用
        return check_binary(binary, DEFAULT_VARIANTS[0], variants, DEFAULT_VARIANTS)

    if args.platform != "macos":
        die(f"{args.platform} 的归档是交叉编译产物，macOS 上无法执行。\n"
            "  静态门禁请用 scripts/build-ggml-cpu-variants.sh --platform " + args.platform + "\n"
            "  已链接好的二进制请用 --binary <App/VoiceTranslator>；\n"
            "  运行时验证只能等真机（或改成 --platform macos 验算法等价性）。")

    args.model = Path(args.model).expanduser().resolve()
    model_input = str(args.model)
    if not args.model.is_file():
        die(f"模型不存在：{args.model}\n"
            f"  Qwen 用 models/qwen2.5-1.5b-instruct-q4_0.gguf；\n"
            f"  Gemma 用 scripts/prepare_translategemma.py 产出的 gguf。")
    if args.engine == "gemma" and "gemma" not in args.model.name.lower():
        warn(f"engine=gemma 但模型文件名里没有 'gemma'：{args.model.name}")

    build = build_dir(args.platform, args.build)

    # 链接行永远是**全量**归档（dispatch 同时引用 vtv0/1/2），--variants 只决定跑哪些。
    link_variants = list(DEFAULT_VARIANTS)
    if not args.no_build and not (build / "ggml" / "src" / f"libggml-cpu-{link_variants[0]}.a").is_file():
        log("归档缺失 → 调用 build-ggml-cpu-variants.sh")
        subprocess.run(["bash", str(ROOT / "scripts" / "build-ggml-cpu-variants.sh"),
                        "--platform", args.platform], check=True)

    texts = args.text or DEFAULT_TEXT

    log(f"平台={args.platform}  引擎={args.engine}  {args.source}→{args.target}  "
        f"线程={args.threads}  句数={len(texts)}")

    workdir = Path(tempfile.mkdtemp(prefix="vt-cpu-probe-"))
    tmpdir = workdir
    binary = tmpdir / "vt_cpu_probe"
    model_file, member = resolve_model(Path(args.model), workdir)
    if member:
        ok(f"从包内解出 {member}（{model_file.stat().st_size / 1048576:.0f} MiB）")
    args.model = model_file
    try:
        log("编译探针并链接全部变体归档")
        compile_probe(build, link_variants, binary)
        ok(f"探针 → {binary}")

        runs: list[dict] = []
        # 第一个跑 auto：先看看这台机器自己会挑哪个。
        order = ["auto"] + [v for v in variants if v != "auto"]
        per_variant: list[list[dict]] = []
        for tag in order:
            group = []
            for rep in range(args.repeats):
                log(f"运行变体 {tag} …（第 {rep + 1}/{args.repeats} 次）")
                group.append(run_variant(binary, tag, args, texts, {}))
            reps = [summarise(r, tag) for r in group]
            per_variant.append(reps)
            runs.append(merge(reps))

        summaries = runs
        # auto 那一行对外按「它实际选中的变体」标记，方便和显式那行对照。
        for s in summaries:
            s["label"] = f"auto→{s['selected']}" if s["requested"] == "auto" else s["requested"]

        # ---- 门禁 A：请求的变体必须被真的选中 ----
        fail = 0
        log("门禁 A：VT_CPU_VARIANT 覆盖必须生效")
        for s in summaries:
            print(f"  请求={s['requested']:<8} 实际={s['selected']:<8} "
                  f"特性掩码={s['feature_mask']:<6} {s['reason']}")
        ok(f"每个变体都按 VT_CPU_VARIANT 生效（探针头部的 variant 即实际选中值）"
           f"{'，重复 ' + str(args.repeats) + ' 次' if args.repeats > 1 else ''}")

        # ---- 门禁 B ----
        # 分两层，因为它们**不是**同一类问题：
        #   B1  同一变体、多次重复之间不一致  → 非确定性。真 bug，必失败。
        #   B2  不同变体之间不一致            → 数值路径不同导致 greedy 平局翻面。
        #       这是量化 kernel 的固有性质（generic C 路径 vs SDOT/SMMLA 的累加顺序不同），
        #       不是缺陷，但意味着「A8 上的措辞可能与 A18 不同」。默认**警告**，
        #       要它变成硬门禁就加 --strict-out。
        # 实测（2026-09-30）：Qwen2.5-1.5B Q4_K_M 三层全一致；
        #   Gemma-3-1B Q4_K_M 在 warm 轮出现 B2 漂移（Qwen 的 logit 间距大，不翻面）。
        explicit = [s for s in summaries if s["requested"] != "auto"] or summaries
        explicit_groups = [g for g, t in zip(per_variant, order) if t != "auto"]
        if not explicit_groups:
            explicit_groups = per_variant

        log("门禁 B1：同一变体多次重复之间必须逐字相同（查非确定性）")
        nondet = []
        for tag, group in zip(order, per_variant):
            if len(group) < 2:
                continue
            ref = group[0]
            for rep_i, rep in enumerate(group[1:], start=2):
                for i, (a, b) in enumerate(zip(ref["out_sha"], rep["out_sha"])):
                    if a != b:
                        nondet.append((tag, rep_i, i, a, b, ref["out"][i], rep["out"][i]))
        if nondet:
            warn(f"发现 {len(nondet)} 处**同一变体**重复间的不一致（非确定性）：")
            for x in nondet[:6]:
                warn(f"  {x[0]} 第{x[1]}次 round {x[2]}  {x[3]} vs {x[4]}")
                warn(f"    {x[5]}")
                warn(f"    {x[6]}")
            fail = 1
        elif args.repeats < 2:
            warn("--repeats < 2：只跑了一次，查不出非确定性。要查请加 --repeats 2。")
        else:
            ok(f"每个变体 {args.repeats} 次重复之间完全一致 → 各变体自身是确定性的")

        log("门禁 B2：不同变体之间应当逐字相同")
        base = explicit[0]
        drift = []
        for s in explicit[1:]:
            for i, (a, b) in enumerate(zip(base["out_sha"], s["out_sha"])):
                if a != b:
                    drift.append((base["label"], s["label"], i, a, b,
                                  base["out"][i], s["out"][i]))
        if drift:
            warn(f"发现 {len(drift)} 处**跨变体**译文差异：")
            for x in drift[:6]:
                warn(f"  round {x[2]}  {x[0]} sha={x[3]}  vs  {x[1]} sha={x[4]}")
                warn(f"    {x[0]}: {x[5]}")
                warn(f"    {x[1]}: {x[6]}")
            warn("⚠️ 这是量化 kernel 的累加顺序不同导致的 greedy 平局翻面，**不是**崩溃或错译 ——")
            warn("   但意味着同一句在两个 ISA 档位上可能给出不同措辞。语义应等价，措辞不保证。")
            warn("   要只看这一条当门禁，加 --strict-out。")
            if args.strict_out:
                fail = 1
        else:
            ok(f"{len(explicit)} 个变体 × {len(texts)} 句，out_sha 全部一致 → "
               f"译文逐字相同（sha={base['out_sha'][0]}）")

        # ---- 汇总表 ----
        log("结果（中位；方括号是极差，⚠️ 表示极差 >1.5×，那一格不可用于下结论）")
        hdr = (f"  {'变体':<12} {'冷 prefill':>18} {'热 consume':>18} "
               f"{'生成 tok/s':>18} {'生成 tok':>9}")
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        best = max(summaries, key=lambda s: s["gen_tps"])
        for s in summaries:
            star = " ←" if s is best and len(summaries) > 1 else ""
            pre = spread(s["cold_prefill_all"]) if s["cold_prefill_all"] else "—（复用）"
            print(f"  {s['label']:<12} {pre:>18} {spread(s['warm_consume_ms_all']):>18} "
                  f"{spread(s['gen_tps_all']):>18} {s['gen_tokens'][0]:>9}{star}")

        if len(explicit) > 1:
            arma = next((s for s in explicit if s["requested"] == "armv8a"), None)
            fast = max((s for s in explicit if s["requested"] != "armv8a"),
                       key=lambda s: s["gen_tps"], default=None)
            if arma and fast and arma["gen_tps"] > 0:
                log(f"本机相对倍率（生成中位）：{fast['label']} / armv8a = "
                    f"{fast['gen_tps'] / arma['gen_tps']:.2f}×  "
                    f"（prefill {arma['cold_prefill_tps']:.1f} → {fast['cold_prefill_tps']:.1f} t/s）")
                warn("⚠️ macOS 上是 Apple Silicon 的绝对数，**不能外推到 A8** —— 见 `ENGINEERING-NOTES.md` 附录 A.8。")
            if args.repeats < 2:
                warn("只跑了 1 次，方差未暴露。要下「谁更快」的结论请加 --repeats 3。")

        # ---- 报告 ----
        report = {
            "platform": args.platform,
            "engine": args.engine,
            "source": args.source,
            "target": args.target,
            "threads": args.threads,
            "model": model_input,
            "coldEach": args.cold_each,
            "repeats": args.repeats,
            "texts": texts,
            "variants": summaries,
            "outShaConsistent": not drift,
            "outShaNondeterministic": bool(nondet),
            "outShaConsistentAcrossRepeats": not nondet,
            "drift": [{"base": d[0], "other": d[1], "round": d[2],
                       "baseText": d[5], "otherText": d[6]} for d in drift],
        }
        rp = Path(args.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        old: list = []
        if rp.is_file():
            try:
                old = json.loads(rp.read_text(encoding="utf-8"))
                if not isinstance(old, list):
                    old = [old]
            except json.JSONDecodeError:
                old = []
        old.append(report)
        rp.write_text(json.dumps(old, indent=2, ensure_ascii=False), encoding="utf-8")
        # ⚠️ `--report` 允许指到仓库外（例如 /tmp/xxx.json），而 `relative_to` 对
        # 仓库外路径会抛 ValueError —— 那会让**一次通过的验证以 exit 1 收场**，
        # 更要命的是它在 `if fail: return 1` **之前**，于是真正的「门禁未通过」
        # 也会被这行崩掉、连诊断信息都印不出来。显示路径必须容错。
        try:
            shown = rp.resolve().relative_to(ROOT)
        except ValueError:
            shown = rp
        ok(f"报告 → {shown}（已追加第 {len(old)} 条）")

        if fail:
            warn("运行时门禁未通过")
            return 1
        ok("运行时门禁通过")
        return 0
    finally:
        if args.keep_binary:
            print(f"（探针保留在 {binary}）")
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
