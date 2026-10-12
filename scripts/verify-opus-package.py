#!/usr/bin/env python3
"""verify-opus-package.py —— 端侧 OPUS 包 vs Python 参考实现，逐字对撞。

## 它回答的问题

「`native/nmt/` 这份 C++ 跑出来的译文，和 Python 门禁脚本里的 `OpusBundle`
是不是同一条？ 」——只要这里对不上，端侧包就不该被打进 App。
（2026-09-28 首次跑通：20/20 逐字相同。）

所以它是**改动 `native/nmt/vt_opus.cc` 之后的回归门禁**：
改了推理、分词、id 映射、按需装载，都先跑这个。

## 它不回答什么

不是质量门禁。这里只比「端侧 == Python」，两边一起错它也不管 ——
译文质量由 `scripts/check-nmt-quality.py` 对着参考译文测（关键错误率 / chrF）。

## 与打包的关系

**默认包（`.tools/opus-package`）自 2026-10-09 起已含 `ja-zh` 接线**，所以拿它跑本脚本
也能覆盖全部 4 个语向。历史注记：在此之前默认包刻意不含 `ja-zh`（英文中转两跳、
门禁里最弱的一向），要全量验证得另建一份完整包 ——

    python3 scripts/export-opus-package.py \\
        --out .tools/opus-package-full --routes zh-en,en-zh,zh-ja,ja-zh

本脚本的 DEFAULT_PACKAGE 仍指向 `.tools/opus-package-full`。**缺哪条接线，对应的方向
就会在结尾被单独列出来，不计入通过率**，不会假装全过。

## 用法

    python3 scripts/verify-opus-package.py                      # 全流程
    python3 scripts/verify-opus-package.py --package .tools/opus-package-full
    python3 scripts/verify-opus-package.py --keep-probe         # 保留 /tmp 下的探针可执行文件
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PACKAGE = ROOT / ".tools" / "opus-package-full"
DEFAULT_DATASET = ROOT / "evaluation" / "zh_en_ja_sample.jsonl"
PROBE_SOURCE = ROOT / "native" / "nmt" / "vt_opus_probe.cc"
CORE_SOURCES = [
    ROOT / "native" / "nmt" / "vt_opus.cc",
    ROOT / "native" / "nmt" / "third_party" / "simple-sentencepiece"
        / "ssentencepiece" / "csrc" / "ssentencepiece.cc",
]
INCLUDE_DIRS = [
    ROOT / "native" / "nmt",
    ROOT / "native" / "nmt" / "third_party" / "simple-sentencepiece",
    ROOT / "native" / "nmt" / "third_party" / "onnxruntime",
]


def die(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def read_routes(package: Path) -> dict[str, list[str]]:
    """包内 routes.txt：`语向<TAB>跳1,跳2`。端侧真正的接线来源。"""
    path = package / "routes.txt"
    if not path.is_file():
        die(f"读不到 {path.relative_to(ROOT)}；先跑 scripts/export-opus-package.py --out {package}")
    routes: dict[str, list[str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        key, _, hops = line.partition("\t")
        routes[key] = [h for h in hops.split(",") if h]
    if not routes:
        die("routes.txt 里没有任何语向")
    return routes


def load_cases(dataset: Path) -> list[dict]:
    cases = []
    for line in dataset.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    if not cases:
        die(f"{dataset} 里没有用例")
    return cases


def load_reference_module():
    """把 check-nmt-quality.py 当模块载入，复用它的 `OpusBundle`。

    刻意不复制一份参考实现 —— 复制出来的第二份迟早和第一份漂移，
    而「端侧 == Python」这个断言本身依赖两边真的是同一套逻辑。
    """
    spec = importlib.util.spec_from_file_location(
        "vt_check_nmt_quality", ROOT / "scripts" / "check-nmt-quality.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "scripts"))
    spec.loader.exec_module(module)
    return module


def ort_paths(explicit: Path | None) -> tuple[Path, Path]:
    """返回 (dylib 所在目录, 真实 dylib 文件)。

    ⚠️ pip wheel 里的 install_name 是 `libonnxruntime.1.dylib`，但实际文件名带小版本号
    （1.30.0）。不补这个软链，探针一启动就是
    `Library not loaded: @rpath/libonnxruntime.1.dylib`。
    链接用真实文件（链接器解析实体），运行时装软链（install_name 要那个名字）。
    """
    if explicit is not None:
        capi = Path(explicit)
    else:
        import onnxruntime

        capi = Path(onnxruntime.__file__).parent / "capi"

    # 过滤掉我们自己建的那个软链，只取带小版本号的真身。
    candidates = [p for p in capi.glob("libonnxruntime.*.dylib") if p.name.count(".") >= 3]
    if not candidates:
        die(f"{capi} 下找不到 libonnxruntime.*.dylib")
    real = sorted(candidates)[-1]
    desired = capi / "libonnxruntime.1.dylib"
    if not desired.exists():
        desired.symlink_to(real.name)
    return capi, real


def build_probe(output: Path, dylib: Path) -> None:
    command = [
        "clang++", "-std=c++17", "-O2", "-Wall", "-Wno-deprecated-declarations",
        # macOS 上直接链接 onnxruntime 的 dylib，`OrtGetApiBase` 是普通符号 ——
        # 所以走 vt_ort.h 的**静态**分支。漏了这个宏，vt::Api() 会落到
        # 「未构建依赖」那一支返回 nullptr，探针报
        # 「这个平台上拿不到 ONNX Runtime」（踩过）。
        # Android 侧对应的是 VT_ORT_DYNAMIC（dlopen）。
        "-DVT_ORT_STATIC",
        *[f"-I{d}" for d in INCLUDE_DIRS],
        *[str(s) for s in CORE_SOURCES],
        str(PROBE_SOURCE),
        str(dylib),
        "-o", str(output),
    ]
    print("编译探针：")
    print("  " + " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        die("探针编译失败")
    warnings = [l for l in result.stderr.splitlines() if "warning:" in l]
    if warnings:
        print(f"  （{len(warnings)} 条编译警告）")


def write_cases(path: Path, cases: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(f"{case['source_lang']}\t{case['target_lang']}\t{case['source']}\n")


def read_probe_output(text: str) -> list[tuple[str, str, str, str]]:
    """探针的 stdout：`源语<TAB>目标语<TAB>原文<TAB>译文`。"""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            die(f"探针输出格式不对（应为 4 列制表符分隔）：{line!r}")
        rows.append((parts[0], parts[1], parts[2], parts[3]))
    return rows


def main() -> int:
    # 这份脚本会把「编译 → Python 参考 → 端侧探针」的顺序打出来，中间还夹着探针的
    # stderr。不强制行缓冲的话 stdout 被块缓冲，看到的就是错乱的顺序。
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description="端侧 OPUS 包 vs Python 参考实现逐字对撞")
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--ort", type=Path, default=None,
                        help="ONNX Runtime dylib 目录（默认取 pip 的 onnxruntime/capi）")
    parser.add_argument("--keep-probe", action="store_true", help="保留探针可执行文件")
    parser.add_argument("--json", type=Path, default=None, help="把逐条结果另存为 JSON")
    args = parser.parse_args()

    package = args.package.resolve()
    if not package.is_dir():
        die(f"找不到端侧包目录：{package}")

    routes = read_routes(package)
    all_cases = load_cases(args.dataset)
    covered = [c for c in all_cases
               if f"{c['source_lang']}-{c['target_lang']}" in routes]
    skipped: dict[str, int] = {}
    for case in all_cases:
        key = f"{case['source_lang']}-{case['target_lang']}"
        if key not in routes:
            skipped[key] = skipped.get(key, 0) + 1

    print(f"端侧包   ：{package.relative_to(ROOT)}")
    print(f"包内接线 ：{json.dumps(routes, ensure_ascii=False)}")
    print(f"数据集   ：{args.dataset.name} 共 {len(all_cases)} 条，"
          f"包覆盖 {len(covered)} 条")
    if skipped:
        print(f"⚠️ 跳过   ：{json.dumps(skipped, ensure_ascii=False)}"
              f"（包内没有这些语向的接线；见文件头「与打包的关系」）")
    if not covered:
        die("包内接线与数据集没有任何交集，验不出东西")

    module = load_reference_module()
    reference_root = module.OPUS_DIR
    if not reference_root.is_dir():
        die(f"Python 参考实现要的模型目录不存在：{reference_root}")

    capi, dylib = ort_paths(args.ort)

    workdir = Path(tempfile.mkdtemp(prefix="vt-opus-verify-"))
    probe = workdir / "vt_probe"
    cases_path = workdir / "cases.tsv"
    try:
        build_probe(probe, dylib)
        write_cases(cases_path, covered)

        # ---- 参考译文（Python OpusBundle，逐句） ----
        print(f"\nPython 参考实现加载中…（{reference_root.relative_to(ROOT)}）")
        # routes 只传包内有的那几条：多传会去加载用不到的包，白白多等。
        reference = module.OpusBundle(root=reference_root,
                                      routes={k: tuple(v) for k, v in routes.items()})
        if reference.missing:
            print(f"  ⚠️ 参考实现缺包：{reference.missing}")
        expected = [reference.translate(case) for case in covered]
        reference.close()
        print(f"  完成 {len(expected)} 条")

        # ---- 端侧译文（C++ 核心 + 端侧包） ----
        print("\n端侧 C++ 核心跑同一批…")
        env = dict(os.environ)
        env["DYLD_LIBRARY_PATH"] = str(capi) + ":" + env.get("DYLD_LIBRARY_PATH", "")
        result = subprocess.run([str(probe), str(package), str(cases_path)],
                                capture_output=True, text=True, env=env)
        if result.returncode != 0:
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            die(f"探针退出码 {result.returncode}")
        actual = read_probe_output(result.stdout)
        # 探针把「已装载」摘要写 stderr，顺手带出来给人看。
        for line in result.stderr.splitlines():
            if line.strip():
                print("  " + line.strip())

        if len(actual) != len(covered):
            die(f"端侧返回 {len(actual)} 条，与输入 {len(covered)} 条不符")
    finally:
        if args.keep_probe:
            kept = ROOT / ".tools" / "vt_probe"
            kept.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(probe, kept)
            print(f"\n探针副本：{kept.relative_to(ROOT)}")
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    # ---- 逐条比对 ----
    # 顺序一一对应：探针严格按输入行序输出，上面已经断言过回显的三列与输入一致。
    rows = []
    mismatch = 0
    for index, (case, (src, tgt, text, got)) in enumerate(zip(covered, actual)):
        # 源语 / 目标语 / 原文是探针自己回显的，顺便核一下有没有串行错位。
        assert (src, tgt, text) == (case["source_lang"], case["target_lang"],
                                    case["source"]), "探针输出与输入顺序不一致"
        want = expected[index]
        same = got == want
        if not same:
            mismatch += 1
        rows.append({"id": case.get("id", ""), "direction": f"{src}-{tgt}",
                     "source": text, "reference_get": want, "native_get": got,
                     "match": same})

    if args.json:
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        print(f"\n逐条结果：{args.json}")

    print()
    print("=" * 78)
    print(f"{'方向':<8} {'原文':<34} {'结论'}")
    print("-" * 78)
    for row in rows:
        mark = "✓ 一致" if row["match"] else "✗ 不一致"
        print(f"{row['direction']:<8} {row['source'][:32]:<34} {mark}")
    print("=" * 78)

    total = len(rows)
    print(f"\n一致 {total - mismatch}/{total}（{total - mismatch} 逐字相同）")
    if skipped:
        print("未覆盖：" + "、".join(f"{k}×{v}" for k, v in sorted(skipped.items())))
    if mismatch:
        print("\n不一致的逐条对照：")
        for row in rows:
            if row["match"]:
                continue
            print(f"\n  [{row['id']}] {row['direction']}  {row['source']}")
            print(f"    Python：{row['reference_get']}")
            print(f"    端侧  ：{row['native_get']}")
        return 1
    print("\n端侧与 Python 参考实现逐字一致 —— 这个包可以打进 App 了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
