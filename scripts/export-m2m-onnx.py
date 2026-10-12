#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把任意 M2M-100（含社区微调，如 Nekofox/M2M100-ja-zh）导出成端侧直接能用的 int8 ONNX。

## 与 `export-marian-onnx.py` 的区别
导出那一步完全一样（Optimum 的 `main_export`，任务名 `text2text-generation-with-past`），
差别只在**随包文件（SIDECAR）**：

| | Marian / OPUS-MT | M2M-100 |
|---|---|---|
| 词表 | `source.spm` + `target.spm`（两套） | `sentencepiece.bpe.model`（一套） |
| piece→id | `vocab.json`（base 是合并表） | **`tokenizer.json`**（权威） |
| 语言 token | 无（单语向模型） | `tokenizer.json` 的 `added_tokens` 里的 `__xx__` |

## 🔴 社区微调包常常**没有 `tokenizer.json`**
Nekofox/M2M100-ja-zh 的根目录只有 `vocab.json` + `sentencepiece.bpe.model` +
`special_tokens_map.json` + `tokenizer_config.json` —— **没有 `tokenizer.json`**。
而端侧 `M2M100Engine` 恰恰以 `tokenizer.json` 为 piece→id 与语言 token 的权威
（见 `check-nmt-quality.py` 的类 docstring 第 2 条）。
所以本脚本在导出后**会自己合成一份最小 `tokenizer.json`**（只含引擎真正读的两个字段）：
    `model.vocab`（来自 `vocab.json`）+ `added_tokens`（特殊 token + 语言 token）
合成完**必须自检**：`<unk>` 与目标语语言 token（如 `__zh__`）都能查到 id，
否则抛错 —— 少一个语言 token 的症状是「模型自己乱猜语言」（实测中译英时猜过 `__ja__`）。

## 用法
    # 先下原始权重（HF 通道走 aifasthub 镜像）
    python3 scripts/fetch-ms.py --source hf --mirror https://aifasthub.com \\
        Nekofox/M2M100-ja-zh pytorch_model.bin .tools/ja-zh/Nekofox-M2M100-ja-zh/pytorch_model.bin
    # 再转
    python3 scripts/export-m2m-onnx.py .tools/ja-zh/Nekofox-M2M100-ja-zh \\
        .tools/ja-zh/Nekofox-M2M100-ja-zh-onnx --langs ja,zh
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# 跟着原模型一起复制的文件（M2M100Engine 加载时按名查找）
SIDECAR = ("config.json", "generation_config.json", "tokenizer_config.json",
           "special_tokens_map.json", "vocab.json", "sentencepiece.bpe.model",
           "tokenizer.json")


def export_fp32(src: Path, out: Path) -> None:
    from optimum.exporters.onnx import main_export

    out.mkdir(parents=True, exist_ok=True)
    print(f"[1/4] fp32 导出：{src} → {out}")
    main_export(
        model_name_or_path=str(src),
        output=str(out),
        task="text2text-generation-with-past",
        opset=17,
        do_validation=False,
    )
    merged = out / "decoder_model_merged.onnx"
    if not merged.is_file():
        raise RuntimeError(
            f"没生成 {merged.name}；实际产物：{sorted(p.name for p in out.glob('*.onnx'))}")


def drop_redundant_decoders(out: Path) -> None:
    """删掉 merge 之后就没用了的 decoder 图。

    optimum 的 `post_process_exported_models` **只负责生成 `decoder_model_merged.onnx`，
    不删** 前两张（见 optimum/exporters/onnx/config.py 的实现 —— 合并后只把
    `onnx_files_subpaths` 指向 merged，原文件留在盘上）。端侧只用 merged，
    所以这三张图并存的那一瞬就是整个流程的磁盘峰值：
        encoder + decoder + decoder_with_past + merged ≈ 4 份权重。
    量化前先把前两张删掉，峰值立刻掉回 2 份。
    """
    merged = out / "decoder_model_merged.onnx"
    if not merged.is_file():
        return
    freed = 0
    for name in ("decoder_model.onnx", "decoder_with_past_model.onnx"):
        p = out / name
        if p.is_file():
            freed += p.stat().st_size
            p.unlink()
    if freed:
        print(f"      已删 merge 后无用的 decoder 图，释放 {freed / 1e9:.2f} GB")


