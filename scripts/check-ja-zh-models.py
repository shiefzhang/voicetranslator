#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日→中：**非官方直译模型** vs **官方两跳 pivot** vs **Qwen 通用模型** 的三方对比门禁。

## 为什么要单独一个脚本
`check-nmt-quality.py` 是「一个 NMT vs Qwen」的两方结构（只有 `len(results)==2` 时才算门禁）。
本次要回答的是**三方**问题：

    ① 非官方 ja→zh 直译（社区微调，如 Nekofox/M2M100-ja-zh，自转 int8 ONNX）
    ② 官方路线：ja→en→zh 两跳 pivot（两个官方 base 包，误差叠加）
    ③ 现役 Qwen2.5-1.5B（生产 prompt）

所以复用 `check-nmt-quality.py` 里的**引擎与度量**（不另立一套），只在外面套一层三方编排。

## 公平性（与既有门禁同一口径）
- 三侧都测「模型常驻 + 预热完成后」的单句端到端延迟，取 P50 / P95。
- Qwen 侧用**生产 prompt**（system + 3 组 few-shot）—— 用户实际遭遇的就是这条路径。
- NMT 两侧直接吃原文（pivot 吃的是上一跳的译文），都是 greedy。
- 线程数三侧同一个 `--threads`。
- 判据沿用仓库既有的 `poc.gate`（关键错误率降 ≥20% 或 P95 降 ≥35%）。

## 用法
    # 只跑两侧 NMT（不编 llama-server，最快）
    python3 scripts/check-ja-zh-models.py --nmt-only
    # 三方全套
    python3 scripts/check-ja-zh-models.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import translation_poc as poc  # noqa: E402


