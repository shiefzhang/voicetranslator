#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 HF Marian（OPUS-MT）模型导出成端侧直接能用的 int8 ONNX。

## 为什么需要这个脚本
Xenova / onnx-community 只转了 **base** 语向（77M），**tc-big** 那几向
（`opus-mt-tc-big-zh-ja`、`opus-mt-tc-big-en-ko` …，209M）**没有任何现成 ONNX**，
只能自己用 optimum 转。本脚本一次做完「fp32 导出 → int8 动态量化 → 落成
`MarianEngine` 期望的文件名」。

## 产物（与 Xenova 的命名保持一致，这样 `scripts/check-nmt-quality.py` 不用改）
    <out>/encoder_model_int8.onnx
    <out>/decoder_model_merged_int8.onnx
    <out>/config.json source.spm target.spm vocab.json
    <out>/tokenizer_config.json special_tokens_map.json generation_config.json

## 用法
    # 先下原始权重
    python3 scripts/fetch-ms.py Helsinki-NLP/opus-mt-tc-big-zh-ja model.safetensors .tools/opus/zh-ja/model.safetensors
    # 再转（要 torch + optimum，本机 venv 已装）
    python3 scripts/export-marian-onnx.py .tools/opus/zh-ja .tools/opus/zh-ja-onnx

## ⚠️ 三个坑
1. **必须转 merged decoder**：端侧只用 `decoder_model_merged`（同一张图同时支持
   「首步不带 past」和「增量带 past」两个分支）。分开的 `decoder_model` +
   `decoder_with_past_model` 会让 bridge 复杂一倍，且 Xenova 自己也是只发 merged。
2. **🔴 必须传 `extra_options={"EnableSubgraph": True}`**：merged decoder 的真身在
   一个 `If` 节点里（`use_cache_branch` 选首步/增量分支），权重挂在顶层 initializer 上，
   但只被**子图内部的节点**引用。`quantize_dynamic` 是基础遍历器，只走
   `graph.node` 顶层，**不会进 `If` 的子图** —— 结果是「量化脚本成功返回、
   产物一个字节没缩小」，静默失败。
   - 实测症状：tc-big（209M）merged decoder fp32 = **670.0 MB**，
     量化后 **670.1 MB**；图里 `QuantizeLinear=0 / MatMulInteger=0`，
     initializer 类型统计是 `{FLOAT: 160}`，零个 INT8。
   - 对照：base 族的 merged decoder 是 Xenova 预先量化好的（57 MB），所以从没暴露过。
   - 判据：**量化后 encoder 缩了、decoder 没缩 → 一定是 If 子图没进去。**
     脚本现在会自己断言压缩比，不达标直接抛错。
3. **量化后必须验证**：int8 动态量化对 Marian 的 `embed_scale` / layernorm 不敏感，
   但历史上出现过「量化后 eos 不再触发、输出复读」的情况。转完一定要用
   `--verify` 跑几个句子对比 fp32 输出。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# 跟着原模型一起复制的文件（MarianEngine 加载时按名查找）
SIDECAR = ("config.json", "generation_config.json", "tokenizer_config.json",
           "special_tokens_map.json", "vocab.json", "source.spm", "target.spm")


def export_fp32(src: Path, out: Path) -> None:
    from optimum.exporters.onnx import main_export

    out.mkdir(parents=True, exist_ok=True)
    print(f"[1/3] fp32 导出：{src} → {out}")
    # ⚠️ 参数名别猜：`main_export` 只有 `no_post_process`（默认 False）与 `do_validation`，
    #    写成 `do_post_process` / `no_validation` 会被静默吞掉 —— 然后你就拿不到
    #    `decoder_model_merged.onnx`（端侧唯一用的那张图），只剩 model + with_past 两张。
    main_export(
        model_name_or_path=str(src),
        output=str(out),
        task="text2text-generation-with-past",   # Marian 的 Optimum 任务名
        opset=17,
        do_validation=False,                     # 导出后自校验很慢，质量我们自己验
    )
    merged = out / "decoder_model_merged.onnx"
    if not merged.is_file():
        raise RuntimeError(
            f"没生成 {merged.name}；实际产物：{sorted(p.name for p in out.glob('*.onnx'))}"
            "（检查 no_post_process 是否被设成 True）")


