#!/usr/bin/env python3
"""pack-opus-models.py —— 把 `.tools/opus-package` 打成 `models/<id>.vtmodel`。

## 与 export-opus-package.py 的分工（别混）

| 脚本 | 输入 | 输出 | 干什么 |
| --- | --- | --- | --- |
| `export-opus-package.py` | `.tools/opus/<单包>/`（HF 原始下载） | `.tools/opus-package/` | 重活：ONNX 拷贝、`.spm` 拆文本词表、两族 id 空间摊平、编译 NFKC 差分表 |
| `pack-opus-models.py`（本脚本） | `.tools/opus-package/` | `models/opus-*.vtmodel` | 打包：补 `licenses/` 与 `manifest.json`，然后交给 `model_pack.build` |

本脚本**不碰模型内容**，也不重新分词 —— 那样会把「端侧加载的到底是什么」变成两个
可能漂移的来源。它读 `routes.txt`（端侧真正的语向数据源）决定要哪些单包，
再把它们原样搬进 staging。

## routes 的默认集合就是端侧实际会用的那一组

`export-opus-package.py` 的 `DEFAULT_ROUTES` 含 `ja-zh`（`ja-en` → `en-zh` 两跳 pivot）。
注意 pivot 语向在包内落的是**单包**目录（`routes/ja-en/` + 复用的 `routes/en-zh/`），
只有 `routes.txt` 里才记成一条 `ja-zh`。

**默认装四条**：

    zh-en（中→英）  en-zh（英→中）  zh-ja（中→日）  ja-zh（日→中，两跳）

合计约 715 MB。之所以连质量最弱的 ja-zh 也带上：门禁实测它关键错误率
75% vs Qwen 50%，但 P50 延迟 69 ms vs 1405 ms（快约 20×），2026-10-09 起由
`TranslationBackend.hybrid` 交给 OPUS —— 用速度换质量。包里有什么、App 就只会用什么，
不存在「装了但永远用不到」的部分。
要退回三条（不打 ja-zh）：先跑
`export-opus-package.py --routes zh-en,en-zh,zh-ja`，再跑本脚本即可。

## 用法

    python3 scripts/pack-opus-models.py                    # 打包（默认 id/name）
    python3 scripts/pack-opus-models.py --list             # 只看会打什么，不写盘
    python3 scripts/pack-opus-models.py --id opus-mt-v2 --name "OPUS-MT 中英日"
    python3 scripts/pack-opus-models.py --out models/x.vtmodel

需要 `export-opus-package.py` 先跑过（`.tools/opus-package/routes.txt` 存在）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from model_pack import build  # noqa: E402

PACKAGE_DIR = ROOT / ".tools" / "opus-package"
STAGING_DIR = ROOT / "model-work" / "opus"
MANIFEST_DIR = ROOT / "model-manifests"
LICENSE_CACHE = ROOT / "downloads" / "licenses"

DEFAULT_ID = "opus-mt-zh-en-ja-v1"
DEFAULT_NAME = "OPUS-MT 中英日 专用翻译"
# 包**内容**的版本（与 id 的 `-v1` 后缀不是一回事：id 描述语向集合，这个描述这一版包里
# 装了哪几条接线）。1.0.0 = zh-en / en-zh / zh-ja 三条；1.1.0 = 加入 ja-zh 两跳 pivot。
DEFAULT_VERSION = "1.1.0"

# 五语契约的固定顺序 —— manifest 里的 languages 必须按它排，端侧才不用排序。
VT_ORDER = ["zh", "ja", "ko", "en", "yue"]

# 每个单包的上游与许可（**逐包不同**，不能一句「CC-BY」盖过去 —— 见 ENGINEERING-NOTES）。
# upstream 用于归属表；spdx 决定要往 licenses/ 里放哪几份全文。
HOPS = {
    "zh-en": {"upstream": "Helsinki-NLP/opus-mt-zh-en", "spdx": "cc-by-4.0"},
    "en-zh": {"upstream": "Helsinki-NLP/opus-mt-en-zh", "spdx": "apache-2.0"},
    "zh-ja": {"upstream": "Helsinki-NLP/opus-mt-tc-big-zh-ja", "spdx": "cc-by-4.0"},
    "ja-en": {"upstream": "Helsinki-NLP/opus-mt-ja-en", "spdx": "apache-2.0"},
    "en-ja": {"upstream": "Helsinki-NLP/opus-mt-en-jap", "spdx": "apache-2.0"},
}

# 许可全文的文件名 -> 需要时从 LICENSE_CACHE 拷进包，缺失就退回「只写 SPDX + URL」。
LICENSE_TITLES = {
    "apache-2.0": ("LICENSE-Apache-2.0.txt", "Apache License 2.0",
                   "https://www.apache.org/licenses/LICENSE-2.0.txt"),
    "cc-by-4.0": ("LICENSE-CC-BY-4.0.txt", "Creative Commons Attribution 4.0 International",
                  "https://creativecommons.org/licenses/by/4.0/legalcode.txt"),
}

# 包内除模型外还带的自有组件（与模型许可分开列，别让人以为模型是 MIT）
BUNDLED_COMPONENTS = [
    ("simple-sentencepiece", "Apache-2.0", "https://github.com/k2-fsa/sherpa-onnx",
     "分词（vendor 进 native/nmt/third_party/simple-sentencepiece，126 KB 静态库）"),
    ("ONNX Runtime", "MIT", "https://github.com/microsoft/onnxruntime",
     "推理运行时（两端随 sherpa-onnx 分发，本包不含其二进制）"),
    ("NFKC 规范化表", "Public Domain",
     "https://www.unicode.org/reports/tr15/",
     "norm/nfkc.bin，由 Python unicodedata 逐码点生成（4928 条差分）"),
]


def die(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def read_routes(package_dir: Path) -> "list[tuple[str, list[str]]]":
    """读 routes.txt：`语向<TAB>跳1,跳2`。这是端侧真正的语向数据源。"""
    path = package_dir / "routes.txt"
    if not path.is_file():
        die(f"读不到 {path.relative_to(ROOT)}；先跑 scripts/export-opus-package.py")
    routes: list[tuple[str, list[str]]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            die(f"routes.txt 第 {lineno} 行格式不对（应为 `语向<TAB>跳1,跳2`）：{raw!r}")
        hops = [h.strip() for h in parts[1].split(",") if h.strip()]
        if not hops:
            die(f"routes.txt 第 {lineno} 行没有跳数：{raw!r}")
        routes.append((parts[0].strip(), hops))
    if not routes:
        die("routes.txt 里没有任何语向")
    return routes


def languages_of(routes) -> list[str]:
    """语向并集，按五语契约顺序排（端侧不做排序，顺序必须确定）。"""
    seen = set()
    for key, _ in routes:
        for code in key.split("-"):
            seen.add(code)
    unknown = seen - set(VT_ORDER)
    if unknown:
        die(f"routes.txt 里出现未知语种代码：{sorted(unknown)}")
    return [code for code in VT_ORDER if code in seen]


def write_licenses(staging: Path, package_id: str, hops: list[str], routes) -> "list[str]":
    """写 licenses/，返回缺全文的 SPDX 列表（调用方据此提示）。"""
    licenses = staging / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)

    used = sorted({HOPS[h]["spdx"] for h in hops if h in HOPS})
    missing = []
    for spdx in used:
        filename, _, url = LICENSE_TITLES[spdx]
        source = LICENSE_CACHE / f"{spdx}.txt"
        if source.is_file():
            shutil.copyfile(source, licenses / filename)
        else:
            missing.append(spdx)
            (licenses / filename).write_text(
                f"本文件应为 {LICENSE_TITLES[spdx][1]} 全文，但本机没有缓存副本。\n"
                f"请从 {url} 获取后放到 {source.relative_to(ROOT)}，再重跑本脚本。\n",
                encoding="utf-8")

    # 归属表：哪个语向 -> 哪个上游仓库 -> 什么许可。这是「逐包登记」的落地处。
    lines = [
        "VoiceTranslator 专用翻译模型包（kind=opus）第三方归属",
        "=" * 64,
        "",
        f"模型包 id：{package_id}",
        "引擎：OPUS-MT（Marian），ONNX 导出后 int8 量化，端侧由 ONNX Runtime 推理。",
        "上游发布者：Helsinki-NLP (University of Helsinki, Language Technology Research Group)",
        "",
        "## 1. 语向 -> 模型 -> 许可",
        "",
        "⚠️ 同一系列的许可**逐包不同**，下表是按仓库逐个核对的结果，不要合并同类项。",
        "",
        "| 语向 | 跳数（单包顺序） | 上游仓库 | 许可 |",
        "| --- | --- | --- | --- |",
    ]
    for key, hop_list in routes:
        cells = []
        for hop in hop_list:
            info = HOPS.get(hop)
            cells.append(f"`{info['upstream']}` ({info['spdx']})" if info
                         else f"`{hop}`（未登记！）")
        lines.append(f"| {key} | {' → '.join(hop_list)} | {' → '.join(cells)} | "
                     f"{' / '.join(sorted({HOPS[h]['spdx'] for h in hop_list if h in HOPS}))} |")
    lines += [
        "",
        "两跳（pivot）表示官方没有直接的这一向模型，由英文中转两跳，误差会叠加。",
        "",
        "## 2. 许可全文",
        "",
    ]
    for spdx in used:
        filename, title, url = LICENSE_TITLES[spdx]
        lines.append(f"* `{filename}` —— {title}")
        lines.append(f"  <{url}>")
    lines += ["", "## 3. 包内其余组件", "",
              "| 组件 | 许可 | 来源 | 用途 |", "| --- | --- | --- | --- |"]
    for name, spdx, url, note in BUNDLED_COMPONENTS:
        lines.append(f"| {name} | {spdx} | <{url}> | {note} |")
    lines += [
        "",
        "## 4. 再分发注意",
        "",
        "* Apache-2.0 要求保留版权与许可声明 —— 本文件与 LICENSE-Apache-2.0.txt 即为此用途。",
        "* CC-BY-4.0 要求署名 —— 上表第 1 节的「上游仓库」列即署名信息，请勿删除。",
        "* 模型的 int8 量化由本项目自行完成（不是社区预量化包），",
        "  量化过程不改变许可条款。",
        "",
    ]
    (licenses / "THIRD-PARTY.txt").write_text("\n".join(lines), encoding="utf-8")

    # 顶层再放一份映射，方便不打开 licenses/ 也能看到。
    (licenses / "SOURCE.txt").write_text(
        "本模型包由 scripts/pack-opus-models.py 生成。\n"
        "详细归属与许可见 licenses/THIRD-PARTY.txt。\n"
        f"语向接线：{json.dumps({k: v for k, v in routes}, ensure_ascii=False)}\n",
        encoding="utf-8")
    return missing


def stage(package_dir: Path, staging: Path, routes) -> "tuple[list[str], int]":
    """把导出产物搬进 staging（模型原样搬，不重算）。返回 (hops, 字节数)。"""
    hops: list[str] = []
    for _, hop_list in routes:
        for hop in hop_list:
            if hop not in hops:
                hops.append(hop)

    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    # norm/ 与 routes.txt 是端侧加载的必需项（model_pack 的 REQUIRED_FILES）。
    shutil.copytree(package_dir / "norm", staging / "norm")
    shutil.copyfile(package_dir / "routes.txt", staging / "routes.txt")
    # routes.json 端侧不读，但带上便于真机排查 —— 只有 1 KB。
    if (package_dir / "routes.json").is_file():
        shutil.copyfile(package_dir / "routes.json", staging / "routes.json")

    total = 0
    for hop in hops:
        source = package_dir / "routes" / hop
        if not source.is_dir():
            die(f"routes.txt 用到了 {hop}，但 {source.relative_to(ROOT)} 不存在；"
                f"把该语向从 routes.txt 去掉，或重跑 export-opus-package.py")
        shutil.copytree(source, staging / "routes" / hop)
        total += sum(p.stat().st_size for p in source.iterdir() if p.is_file())

    # 导出目录里可能有本轮不用的单包残留（比如 ja-en），不复制就对了；
    # 但要说一声，免得以为「怎么少了一个」。
    present = {p.name for p in (package_dir / "routes").iterdir() if p.is_dir()}
    unused = sorted(present - set(hops))
    if unused:
        print(f"  跳过未接线的单包目录：{', '.join(unused)}"
              f"（routes.txt 没用到，不打包）")
    return hops, total


def main() -> int:
    parser = argparse.ArgumentParser(description="把 .tools/opus-package 打成 .vtmodel")
    parser.add_argument("--id", default=DEFAULT_ID, help=f"模型包 id（默认 {DEFAULT_ID}）")
    parser.add_argument("--name", default=DEFAULT_NAME, help="设置页显示名")
    parser.add_argument("--version", default=DEFAULT_VERSION,
                        help=f"包内容版本（默认 {DEFAULT_VERSION}）")
    parser.add_argument("--package-dir", default=str(PACKAGE_DIR),
                        help="端侧目录（默认 .tools/opus-package）")
    parser.add_argument("--staging", default=str(STAGING_DIR),
                        help="打包前的组装目录（默认 model-work/opus）")
    parser.add_argument("--out", default="", help="输出 .vtmodel 路径（默认 models/<id>.vtmodel）")
    parser.add_argument("--list", action="store_true", help="只列会打什么，不写盘")
    parser.add_argument("--keep-staging", action="store_true",
                        help="保留组装目录（默认保留，加 --clean 才删）")
    parser.add_argument("--clean", action="store_true", help="打完后删掉组装目录")
    args = parser.parse_args()

    package_dir = Path(args.package_dir).resolve()
    if not package_dir.is_dir():
        die(f"找不到端侧目录：{package_dir}；先跑 scripts/export-opus-package.py")

    routes = read_routes(package_dir)
    languages = languages_of(routes)
    staging = Path(args.staging).resolve()
    out = Path(args.out) if args.out else ROOT / "models" / f"{args.id}.vtmodel"
    if not out.is_absolute():
        out = (ROOT / out).resolve()

    print(f"端侧目录：{package_dir.relative_to(ROOT)}")
    print(f"语向接线（routes.txt）：{json.dumps({k: v for k, v in routes}, ensure_ascii=False)}")
    print(f"languages：{languages}")
    declared_hops = sorted({h for _, hl in routes for h in hl})
    print(f"需要单包：{declared_hops}")

    if args.list:
        print("\n--list：不写盘。")
        return 0

    hops, model_bytes = stage(package_dir, staging, routes)
    missing = write_licenses(staging, args.id, hops, routes)
    if missing:
        print(f"  ⚠️ 缺少 {'/'.join(missing)} 的许可全文缓存"
              f"（{LICENSE_CACHE.relative_to(ROOT)}），包里放的是占位说明。"
              f"补上后重跑本脚本即可。")

    manifest = {
        "schemaVersion": 1,
        "id": args.id,
        "name": args.name,
        "version": args.version,
        "kind": "opus",
        "engine": "opus-mt-marian",
        "runtime": "onnxruntime 1.27.1",
        # OPUS 是「一个语向一个模型」，包只覆盖它能翻的方向 —— 所以这里是五语的子集。
        "languages": languages,
        # 端侧不读这个字段（它读包内 routes.txt），但清单里留一份便于人工核对。
        "routes": declared_hops,
        "idMode": {hop: json.loads((staging / "routes" / hop / "route.json")
                                   .read_text(encoding="utf-8"))["idMode"] for hop in hops},
        "source": "https://huggingface.co/Helsinki-NLP",
        "note": "模型由 scripts/pack-opus-models.py 生成；归属与许可见 licenses/THIRD-PARTY.txt",
    }
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = MANIFEST_DIR / f"{args.id}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    print(f"  清单       写入 {manifest_path.relative_to(ROOT)}")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    # model_pack.build 自己会 rglob 组装目录、逐文件算 SHA-256、再 verify 一遍。
    build(staging, manifest_path, out)
    print(f"  模型合计   {model_bytes / 1e6:.1f} MB（不含清单与许可）")

    if args.clean and not args.keep_staging:
        shutil.rmtree(staging)
        print(f"  清理       移除 {staging.relative_to(ROOT)}")
    else:
        print(f"  组装目录   保留 {staging.relative_to(ROOT)}"
              f"（下次重打会先清掉；加 --clean 可打完后删除）")

    print("\n下一步：把这份包分发给用户（两端都不再内置任何模型包，由用户自行导入）")
    print("  · 体检它能不能被导入：python3 scripts/model-tiers.py")
    print("  · 要换掉推荐档位：改 scripts/model-tiers.py 顶部的 RECOMMENDED['opus']")
    return 0


if __name__ == "__main__":
    sys.exit(main())