def _load_dashed(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


CNQ = _load_dashed("cnq", ROOT / "scripts" / "check-nmt-quality.py")
CTQ = CNQ.CTQ

# ⚠️ 仓库内置两语文料去重后**只有 4 条** ja-zh 用例（判定粒度 25%，区分不出差异），
# 所以默认再加一份自撰补充语料（16 条）。自撰语料按仓库口径**只能否决、不能采纳** ——
# 也就是说：直译模型若在补充语料上明显更差，这个结论成立；明显更好则还需要门禁级语料复核。
DEFAULT_DATASETS = [
    ROOT / "evaluation" / "zh_en_ja_sample.jsonl",
    ROOT / "evaluation" / "zh_ja_ko_sample.jsonl",
    ROOT / "evaluation" / "ja_zh_supplement.jsonl",
]
FT_DIR = ROOT / ".tools" / "ja-zh" / "Nekofox-M2M100-ja-zh-onnx"
REPORT = ROOT / "dist" / "verification" / "ja-zh-compare.json"


def load_ja_zh(datasets: list[Path], direction: str) -> list[dict]:
    """从多个语料文件里取指定语向的用例，按 id 去重（后出现的覆盖先出现的）。"""
    by_id: dict[str, dict] = {}
    for path in datasets:
        for case in CNQ.load_cases(path):
            if f'{case["source_lang"]}-{case["target_lang"]}' == direction:
                by_id[case["id"]] = case
    if not by_id:
        raise SystemExit(f"这些语料里没有 {direction} 的用例：{[str(p) for p in datasets]}")
    return [by_id[k] for k in sorted(by_id)]


def main() -> int:
    ap = argparse.ArgumentParser(description="日→中：直译模型 / 两跳 pivot / Qwen 三方对比")
    ap.add_argument("--dataset", type=Path, action="append", default=None,
                    help="可重复；默认用内置两份语料里的 ja-zh 用例")
    ap.add_argument("--direction", default="ja-zh")
    ap.add_argument("--ft-dir", type=Path, default=FT_DIR,
                    help="非官方 ja→zh 直译模型的 ONNX 目录")
    ap.add_argument("--with-base-m2m", action="store_true",
                    help="加一个**对照组**：基准 m2m100_418M（多语种，未针对日中微调，"
                         "仓库里已有 int8 ONNX）。有它才能把「直译 vs 两跳」与"
                         "「社区微调 vs 未微调」两个变量拆开。默认不加。")
    ap.add_argument("--pivot", default="ja-en+en-zh", help="两跳 pivot 的接线")
    ap.add_argument("--opus-dir", type=Path, default=CNQ.OPUS_DIR)
    ap.add_argument("--variant", default="_int8")
    ap.add_argument("--nmt-only", action="store_true", help="只跑 NMT 两侧（不编 llama-server）")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--arch", default=CTQ.DEFAULT_ARCH)
    ap.add_argument("--accuracy-threshold", type=float, default=.45)
    ap.add_argument("--qwen-model", type=Path,
                    default=ROOT / "models" / "qwen25-1.5b-q4km-zh-ja-ko-en-yue.vtmodel")
    ap.add_argument("--port", type=int, default=18093)
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()

    datasets = args.dataset or DEFAULT_DATASETS
    cases = load_ja_zh(datasets, args.direction)
    print(f"语料：{len(cases)} 条 {args.direction} 用例"
          f"（来自 {[p.name for p in datasets]}）\n")

    hops = tuple(h.strip() for h in args.pivot.split("+") if h.strip())
    results, engines = [], []
    try:
        # ① 非官方直译：M2M-100 社区微调，自转 int8 ONNX
        ft = CNQ.M2M100Engine(args.ft_dir, threads=args.threads)
        ft.name = f"{args.ft_dir.name}-int8(ort)"
        print(f"① 直译模型就绪：{ft.describe()}\n     装载 {ft.load_ms:.0f} ms\n")
        engines.append(ft)

        # ② 官方两跳 pivot
        bundle = CNQ.OpusBundle(args.opus_dir, threads=args.threads,
                                routes={args.direction: hops}, variant=args.variant,
                                max_new_tokens=128)
        bundle.name = f"opus-pivot[{'+'.join(hops)}](ort)"
        print(f"② 两跳 pivot 就绪：{bundle.describe()}\n     装载 {bundle.load_ms:.0f} ms\n")
        engines.append(bundle)

        # ②b 对照组（可选）：基准 m2m100_418M ——「直译」但不为日中微调。
        # 与 ① 配对，才能回答「社区微调到底带来了多少增益」。
        # ⚠️ 必须插在 pivot 之后、Qwen 之前：后面用 results[0]=直译、results[1]=pivot、
        #    results[-1]=Qwen 定位，顺序不能乱。
        if args.with_base_m2m:
            base = CNQ.M2M100Engine(CNQ.NMT_DIR, threads=args.threads)
            base.name = "m2m100-418m-base-int8(ort)"
            print(f"②b 对照（未微调基准）就绪：{base.describe()}\n"
                  f"     装载 {base.load_ms:.0f} ms\n")
            engines.append(base)

        # ③ Qwen
        if not args.nmt_only:
            cmake = CTQ.find_cmake()
            server = CNQ.build_server(cmake, args.jobs, args.arch)
            model = CTQ.resolve_model(args.qwen_model)
            print(f"③ Qwen 模型：{model}（arch={CTQ.gguf_arch(model) or '?'}）", flush=True)
            engines.append(CNQ.QwenServerEngine(server, model, args.port, args.threads,
                                                args.arch))

        for engine in engines:
            print(f"跑 {engine.name} …", flush=True)
            results.append(CNQ.evaluate(engine, cases, args.accuracy_threshold))
    finally:
        for engine in engines:
            try:
                engine.close()
            except Exception:
                pass

    # ---------------- 译文对照
    print("\n### 译文对照\n")
    for case in cases:
        print(f'**{case["id"]}**（{args.direction}）：{case["source"]}')
        print(f'  参考：{case["reference"]}')
        for res in results:
            row = next(r for r in res["rows"] if r["id"] == case["id"])
            mark = "✓" if row["critical_pass"] else "✗"
            print(f'  {mark} {res["engine"]:34} {row["output"]}')
        print()

    # ---------------- 汇总
    print("### 汇总\n")
    print("| 引擎 | 关键错误率 | 平均 chrF | P50 ms | P95 ms | 包体 | 装载 ms |")
    print("|---|---|---|---|---|---|---|")
    for res in results:
        pkg = res["package_bytes"]
        pkg_txt = f'{pkg / 1e6:.0f} MB' if pkg else "-"
        lat = res["latency_ms"]
        print(f'| {res["engine"]} | {res["critical_error_rate"]:.2%} | {res["mean_chrf"]:.4f} | '
              f'{lat["p50"]:.0f} | {lat["p95"]:.0f} | {pkg_txt} | {res["load_ms"]:.0f} |')
    print()

    report = {"schema_version": 1, "direction": args.direction,
              "datasets": [str(p) for p in datasets], "results": results}

    # ---------------- 门禁：两个 NMT 分别对 Qwen
    if len(results) >= 2:
        qwen = results[-1] if not args.nmt_only else None
        print("### 门禁（NMT 相对 Qwen）\n")
        if qwen is not None:
            print("| 引擎 | 关键错误率降幅 | P95 降幅 | 是否 promote |")
            print("|---|---|---|---|")
            gates = {}
            for res in results[:-1]:
                g = poc.gate(qwen, res)
                gates[res["engine"]] = g
                print(f'| {res["engine"]} | {g["critical_error_reduction"]:.2%} | '
                      f'{g["p95_latency_reduction"]:.2%} | {"✅ 是" if g["promote_opus"] else "❌ 否"} |')
            print(f'\n判据：{poc.gate(qwen, results[0])["rule"]}')
            report["gates_vs_qwen"] = gates
        # 直译 vs pivot 的直接对比（同一基线，谁更优）
        if len(results) >= 2:
            direct, pivot = results[0], results[1]
            report["direct_vs_pivot"] = poc.gate(pivot, direct)
            d = report["direct_vs_pivot"]
            print(f'\n**直译 vs 两跳 pivot**：关键错误率降幅 {d["critical_error_reduction"]:.2%}，'
                  f'P95 降幅 {d["p95_latency_reduction"]:.2%}，'
                  f'直译更优={d["promote_opus"]}')
    print()

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入 {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
