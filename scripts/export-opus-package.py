#!/usr/bin/env python3
"""export-opus-package.py —— 把 OPUS-MT(Marian) 的 ONNX + spm 转成端侧可直接加载的目录。

为什么需要这一步：端侧不能用 sentencepiece 的 C++ 库（iOS/Android 都没有可靠的
预编译产物，自己交叉编译要额外拉一份 protobuf）。本项目改用 sherpa-onnx 同款的
`simple-sentencepiece`（Apache-2.0，126 KB 静态库，纯查表 + DP，无 protobuf 依赖）。

它带来两个必须在本脚本里补齐的差距：

1. **只吃文本词表**。`ssentencepiece` 的输入是「每行 `<piece>\t<score>`，行号即 id」，
   不是 `.spm` 的 protobuf。所以这里把 `.spm` 拆成 `source.vocab.txt` /
   `target.vocab.txt`。
2. **不做 NMT-NFKC 规范化**（`.spm` 里那 237 KB 的 `precompiled_charsmap` 它不读）。
   后果是中文的全角标点在端侧会整片变成 `<unk>`。实测（2026-09-28，21 条边界用例
   含全角标点 / 弯引号 / 罗马数字 / 半角片假名）：
       `unicodedata.normalize("NFKC", s)` **完全复现**了 sentencepiece 的输出，
       21/21 条 id 逐个相同。
   所以这里把 NFKC 的**逐码点差分**编译成 `norm/nfkc.bin`，由 C++ 侧在分词前套一层。

另外把**两族模型的 id 空间差异**在构建期就抹平：端侧只看到两张定长映射表
（`encode.i32` / `decode.i32`），不再需要知道自己是 base 还是 tc-big。

    base 族（zh-en / en-zh / ja-en）
        `vocab.json` 是 source+target 两份 spm 的**合并去重表**（65001 条），
        与任一 spm 的 id 都对不上 → encode 查 vocab.json，decode 再翻回 target.spm 原生 id
    tc-big 族（zh-ja）
        `vocab.json` **就是 target.spm 本身**（32001 条，100% 一致），源文 piece 有 3/4
        根本不在里面 → 两张表都是恒等映射
    判据：`len(vocab.json) >= 1.15 × max(两份 spm)` → 合并表，否则原生 id。

用法：
    python3 scripts/export-opus-package.py --out .tools/opus-package
    python3 scripts/export-opus-package.py --out .tools/opus-package --routes zh-en,en-zh

默认导出端侧**实际会用到**的四条语向（见 DEFAULT_ROUTES）：
zh-en / en-zh / zh-ja / ja-zh。
导出完用 `scripts/pack-opus-models.py` 打成 .vtmodel。
"""

import argparse
import json
import shutil
import struct
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 端侧路由表：语向 -> 需要哪几个单包（多于一跳 = pivot，英文中转）
# 与 scripts/check-nmt-quality.py 的 OPUS_ROUTES 必须一致。
ROUTES = {
    "zh-en": ["zh-en"],
    "en-zh": ["en-zh"],
    "zh-ja": ["zh-ja"],
    "ja-zh": ["ja-en", "en-zh"],
}

# 端侧**默认导出**哪几条。
#
# ja→zh 必须走 ja-en → en-zh 两跳 pivot：官方 ja-* 里没有任何以 zh 为目标的模型
# （zh-* 里中日向只有单向的 tc-big-zh-ja），所以只能日→英→中两跳解码，误差会叠加。
# 2026-09-28 的门禁实测（20 条）它是四向里唯一明显落后的一向：关键错误率 75% vs
# Qwen 50%；但 P50 延迟只有 69 ms（Qwen 1405 ms，快约 20×）。
#
# 2026-10-09 决定**纳入默认集合**，由 TranslationBackend.hybrid 把 ja→zh 交给 OPUS，
# 用速度换质量（代价：多 139 MB 的 routes/ja-en，以及上面那条质量差距）。
# 要退回旧行为（不打 ja-zh）：--routes zh-en,en-zh,zh-ja
DEFAULT_ROUTES = ["zh-en", "en-zh", "zh-ja", "ja-zh"]

# 端侧每个单包只放这三样：两张 ONNX、两份词表、两张映射表、一份精简配置
MODEL_FILES = ("encoder_model_int8.onnx", "decoder_model_merged_int8.onnx")


def load_spm(path: Path):
    """读 .spm，返回 (pieces, scores)。行号即 id —— 这正是 ssentencepiece 的约定。"""
    from sentencepiece import sentencepiece_model_pb2 as pb

    proto = pb.ModelProto()
    proto.ParseFromString(path.read_bytes())
    pieces = [p.piece for p in proto.pieces]
    scores = [p.score for p in proto.pieces]
    return pieces, scores