def quantize(out: Path, min_ratio: float = 1.6) -> None:
    """int8 动态量化。

    ⚠️ `EnableSubgraph=True` 是**必须的**：merged decoder 是「一个 If 包住全部节点、
    权重挂在顶层 initializer」的结构，默认遍历器不会进子图，会**静默地什么都不做**。
    """
    from onnxruntime.quantization import QuantType, quantize_dynamic

    print("[2/3] int8 动态量化（encoder + merged decoder，含 If 子图）")
    for stem in ("encoder_model", "decoder_model_merged"):
        src = out / f"{stem}.onnx"
        if not src.is_file():
            raise FileNotFoundError(f"{src} 不存在 —— optimum 没导出 merged decoder？"
                                    f"（目录内容：{sorted(p.name for p in out.glob('*.onnx'))}）")
        dst = out / f"{stem}_int8.onnx"
        quantize_dynamic(str(src), str(dst), weight_type=QuantType.QInt8,
                         extra_options={"EnableSubgraph": True})
        ratio = src.stat().st_size / max(dst.stat().st_size, 1)
        print(f"      {dst.name}  {dst.stat().st_size / 1e6:.1f} MB"
              f"  (fp32 {src.stat().st_size / 1e6:.1f} MB, {ratio:.2f}x)")
        if ratio < min_ratio:
            raise RuntimeError(
                f"{stem}: 量化后只缩到 {ratio:.2f}x，明显没生效。"
                "检查是否漏传 extra_options={'EnableSubgraph': True}"
                "（merged decoder 的权重在 If 子图里，默认遍历器进不去）")


def assert_quantized(out: Path) -> None:
    """从图结构上确认 int8 真的落进去了 —— 比看文件大小更硬。"""
    import collections
    import onnx
    from onnx import TensorProto

    for name in ("encoder_model_int8.onnx", "decoder_model_merged_int8.onnx"):
        path = out / name
        model = onnx.load(str(path), load_external_data=False)
        int8 = 0

        def walk(graph):
            nonlocal int8
            for t in graph.initializer:
                if t.data_type == TensorProto.INT8:
                    int8 += 1
            for node in graph.node:
                for attr in node.attribute:
                    if attr.HasField("g"):
                        walk(attr.g)
                    for g in attr.graphs:
                        walk(g)

        walk(model.graph)
        if int8 == 0:
            raise RuntimeError(f"{name}: 图里一个 INT8 initializer 都没有 —— 量化没生效")
        print(f"      校验 {name}: INT8 initializer {int8} 个 ✅")


def copy_sidecar(src: Path, out: Path) -> None:
    print("[3/3] 复制词表与配置")
    for name in SIDECAR:
        s = src / name
        if not s.is_file():
            print(f"      ⚠️ 缺 {name}（源目录没有）")
            continue
        d = out / name
        # 就地导出（src == out）时必须跳过 —— shutil.copy2 同文件会抛 SameFileError，
        # 而那时量化已经跑完，白等一场
        if s.resolve() == d.resolve():
            continue
        shutil.copy2(s, d)


def verify(out: Path, sentences: list[str], threads: int) -> None:
    """用导出后的 int8 跑几句，确认没有复读 / 空输出 / eos 失效。"""
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "cnq", root / "scripts" / "check-nmt-quality.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cnq"] = mod
    spec.loader.exec_module(mod)

    eng = mod.MarianEngine(out, threads=threads)
    print(f"\n[验证] {eng.describe()}")
    for text in sentences:
        out_text = eng.translate_text(text)
        flag = "⚠️ 空/复读" if (not out_text or len(set(out_text.split())) == 1) else "✅"
        print(f"  {flag} {text!r}\n     -> {out_text!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="HF Marian → 端侧 int8 ONNX")
    ap.add_argument("src", type=Path, help="含 model.safetensors 的 HF 模型目录")
    ap.add_argument("out", type=Path, help="输出目录")
    ap.add_argument("--skip-export", action="store_true", help="已有 fp32 ONNX，只做量化+复制")
    ap.add_argument("--keep-fp32", action="store_true",
                    help="保留 fp32 中间产物（默认删，一个 209M 模型的 fp32 要 1 GB+）")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--verify", nargs="*", default=None,
                    metavar="句", help="指定验证句；给空列表则用默认中→日三句")
    args = ap.parse_args()

    if not args.skip_export:
        export_fp32(args.src, args.out)
    fp32_bytes = sum(f.stat().st_size for f in args.out.glob("*.onnx") if "int8" not in f.name)
    quantize(args.out)
    assert_quantized(args.out)
    copy_sidecar(args.src, args.out)

    if args.verify is not None:
        sentences = args.verify or ["你好。", "请问洗手间在哪里？", "请在明天早上八点叫我起床。"]
        verify(args.out, sentences, args.threads)

    int8_bytes = sum(f.stat().st_size for f in args.out.glob("*_int8.onnx"))
    print(f"\n完成：int8 {int8_bytes / 1e6:.1f} MB"
          f"（fp32 为 {fp32_bytes / 1e6:.1f} MB，压缩比 {fp32_bytes / max(int8_bytes, 1):.2f}x）")

    if not args.keep_fp32:
        freed = 0
        for f in args.out.glob("*.onnx"):
            if "int8" not in f.name:
                freed += f.stat().st_size
                f.unlink()
        print(f"已删除 fp32 中间产物，释放 {freed / 1e9:.2f} GB（--keep-fp32 可保留）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
