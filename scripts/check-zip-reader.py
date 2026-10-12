#!/usr/bin/env python3
"""用**真实的** iOS 读器去解真实与合成的 `.vtmodel`，验 「>2 GiB 还能不能读」。

## 为什么单独做这件事

iOS 侧不走 `ZipInputStream`，而是自研的随机访问 ZIP64 读器
（`ios/VoiceTranslator/Bridge/VTZip.mm`）。也就是说：

- 编译期的四道门禁（`build-ggml-cpu-variants.sh`）管不到它；
- Android 侧的语法检查管不到它；
- 它只在真机导入模型包时才被执行 —— 而「导入 2 GB 的包失败」正是最难在真机上
  反复试的场景（一次要传 2 GB、解 2 GB、验 2 GB）。

所以把 `VTZip.mm` **原样**编进一个 macOS 命令行探针里跑。这是同一份源码、同一套
算法、同样的 zlib 与 CommonCrypto，差别只在宿主。`VTZip.mm` 里没有任何
UIKit / 沙盒依赖，所以这个搬迁是成立的。

## 门禁

| 门禁 | 判据 | 为什么 |
| --- | --- | --- |
| A 打开 | `archiveAtPath:` 非 nil | 中央目录（含 ZIP64 记录）解析成功 |
| B 条目名 | 全部通过 `VTSafeArchivePath` | 与两端 App 导入校验同一条规则 |
| C 逐条解出 | `extractEntryNamed:` 返回 YES | size 与 SHA-256 都由它内部核对 |
| D 条目数 | 中央目录条目数 == 清单 files + 1 | 少一条 = App 会少解一个文件 |
| E 越过悬崖 | 合成包的 cdOffset > 2^31 | 证明真的在验 >2 GiB 那一段，而不是自欺 |

E 尤其重要：本机最大的真实包（TranslateGemma 4B Q3_K_M）中央目录偏移是
**2,098,495,0xx**，距 2^31 只剩约 49 MB —— 它**没有**越过悬崖。所以必须合成一个
越过的（见 scripts/make-large-package.py，用稀疏填充，磁盘只占几 KB）。

## 用法

    python3 scripts/check-zip-reader.py                     # 合成包 + models/ 下全部真实包
    python3 scripts/check-zip-reader.py --synthetic-only    # 只验合成的大包（快）
    python3 scripts/check-zip-reader.py --skip-over 200000000   # 跳过 >200 MB 的条目（省 I/O）
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

CLIFF = 0x80000000
PROBE_SRC = ROOT / "native" / "zip" / "vt_zip_probe.mm"
VTZIP_SRC = ROOT / "ios" / "VoiceTranslator" / "Bridge" / "VTZip.mm"
BRIDGE_INC = ROOT / "ios" / "VoiceTranslator" / "Bridge"
BUILD_DIR = ROOT / ".tools" / "zip-reader"
DEFAULT_REPORT = ROOT / "dist" / "verification" / "zip-reader.json"


def log(message: str) -> None:
    print(message, flush=True)


def die(message: str) -> "NoReturn":  # noqa: F821
    print(f"错误：{message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def compile_probe() -> Path:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    binary = BUILD_DIR / "vt_zip_probe"
    cmd = [
        "clang++", "-std=c++17", "-fobjc-arc", "-O1", "-g0",
        "-Wall", "-Wextra", "-Wno-unused-parameter",
        "-x", "objective-c++",
        "-I", str(BRIDGE_INC),
        str(PROBE_SRC), str(VTZIP_SRC),
        "-framework", "Foundation", "-lz",
        "-o", str(binary),
    ]
    log(f"编译探针：{' '.join(cmd[0:6])} …")
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        print(done.stdout)
        print(done.stderr)
        die("探针编译失败")
    if done.stderr.strip():
        log(done.stderr.strip())
    return binary


def make_synthetic(target: Path) -> dict:
    import importlib.util
    path = ROOT / "scripts" / "make-large-package.py"
    spec = importlib.util.spec_from_file_location("make_large_package", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    info = module.build(target)
    if not info["crossesCliff"]:
        die(f"合成包的中央目录偏移 {info['cdOffset']} 没越过 2^31，这个测试没有意义")
    return info


def run_probe(binary: Path, packages: list[Path], out_dir: Path, skip_over: int) -> dict:
    cmd = [str(binary), "--out", str(out_dir)]
    if skip_over:
        cmd += ["--skip-over", str(skip_over)]
    cmd += [str(p) for p in packages]
    done = subprocess.run(cmd, capture_output=True, text=True)

    rows: list[dict] = []
    for line in done.stdout.splitlines():
        if line.startswith("VT_ZIP_PROBE "):
            row = {"kind": "package"}
            for token in line.split()[1:]:
                key, _, value = token.partition("=")
                row[key] = value
            rows.append(row)
        elif line.startswith("VT_ZIP_PROBE_ENTRY "):
            row = {"kind": "entry", "skipped": "0"}
            for token in line.split()[1:]:
                key, _, value = token.partition("=")
                row[key] = value
            rows.append(row)
        elif line.startswith("VT_ZIP_PROBE_DONE "):
            row = {"kind": "done"}
            for token in line.split()[1:]:
                key, _, value = token.partition("=")
                row[key] = value
            rows.append(row)
        elif line.startswith("VT_ZIP_PROBE_SUMMARY "):
            row = {"kind": "summary"}
            for token in line.split()[1:]:
                key, _, value = token.partition("=")
                row[key] = value
            rows.append(row)

    if not rows or done.returncode not in (0, 1) or not any(r["kind"] == "summary" for r in rows):
        print(done.stdout[-4000:])
        print(done.stderr[-4000:], file=sys.stderr)
        die("探针没有产出可解析的结果（编译出来的二进制可能根本没跑起来）")

    return {
        "exitCode": done.returncode,
        "stderr": done.stderr.strip(),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="用真实 iOS 读器验证 >2 GiB 的 .vtmodel")
    parser.add_argument("packages", nargs="*", type=Path,
                        help="要验的真实包（默认 models/ 下全部 *.vtmodel）")
    parser.add_argument("--synthetic-only", action="store_true", help="只验合成的 >2 GiB 包")
    parser.add_argument("--skip-over", type=int, default=0,
                        help="跳过大于该字节数的条目（会记为未通过，仅用于快速冒烟）")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    real = [] if args.synthetic_only else (
        [p.resolve() for p in args.packages] if args.packages
        else sorted((ROOT / "models").glob("*.vtmodel")))
    for p in real:
        if not p.is_file():
            die(f"找不到包：{p}")

    binary = compile_probe()

    work = Path(tempfile.mkdtemp(prefix="vt-zip-reader-"))
    failures = []
    try:
        targets = []
        synthetic = None
        if not args.synthetic_only or not real:
            synthetic = make_synthetic(ROOT / "dist" / "large" / "synthetic-over-2g.vtmodel")
            log(f"合成包：{synthetic['fileBytes']:,} 字节（磁盘 {synthetic['diskBytes']:,} 字节，稀疏）")
            log(f"  中央目录偏移 {synthetic['cdOffset']:,} > 2^31  ✅ 真的越过了悬崖")

        targets = real + ([Path(synthetic["path"])] if synthetic else [])
        if not targets:
            die("没有任何要验的包")

        out = work / "extract"
        out.mkdir()
        result = run_probe(binary, targets, out, args.skip_over)

        # ---- 按包汇总 ----
        by_pkg: dict[str, dict] = {}
        for row in result["rows"]:
            if row["kind"] == "package":
                by_pkg[row["package"]] = {"package": row, "entries": [], "done": None}
        for row in result["rows"]:
            if row["kind"] == "entry":
                by_pkg.setdefault(row["package"], {"package": None, "entries": [], "done": None})
                by_pkg[row["package"]]["entries"].append(row)
            elif row["kind"] == "done":
                by_pkg.setdefault(row["package"], {"package": None, "entries": [], "done": None})
                by_pkg[row["package"]]["done"] = row

        log("")
        log(f"{'包':<52}{'条目':>6}{'解出':>6}{'跳过':>6}{'字节':>16}{'打开 ms':>9}")
        log("-" * 96)
        gate_fail = 0
        for path, info in by_pkg.items():
            head, done = info["package"], info["done"]
            n_entries = int(head["entries"]) if head else -1
            extracted = int(done["extracted"]) if done else -1
            skipped = int(done["skipped"]) if done else -1
            total = int(done["bytes"]) if done else 0
            open_ms = int(head["open_ms"]) if head else -1
            # 门禁 D：中央目录条目数必须等于「解出 + 跳过 + 1(manifest.json)」
            ok = done and n_entries == extracted + skipped + 1 and skipped == 0
            if not ok:
                gate_fail += 1
            log(f"{Path(path).name:<52}{n_entries:>6}{extracted:>6}{skipped:>6}"
                f"{total:>16,}{open_ms:>9}{'' if ok else '   ❌'}")

        if result["stderr"].strip():
            log("")
            log("探针 stderr：")
            for line in result["stderr"].splitlines():
                log("  " + line)

        cross = synthetic["cdOffset"] > CLIFF if synthetic else None
        passed = result["exitCode"] == 0 and gate_fail == 0 and cross is not False
        if not passed:
            failures.append("探针退出码或条目数门禁未通过")

        report = {
            "status": "passed" if passed else "failed",
            "probeExitCode": result["exitCode"],
            "entryCountGateFailures": gate_fail,
            "synthetic": synthetic,
            "packages": [
                {"path": path, "entries": int(by_pkg[path]["package"]["entries"]) if by_pkg[path]["package"] else None,
                 "extracted": int(by_pkg[path]["done"]["extracted"]) if by_pkg[path]["done"] else None,
                 "bytes": int(by_pkg[path]["done"]["bytes"]) if by_pkg[path]["done"] else 0}
                for path in by_pkg
            ],
            "note": "由 scripts/check-zip-reader.py 驱动 android 之外的 iOS 读器（VTZip.mm 原样编译）。"
                    "合成的 >2 GiB 包用稀疏填充，磁盘占用接近 0。",
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log("")
        log(("✅ 通过" if passed else "❌ 未通过") + f"，报告 → {args.report}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