def quantize(out: Path, min_ratio: float = 1.6) -> None:
    """int8 动态量化。

    ⚠️ `EnableSubgraph=True` 是**必须的**：merged decoder 的真身在一个 `If` 节点里
    （`use_cache_branch` 选首步/增量分支），权重挂在顶层 initializer、只被**子图内部**
    的节点引用。`quantize_dynamic` 是基础遍历器，不进 `If` 子图 —— 结果就是
    「脚本成功返回、产物一个字节没缩小」的静默失败。
    """
    from onnxruntime.quantization import QuantType, quantize_dynamic

    print("[2/4] int8 动态量化（encoder + merged decoder，含 If 子图）")
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
                "检查是否漏传 extra_options={'EnableSubgraph': True}")


def assert_quantized(out: Path) -> None:
    import onnx
    from onnx import TensorProto

    for name in ("encoder_model_int8.onnx", "decoder_model_merged_int8.onnx"):
        model = onnx.load(str(out / name), load_external_data=False)
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


def build_tokenizer_json(src: Path, out: Path, langs: list[str]) -> None:
    """合成最小 `tokenizer.json`：`model.vocab`（来自 vocab.json）+ `added_tokens`。

    端侧 `M2M100Engine` 只读这两个字段，所以不必还原完整的 HF tokenizer。

    🔴 **语言 token 的 id 从哪来**：社区包只给了 `vocab.json`（**不含** `__ja__` 这类语言
    token，实测 Nekofox 的 vocab.json 是基准词表**去掉 100 个语言 token** 的结果）
    与 `tokenizer_config.json` 的 `additional_special_tokens`（100 条，按字母序）。
    HF 加这些 token 的规则就是「**接在词表之后、按 `additional_special_tokens` 的顺序**」，
    所以这里照做：`next_id = max(vocab.values()) + 1` 起顺次分配。

    实测核对（2026-10-10，与 `.tools/m2m/tokenizer.json` 逐条比对）：
      • 规则复现出的 100 个语言 token id 与基准**零不一致**
        （`__en__`=128022、`__ja__`=128046、`__zh__`=128102）；
      • Nekofox `vocab.json` 与基准 `model.vocab` 交集 128004 条，**id 冲突 0**，
        且 Nekofox **没有**基准之外的新条目 ⇒ 同一个 id 空间；
      • 基准独有的 108 条 = 100 个语言 token + `<madeupword0..7>`（128104-128111）。
        那 8 条是「凑数词」，训练/推理都不会被选中，合成时**故意不收**：
        `_decode` 里 `_id2piece.get(id, "")` 会把它变成空串直接丢弃，比收进来更安全。
      • 两端 `config.vocab_size` 都是 **128112**，模型侧 embedding 行数一致。
    """
    v_p = src / "vocab.json"
    if not v_p.is_file():
        raise FileNotFoundError(f"缺 {v_p}（合成 tokenizer.json 需要它）")
    vocab = json.loads(v_p.read_text(encoding="utf-8"))

    specials: dict[str, int] = {}
    stm: dict = {}
    stm_p = src / "special_tokens_map.json"
    if stm_p.is_file():
        stm = json.loads(stm_p.read_text(encoding="utf-8"))
        for key in ("unk_token", "bos_token", "eos_token", "pad_token"):
            v = stm.get(key)
            if isinstance(v, dict):
                v = v.get("content")
            if isinstance(v, str) and v in vocab:
                specials[v] = vocab[v]

    # 语言 token 等「词表外」的特殊 token：按 tokenizer_config 的顺序顺次编号
    tc: dict = {}
    tc_p = src / "tokenizer_config.json"
    if tc_p.is_file():
        tc = json.loads(tc_p.read_text(encoding="utf-8"))
    extra = tc.get("additional_special_tokens") or stm.get("additional_special_tokens") or []
    next_id = max(vocab.values()) + 1
    for t in extra:
        t = t.get("content") if isinstance(t, dict) else t
        if isinstance(t, str) and t not in vocab and t not in specials:
            specials[t] = next_id
            next_id += 1
    # 兜底：万一 additional_special_tokens 是空的，至少补上需要用的语向
    for code in langs:
        tok = f"__{code}__"
        if tok not in vocab and tok not in specials:
            specials[tok] = next_id
            next_id += 1

    # 语言 token 必须能查到 id，否则 M2M100Engine 会取不到目标语言（症状：模型乱猜语言）
    missing = [f"__{c}__" for c in langs if f"__{c}__" not in vocab and f"__{c}__" not in specials]
    if missing:
        raise RuntimeError(f"找不到语言 token {missing} —— 这个包的语言码可能不是标准 M2M-100 码")
    if "<unk>" not in specials:
        raise RuntimeError("special_tokens_map.json 里没有可用的 <unk>，无法合成 tokenizer.json")

    tj = {"model": {"vocab": vocab},
          "added_tokens": [{"content": k, "id": int(v)} for k, v in specials.items()]}
    dst = out / "tokenizer.json"
    dst.write_text(json.dumps(tj, ensure_ascii=False), encoding="utf-8")
    lang_ids = ", ".join("__%s__=%s" % (c, specials.get("__%s__" % c)) for c in langs)
    print(f"      合成 tokenizer.json：vocab {len(vocab)} 条，added_tokens {len(specials)} 条；"
          f"语向 id：{lang_ids}")