def write_vocab(path: Path, pieces, scores) -> None:
    """ssentencepiece 的词表：每行 `piece\\t score`，行号 = id。"""
    with path.open("w", encoding="utf-8") as fh:
        for piece, score in zip(pieces, scores):
            fh.write(f"{piece}\t{score}\n")


def build_nfkc(path: Path) -> int:
    """把 NMT-NFKC 的逐码点差分编译成二进制表。

    只收「单码点 NFKC 结果与自身不同」的条目 —— 组合序列（如 e + U+0301）不在此列，
    因为 ASR 输出本来就是 NFC 文本，见文件头的实测数据。

    格式：
        magic 'VTNF' | u32 version | u32 count
        count × { u8 src_len, u8 dst_len, src bytes, dst bytes }
    C++ 侧加载后按首字节分桶，匹配时按码点推进，未命中就原样复制。
    """
    entries = []
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:  # 代理区不是合法码点
            continue
        char = chr(cp)
        norm = unicodedata.normalize("NFKC", char)
        if norm == char:
            continue
        src, dst = char.encode("utf-8"), norm.encode("utf-8")
        if len(src) > 255 or len(dst) > 255:
            continue
        entries.append((src, dst))

    # 长的在前：C++ 侧同一起点要按最长匹配优先（虽然当前都是单码点，别写死这个假设）
    entries.sort(key=lambda e: (-len(e[0]), e[0]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(b"VTNF")
        fh.write(struct.pack("<II", 1, len(entries)))
        for src, dst in entries:
            fh.write(struct.pack("<BB", len(src), len(dst)))
            fh.write(src)
            fh.write(dst)
    return len(entries)


def build_route(name: str, src_dir: Path, out_dir: Path) -> dict:
    """把一个单包转成端侧目录，返回写进 manifest 需要的摘要。"""
    for required in (*MODEL_FILES, "config.json", "source.spm", "target.spm", "vocab.json"):
        if not (src_dir / required).is_file():
            raise FileNotFoundError(
                f"{name}: 缺 {required}（{src_dir}）。先跑 scripts/export-marian-onnx.py 与该包的 fetch。")

    cfg = json.loads((src_dir / "config.json").read_text(encoding="utf-8"))
    src_pieces, src_scores = load_spm(src_dir / "source.spm")
    tgt_pieces, tgt_scores = load_spm(src_dir / "target.spm")
    vocab = {k: int(v) for k, v in
             json.loads((src_dir / "vocab.json").read_text(encoding="utf-8")).items()}

    # —— 两族判定：合并去重表 vs 原生 id 空间（详见文件头）
    merged = len(vocab) >= 1.15 * max(len(src_pieces), len(tgt_pieces))

    out_dir.mkdir(parents=True, exist_ok=True)
    # 旧版本产出的文件名（decode 改成 piece 文本表后不再生成），顺手清掉，
    # 否则打包时会把废弃文件一起塞进 .vtmodel。
    for stale in ("decode.i32", "target.vocab.txt"):
        (out_dir / stale).unlink(missing_ok=True)
    for filename in MODEL_FILES:
        src_file, dst_file = src_dir / filename, out_dir / filename
        # 718 MB 的 ONNX 每次重导都抄一遍太贵；大小一致就认为还是那份。
        if not (dst_file.is_file() and dst_file.stat().st_size == src_file.stat().st_size):
            dst_file.write_bytes(src_file.read_bytes())
    write_vocab(out_dir / "source.vocab.txt", src_pieces, src_scores)

    unk_in_vocab = vocab.get("<unk>", 1)
    inv = {v: k for k, v in vocab.items()}

    if merged:
        # encode：源 spm 的 piece -> 合并表的 id
        encode_map = [vocab.get(piece, unk_in_vocab) for piece in src_pieces]
        # decode：模型 id -> **piece 文本**（不是 target spm 的原生 id！）
        #
        # 为什么不映射到 target 的 id 空间：模型完全可能输出一个只存在于**源**侧
        # 词表的 piece，那个 id 在 target.spm 里没有对应项，映射只能退回 <unk>，
        # 译文里就会凭空冒出字面量 "<unk>"（实测：Chen Xiaoming -> Chen <unk>iaoming）。
        # Python 参考实现是把 piece 文本直接交给 sentencepiece 的 decode，这里照做。
        decode_pieces = [inv.get(i, "") for i in range(len(vocab))]
    else:
        encode_map = list(range(len(src_pieces)))
        decode_pieces = tgt_pieces

    (out_dir / "encode.i32").write_bytes(struct.pack(f"<{len(encode_map)}i", *encode_map))
    with (out_dir / "decode.pieces.txt").open("w", encoding="utf-8") as fh:
        for piece in decode_pieces:
            fh.write(piece + "\n")

    # 端侧精简配置：只留推理必需项。eos/start/pad/bad_words **一律从模型配置读**，
    # 不许写死 —— base 与 tc-big 是两套完全不同的值（eos 0 vs 2），
    # 任何一个硬编码都会静默译错。
    bad_words = sorted({int(x) for group in (cfg.get("bad_words_ids") or []) for x in group})
    head_dim = int(cfg["d_model"]) // int(cfg["decoder_attention_heads"])
    slim = {
        "idMode": "merged-vocab" if merged else "native",
        "eosId": int(cfg["eos_token_id"]),
        "decoderStartId": int(cfg["decoder_start_token_id"]),
        "padId": int(cfg["pad_token_id"]),
        "badWordIds": bad_words,
        "decoderLayers": int(cfg["decoder_layers"]),
        "decoderHeads": int(cfg["decoder_attention_heads"]),
        "headDim": head_dim,
        "modelVocabSize": len(vocab) if merged else len(tgt_pieces),
    }
    (out_dir / "route.json").write_text(
        json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "idMode": slim["idMode"],
        "srcPieces": len(src_pieces),
        "modelVocab": slim["modelVocabSize"],
        "eos": slim["eosId"],
        "start": slim["decoderStartId"],
        "dModel": int(cfg["d_model"]),
        "bytes": sum((out_dir / f).stat().st_size
                     for f in (*MODEL_FILES, "encode.i32", "decode.pieces.txt",
                               "source.vocab.txt")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="导出端侧 OPUS-MT 包目录")
    parser.add_argument("--src", default=str(ROOT / ".tools" / "opus"),
                        help="各单包源目录的父目录（默认 .tools/opus）")
    parser.add_argument("--out", default=str(ROOT / ".tools" / "opus-package"),
                        help="输出的端侧包目录")
    parser.add_argument("--routes", default="",
                        help="逗号分隔的语向，默认全部；也接受单包名（如 zh-en,en-zh）")
    args = parser.parse_args()

    src_root, out_root = Path(args.src), Path(args.out)
    needed: list[str] = []
    selected: dict[str, list[str]] = {}
    wanted = [x.strip() for x in args.routes.split(",") if x.strip()]
    if wanted:
        for key in wanted:
            hops = ROUTES.get(key) or ([key] if (src_root / key).is_dir() else None)
            if not hops:
                raise SystemExit(f"未知语向 {key}；已知：{sorted(ROUTES)}")
            selected[key] = hops
    else:
        selected = {key: ROUTES[key] for key in DEFAULT_ROUTES}
    for hops in selected.values():
        for hop in hops:
            if hop not in needed:
                needed.append(hop)

    out_root.mkdir(parents=True, exist_ok=True)
    count = build_nfkc(out_root / "norm" / "nfkc.bin")
    print(f"NFKC 表：{count} 条差分 -> norm/nfkc.bin")

    # 清掉上一轮跑过、这次没接线的单包目录。
    # 留着不管会有两个后果：① 打包时可能被误当成本轮产物塞进 .vtmodel；
    # ② 目录里混着两个版本的文件，排查时说不清到底加载的是哪一份。
    routes_out = out_root / "routes"
    if routes_out.is_dir():
        for stale in sorted(p for p in routes_out.iterdir() if p.is_dir()):
            if stale.name not in needed:
                shutil.rmtree(stale)
                print(f"  清理        移除上次遗留的 routes/{stale.name}")

    summary = {}
    for hop in needed:
        info = build_route(hop, src_root / hop, out_root / "routes" / hop)
        summary[hop] = info
        print(f"  routes/{hop:<6} id={info['idMode']:<13} "
              f"src词表 {info['srcPieces']:>5}  模型词表 {info['modelVocab']:>5}  "
              f"eos={info['eos']:<2} start={info['start']:<6} {info['bytes'] / 1e6:7.1f} MB")

    # routes.txt 给 C++ 侧读：一行一个语向，制表符分隔「语向 + 跳数」。
    # 刻意不用 JSON —— 端侧为了零依赖不引 JSON 解析器，而这张表只有两层结构。
    with (out_root / "routes.txt").open("w", encoding="utf-8") as fh:
        for key, hops in selected.items():
            fh.write(f"{key}\t{','.join(hops)}\n")

    (out_root / "routes.json").write_text(
        json.dumps({"routes": selected, "packages": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    total = sum(v["bytes"] for v in summary.values())
    files = sum(1 for _ in out_root.rglob("*") if _.is_file())
    print(f"\n完成：{len(summary)} 个单包 / {len(selected)} 个语向，模型合计 {total / 1e6:.1f} MB，"
          f"目录共 {files} 个文件 -> {out_root}")
    print(f"语向接线：{json.dumps(selected, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