def copy_sidecar(src: Path, out: Path) -> None:
    print("[4/4] 复制词表与配置")
    for name in SIDECAR:
        s = src / name
        if not s.is_file():
            print(f"      ⚠️ 缺 {name}（源目录没有）")
            continue
        d = out / name
        if s.resolve() == d.resolve():
            continue
        shutil.copy2(s, d)


def verify(out: Path, sentences: list[str], threads: int) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("cnq", root / "scripts" / "check-nmt-quality.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cnq"] = mod
    spec.loader.exec_module(mod)

    eng = mod.M2M100Engine(out, threads=threads)
    print(f"\n[验证] {eng.describe()}")
    for text in sentences:
        row = {"source_lang": "ja", "target_lang": "zh", "source": text}
        out_text = eng.translate(row)
        flag = "⚠️ 空/复读" if (not out_text or len(set(out_text.split())) == 1) else "✅"
        print(f"  {flag} {text!r}\n     -> {out_text!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="HF M2M-100（含社区微调）→ 端侧 int8 ONNX")
    ap.add_argument("src", type=Path, help="含 pytorch_model.bin 的 HF 模型目录")
    ap.add_argument("out", type=Path, help="输出目录")
    ap.add_argument("--skip-export", action="store_true", help="已有 fp32 ONNX，只做量化+复制")
    ap.add_argument("--keep-fp32", action="store_true", help="保留 fp32 中间产物")
    ap.add_argument("--drop-src-weights", action="store_true",
                    help="fp32 导出成功后**立刻删掉源 pytorch_model.bin**。"
                         "大模型（>1 GB）导出时磁盘峰值 = 源权重 + 4 份 ONNX（encoder + "
                         "decoder + with_past + merged），动辄 8 GB+；先在导出完删源，"
                         "峰值能压掉一个权重的体积。删之前 `export_fp32` 已断言过 merged 存在。")
    ap.add_argument("--langs", default="ja,zh", help="需要保证存在的语言 token（逗号分隔）")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--verify", nargs="*", default=None, metavar="句",
                    help="指定验证句；给空列表则用默认日→中三句")
    args = ap.parse_args()

    langs = [c.strip() for c in args.langs.split(",") if c.strip()]

    if not args.skip_export:
        export_fp32(args.src, args.out)
        drop_redundant_decoders(args.out)
        if args.drop_src_weights:
            for name in ("pytorch_model.bin", "model.safetensors"):
                w = args.src / name
                if w.is_file():
                    print(f"      删除源权重 {w.name}（{w.stat().st_size / 1e9:.2f} GB）")
                    w.unlink()
    fp32_bytes = sum(f.stat().st_size for f in args.out.glob("*.onnx") if "int8" not in f.name)
    quantize(args.out)
    assert_quantized(args.out)
    print("[3/4] 词表与配置")
    copy_sidecar(args.src, args.out)
    if not (args.out / "tokenizer.json").is_file():
        build_tokenizer_json(args.src, args.out, langs)

    if args.verify is not None:
        sentences = args.verify or ["こんにちは。", "すみません、トイレはどこですか？",
                                    "明日の朝八時に起こしてください。"]
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
