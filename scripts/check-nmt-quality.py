#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""专用 NMT（M2M-100-418M int8 / ONNX Runtime）对比现役 Qwen2.5-1.5B 的桌面门禁。

判据沿用仓库既有 gate（见 `evaluation/README.md`，实现在 `translation_poc.gate`）：
    关键语义错误率相对 Qwen 降低 >= 20%
    或 P95 稳态延迟降低 >= 35%
满足其一才算 promote。

## 为什么单独一个脚本
`translation_poc.py` 是 Windows 导向的（默认 `llama-server.exe`、OPUS 侧要 torch），
但它里面的 `chrf` / `critical_ok` / `percentile` / `gate` 就是本仓库对「翻译质量」的
既有定义。这里**直接 import 复用**，不另立一套度量 —— 新脚本只补两块：
  1. `M2M100Engine`：在 ONNX Runtime 上跑 Xenova/m2m100_418M 的 int8 导出；
  2. `QwenServerEngine`：本机 llama-server + **生产 prompt**（逐字复用
     `check-translation-quality.build_prompt`，即 translator.cpp 的 Qwen 分支）。

## 公平性设计（本脚本的重点）
- 两侧都测「模型常驻 + 预热完成后」的单句端到端延迟，取 p50 / p95。
- Qwen 侧用**生产 prompt**（system 约束 + 3 组 few-shot）—— 用户实际感受到的
  就是这条路径的速度与质量，拿它当基准才有意义。
- M2M 侧是 NMT，直接吃原文，没有 few-shot 概念；这正是它快的原因之一，不是作弊。
- 采样两端都是 greedy（Qwen: temp=0 / top-k=1；M2M: argmax）。
- 线程数两侧用同一个 `--threads`。

## ⚠️ 桌面读数不能直接搬到 iPad mini 4
Qwen 侧能用 `--arch armv8-a` 复现 A8 的基线指令集，但 **ORT 的 macOS wheel 降不下去**
（它由 Apple Silicon 的 dotprod / i8mm 路径编译，没有 armv8-a 变体）。所以：
    · 「同一台机器上 NMT vs Qwen 的相对倍率」可信；
    · 绝对 t/s 不可外推，端侧必须重测。

## 用法
    python3 scripts/check-nmt-quality.py --nmt-only          # 只验 NMT 跑得通、看译文
    python3 scripts/check-nmt-quality.py                     # 全套对比（会编 llama-server）
    python3 scripts/check-nmt-quality.py --dataset evaluation/zh_ja_ko_sample.jsonl --nmt-only
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import translation_poc as poc  # noqa: E402  （复用 chrF / critical_ok / percentile / gate）


def _load_dashed(name: str, path: Path):
    """连字符文件名的模块加载器（check-translation-quality.py 不能直接 import）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


CTQ = _load_dashed("ctq", ROOT / "scripts" / "check-translation-quality.py")
NMT_DIR = ROOT / ".tools" / "m2m"
WORK = ROOT / ".tools" / "nmt-check"
REPORT = ROOT / "dist" / "verification" / "nmt-poc.json"

# VT 语料里的语言码 → Qwen prompt 用的英文名。
# **不在这里另写一份** —— 唯一真源是 `check-translation-quality.NAME`
# （prompt 就是那边渲染的，两处各写一份必然漂移）。
NAME = CTQ.NAME
# M2M-100 的 100 语表覆盖（粤语不在其中）
M2M_LANGS = ("zh", "ja", "ko", "en", "fr", "de", "ru")
DEFAULT_DATASET = ROOT / "evaluation" / "zh_en_asr_sample.jsonl"


def arch_tag(arch: str) -> str:
    return arch.replace("+", "_")


# ---------------------------------------------------------------- 数据集
def load_cases(path: Path) -> list[dict]:
    """与 translation_poc.load_cases 同格式，但**放开方向限制**（不止 zh-en/en-zh）。

    返回值保留 `source_lang` / `target_lang` **语言码**（各引擎要按码选模型），
    所以不能直接换成 `check-translation-quality.load_dataset` —— 那边返回的是
    prompt 用的英文名。两边校验规则一致，改一边要同步另一边。
    """
    cases = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        row = json.loads(line)
        missing = {"id", "source_lang", "target_lang", "source", "reference"} - row.keys()
        if missing:
            raise ValueError(f"{path}:{line_no}: 缺少字段 {sorted(missing)}")
        for key in ("source_lang", "target_lang"):
            if row[key] not in NAME:
                raise ValueError(f"{path}:{line_no}: 未知语言码 {row[key]!r}")
        row.setdefault("critical_terms", [])
        cases.append(row)
    if not cases:
        raise ValueError(f"{path} 是空的")
    return cases


# ---------------------------------------------------------------- 引擎 A：M2M-100 int8 / ORT
class M2M100Engine:
    """Xenova/m2m100_418M 的 int8 ONNX 导出，跑在 ONNX Runtime 上。

    用 ORT 而不是 CTranslate2，是为了**与端侧同一套运行时** —— 这两条 ONNX 文件
    就是将来要塞进 App 的那两份，桌面跑通等于把端侧路线验证掉大半。

    ⚠️ 四个实测踩出来的坑（文档里都查不到，换模型重做时必须重验）：
    1. **`tokenizers` 库拒绝加载这份 tokenizer.json**：新版对 added_tokens 校验过严，
       报 `Token 'ଙ' out of vocabulary`（0.20.3 / 0.23.2 都拒）。改用 sentencepiece。
    2. **`vocab.json` 与 sentencepiece 的 id 空间不一致**：同一个 piece `▁最近` 在
       sp 里是 80591、在 vocab.json 里是 76049，语言 token 也不在 vocab.json 里。
       权威是 `tokenizer.json` 的 `model.vocab` + `added_tokens`（那份与 vocab.json 一致）。
    3. **merged decoder 的 past 形状**：encoder 侧必须给**真实源句长度**（cross-attention
       的 KV 长度恒等于源句长），只有 decoder 侧给 0；encoder 给零长会让 ONNX 的
       `Reshape` 把 0 当成「保持该维」，崩在 `reshape_helper.h:35`。
    4. **必须显式强制目标语言**（HF 的 `forced_bos_token_id`）：decoder 第一步的输入是
       `decoder_start_token_id`，其输出要被改写成目标语言 token，否则模型自己乱猜
       —— 实测中译英时它猜了 `__ja__`。
    """

    name = "m2m100-418m-int8(ort)"

    def __init__(self, model_dir: Path = NMT_DIR, threads: int = 4, max_new_tokens: int = 128):
        import numpy as np
        import onnxruntime as ort
        import sentencepiece as spm

        self._np = np
        enc_p = model_dir / "encoder_model_int8.onnx"
        dec_p = model_dir / "decoder_model_merged_int8.onnx"
        sp_p = model_dir / "sentencepiece.bpe.model"
        tj_p = model_dir / "tokenizer.json"
        cfg_p = model_dir / "config.json"
        for p in (enc_p, dec_p, sp_p, tj_p, cfg_p):
            if not p.is_file():
                raise FileNotFoundError(
                    f"缺 {p}；用 scripts/fetch-ms.py Xenova/m2m100_418M 从 ModelScope 拉")

        cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
        self.d_model = int(cfg["d_model"])
        self.heads = int(cfg["decoder_attention_heads"])
        self.head_dim = self.d_model // self.heads
        self.eos_id = int(cfg["eos_token_id"])
        self.start_id = int(cfg["decoder_start_token_id"])
        self.max_new_tokens = max_new_tokens

        # tokenizer：sentencepiece 负责**分词**，tokenizer.json 负责 piece→id。
        # 不能反过来用 sp 的 id —— 那是另一套空间（见类 docstring 第 2 条）。
        self.sp = spm.SentencePieceProcessor(model_file=str(sp_p))
        tj = json.loads(tj_p.read_text(encoding="utf-8"))
        self._piece2id: dict[str, int] = tj["model"]["vocab"]
        self._id2piece = {v: k for k, v in self._piece2id.items()}
        self._special = {t["content"]: int(t["id"]) for t in tj["added_tokens"]}
        self._unk_id = self._special["<unk>"]
        self._special_ids = set(self._special.values())

        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.log_severity_level = 3

        t0 = time.perf_counter()
        self.enc = ort.InferenceSession(str(enc_p), so, providers=["CPUExecutionProvider"])
        self.dec = ort.InferenceSession(str(dec_p), so, providers=["CPUExecutionProvider"])
        self.load_ms = (time.perf_counter() - t0) * 1000.0
        self.package_bytes = enc_p.stat().st_size + dec_p.stat().st_size

        self._out_names = [o.name for o in self.dec.get_outputs()]
        self._logits_idx = next(
            (i for i, n in enumerate(self._out_names) if n == "logits"), 0)
        self._past_in = [i.name for i in self.dec.get_inputs()
                         if i.name.startswith("past_key_values")]
        self._has_branch = any(i.name == "use_cache_branch" for i in self.dec.get_inputs())
        # 槽位映射 past_key_values.X.Y <-> present.X.Y（两侧顺序一致，但按名取更稳）
        name2out = {n: i for i, n in enumerate(self._out_names)}
        self._dec_slots = [(p, name2out[p.replace("past_key_values", "present")])
                           for p in self._past_in if ".decoder." in p]
        self._enc_slots = [(p, name2out[p.replace("past_key_values", "present")])
                           for p in self._past_in if ".encoder." in p]

        self._lang_ids: dict[str, int] = {}
        for code in M2M_LANGS:
            tid = self._special.get(f"__{code}__")
            if tid is not None:
                self._lang_ids[code] = tid

    def describe(self) -> str:
        return (f"{len(self._lang_ids)} 个语言 token | "
                f"decoder 输入 {len(self._past_in)} 个 past 槽 | use_cache_branch={self._has_branch}")

    def translate(self, row: dict) -> str:
        np = self._np
        src, dst = row["source_lang"], row["target_lang"]
        for side, code in (("源", src), ("目标", dst)):
            if code not in self._lang_ids:
                raise ValueError(
                    f"M2M-100 不支持{side}语言 {code!r}（100 语表无此码；粤语由 Qwen 兜底）")

        tgt_id = self._lang_ids[dst]
        ids = [self._lang_ids[src]]
        ids += [self._piece2id.get(p, self._unk_id)
                for p in self.sp.encode(row["source"], out_type=str)]
        ids += [self.eos_id]
        ids_arr = np.array([ids], dtype=np.int64)
        mask = np.ones_like(ids_arr)
        src_len = int(ids_arr.shape[1])

        hidden = self.enc.run(None, {"input_ids": ids_arr, "attention_mask": mask})[0]

        # 第一轮：decoder 侧 past 长度 0，**encoder 侧必须给真实源句长**（见类 docstring 第 3 条）。
        # ⚠️ 每个槽必须是**独立**的数组：24 个输入共用同一个 ndarray 会让 ORT 的
        #    If 分支在此处崩掉（reshape_helper.h:35）—— 实测踩过。
        cache = {
            n: np.zeros((1, self.heads, 0 if ".decoder." in n else src_len, self.head_dim),
                        np.float32)
            for n in self._past_in
        }

        tok_id = self.start_id
        out_ids: list[int] = []
        for step in range(self.max_new_tokens):
            feed = {"input_ids": np.array([[tok_id]], dtype=np.int64),
                    "encoder_hidden_states": hidden,
                    "encoder_attention_mask": mask}
            feed.update(cache)
            if self._has_branch:
                feed["use_cache_branch"] = np.array([step > 0])
            outs = self.dec.run(None, feed)
            # 第一步强制目标语言（HF 的 forced_bos_token_id）——见类 docstring 第 4 条
            nxt = tgt_id if step == 0 else int(np.argmax(outs[self._logits_idx][0, -1]))
            if nxt == self.eos_id:
                break
            out_ids.append(nxt)
            # ⚠️ 只更新 decoder 槽；**encoder 槽只在第 0 步更新一次**（见类 docstring 第 5 条）
            for name, idx in self._dec_slots:
                cache[name] = outs[idx]
            if step == 0:
                for name, idx in self._enc_slots:
                    cache[name] = outs[idx]
            tok_id = nxt
        return self._decode(out_ids)

    def _decode(self, ids: list[int]) -> str:
        """id → 文本：跳过所有特殊 token（含 `__zh__` 这类语言 token），`▁` 还原为空格。"""
        text = "".join(self._id2piece.get(i, "") for i in ids if i not in self._special_ids)
        return text.replace("\u2581", " ").strip()

    def rss(self):
        return poc.process_rss_bytes(__import__("os").getpid())

    def close(self) -> None:
        self.enc = self.dec = None


# ---------------------------------------------------------------- 引擎 A2：OPUS-MT / Marian int8
OPUS_DIR = ROOT / ".tools" / "opus"

# 本轮验证范围的接线：语向 -> 跳数（单元素=直译，两元素=pivot 两跳）
#   zh↔en        官方 base 包
#   zh→ja        官方只有 tc-big 版
#   ja→zh        官方**完全没有**，只能 pivot（ja→en→zh）
#
# ⚠️ 四个包**全部用 scripts/export-marian-onnx.py 自转**，不要用 Xenova 的预量化 ONNX：
#    实测 Xenova 的 base `_int8` 让 `lm_head` 走 fp32 MatMul、且每步反量化一遍绑定词嵌入
#    （ORT profiler：68.5 + 32.2 ms / 5 步），自转后快 7.6~8.4 倍，代价只有包体 +16%。
#    判据：跑 ORT profiler 看是 `MatMul` 还是 `MatMulInteger`。
OPUS_ROUTES = {
    "zh-en": ("zh-en",),
    "en-zh": ("en-zh",),
    "ja-en": ("ja-en",),
    # ja→zh 官方**完全没有** → pivot 两跳（ja→en→zh），两个包都是现成 ONNX
    "ja-zh": ("ja-en", "en-zh"),
    # zh→ja 官方只有 tc-big（209M，无现成 ONNX）→ 用 scripts/export-marian-onnx.py 自转
    # ⚠️ 这一包必须走 native id 模式（见 MarianEngine 里的两族说明）
    "zh-ja": ("zh-ja",),
}


class MarianEngine:
    """单个语向的 OPUS-MT（Marian）int8 ONNX，跑在 ONNX Runtime 上。

    与 `M2M100Engine` 同属 Optimum 导出的 merged decoder，但**参数一个都不能照抄**：

    | 项 | M2M-100 | OPUS-MT base | OPUS-MT tc-big |
    |---|---|---|---|
    | `eos_token_id` | 2 | **0** | **2** |
    | `decoder_start_token_id` | 2 | **65000** | **32000** |
    | `pad_token_id` | 1 | 65000 | 32000 |
    | 目标语言控制 | **必须**手动强制 `__xx__` | 无（用 decoder_start） | 无 |
    | past 槽数 | 48（12 层 × 4） | **24**（6 层 × 4） | 24 |
    | 分词 | 单份 spm + tokenizer.json | **两份 spm**（source/spm 编码、target.spm 还原） | 同左 |

    ⚠️ **`eos` / `decoder_start` / `pad` / `bad_words` 全部从 `config.json` 读，不许写死** ——
    tc-big-zh-ja 与 base 就是两套值，任何一个硬编码都会静默译错。

    沿用 `M2M100Engine` 实测出来的三条 merged-decoder 铁律（见该类 docstring）：
      ① 每个 past 槽必须是**独立** ndarray（共用会让 ORT 的 If 分支崩）；
      ② encoder 侧 past 给**真实源句长**、decoder 侧给 0；
      ③ **encoder 槽只在第 0 步更新一次** —— 增量分支返回的 `present.*.encoder.*` 是空张量，
         每步都覆盖会把真实 cross-attention KV 换成垃圾（症状：前 2 个 token 正常，第 3 个起崩）。
    """

    def __init__(self, model_dir: Path, threads: int = 4, max_new_tokens: int = 128,
                 variant: str = "_int8", id_mode: str = "auto"):
        import numpy as np
        import onnxruntime as ort
        import sentencepiece as spm

        self._np = np
        self.dir = Path(model_dir)
        self.variant = variant
        self.name = f"opus-mt{self.dir.name}{variant}(ort)" if self.dir.name not in (
            "",) else "opus-mt(ort)"
        enc_p = self.dir / f"encoder_model{variant}.onnx"
        dec_p = self.dir / f"decoder_model_merged{variant}.onnx"
        cfg_p = self.dir / "config.json"
        sp_p = self.dir / "source.spm"
        tsp_p = self.dir / "target.spm"
        v_p = self.dir / "vocab.json"
        for p in (enc_p, dec_p, cfg_p, sp_p, tsp_p, v_p):
            if not p.is_file():
                raise FileNotFoundError(
                    f"缺 {p}；用 scripts/fetch-ms.py <repo> <远程路径> {p} 拉（见 OPUS_ROUTES）")

        cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
        self.d_model = int(cfg["d_model"])
        self.heads = int(cfg["decoder_attention_heads"])
        self.head_dim = self.d_model // self.heads
        self.layers = int(cfg["decoder_layers"])
        # —— 这三行是「不能照抄 M2M」的全部要害
        self.eos_id = int(cfg["eos_token_id"])
        self.start_id = int(cfg["decoder_start_token_id"])
        self.pad_id = int(cfg["pad_token_id"])
        self.bad_ids = {int(x) for grp in (cfg.get("bad_words_ids") or []) for x in grp}
        self.max_new_tokens = max_new_tokens

        # 分词：source.spm 负责把原文切成 piece，vocab.json 负责 piece→id。
        # **不能直接用 sp 的 id 空间** —— vocab.json 是 source+target 两套 spm 的
        # 合并去重表（65001 条 vs 单份 spm 的 32000），两者 id 完全对不上。
        self.src_sp = spm.SentencePieceProcessor(model_file=str(sp_p))
        self.tgt_sp = spm.SentencePieceProcessor(model_file=str(tsp_p))
        self.vocab: dict[str, int] = {k: int(v) for k, v in
                                      json.loads(v_p.read_text(encoding="utf-8")).items()}
        self.inv = {v: k for k, v in self.vocab.items()}
        self.unk_id = self.vocab.get("<unk>", 1)

        # —— 🔴 两族模型的 id 空间规则**相反**，必须按包判定（实测 2026-09-28）
        #    base 族（zh-en/en-zh/ja-en）: vocab.json 是 source+target 的**合并去重表**
        #       （65001 ≈ 2×32000），和任一 spm 的 id 都对不上 → 必须**查 vocab.json**
        #    tc-big 族（zh-ja）: vocab.json **就是 target.spm 本身**（32001，100% 一致），
        #       且源文 piece 有 3/4 根本不在里面 → 只能**用各自 spm 的原生 id**
        #    判据：vocab.json 明显大于单个 spm 的词表 → 合并表；否则 → 原生 id。
        #    ⚠️ 选错的表现是输出整句 `<unk>`（解码成 `⁇`）或胡言乱语，且**量化档位无关**
        #       （fp32 与 int8 输出逐字相同）—— 遇到这种先查 id 空间，别怪量化。
        if id_mode == "auto":
            merged = len(self.vocab) >= 1.15 * max(self.src_sp.get_piece_size(),
                                                  self.tgt_sp.get_piece_size())
            self.id_mode = "vocab" if merged else "native"
        else:
            self.id_mode = id_mode

        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.log_severity_level = 3

        t0 = time.perf_counter()
        self.enc = ort.InferenceSession(str(enc_p), so, providers=["CPUExecutionProvider"])
        self.dec = ort.InferenceSession(str(dec_p), so, providers=["CPUExecutionProvider"])
        self.load_ms = (time.perf_counter() - t0) * 1000.0
        self.package_bytes = enc_p.stat().st_size + dec_p.stat().st_size

        self._out_names = [o.name for o in self.dec.get_outputs()]
        self._logits_idx = next((i for i, n in enumerate(self._out_names) if n == "logits"), 0)
        self._past_in = [i.name for i in self.dec.get_inputs()
                         if i.name.startswith("past_key_values")]
        self._has_branch = any(i.name == "use_cache_branch" for i in self.dec.get_inputs())
        name2out = {n: i for i, n in enumerate(self._out_names)}
        self._dec_slots = [(p, name2out[p.replace("past_key_values", "present")])
                           for p in self._past_in if ".decoder." in p]
        self._enc_slots = [(p, name2out[p.replace("past_key_values", "present")])
                           for p in self._past_in if ".encoder." in p]

    def describe(self) -> str:
        return (f"eos={self.eos_id} start={self.start_id} pad={self.pad_id} | "
                f"{self.layers} 层 | id={self.id_mode} | past {len(self._past_in)} 槽"
                f"（decoder {len(self._dec_slots)} / encoder {len(self._enc_slots)}）| "
                f"use_cache_branch={self._has_branch}")

    def encode(self, text: str) -> "list[int]":
        if self.id_mode == "native":
            return self.src_sp.encode(text) + [self.eos_id]
        pieces = self.src_sp.encode(text, out_type=str)
        return [self.vocab.get(p, self.unk_id) for p in pieces] + [self.eos_id]

    def decode(self, ids: "list[int]") -> str:
        if self.id_mode == "native":
            return self.tgt_sp.decode(ids).strip()
        pieces = [self.inv[i] for i in ids if i in self.inv]
        return self.tgt_sp.decode(pieces).strip()

    def translate_text(self, text: str) -> str:
        np = self._np
        ids = self.encode(text)
        ids_arr = np.array([ids], dtype=np.int64)
        mask = np.ones_like(ids_arr)
        src_len = int(ids_arr.shape[1])
        hidden = self.enc.run(None, {"input_ids": ids_arr, "attention_mask": mask})[0]

        cache = {
            n: np.zeros((1, self.heads, 0 if ".decoder." in n else src_len, self.head_dim),
                        np.float32)
            for n in self._past_in
        }
        tok = self.start_id
        out_ids: list[int] = []
        for step in range(self.max_new_tokens):
            feed = {"input_ids": np.array([[tok]], dtype=np.int64),
                    "encoder_hidden_states": hidden, "encoder_attention_mask": mask}
            feed.update(cache)
            if self._has_branch:
                feed["use_cache_branch"] = np.array([step > 0])
            outs = self.dec.run(None, feed)
            # Marian **没有 forced_bos**：第一步的 decoder_start 已经带方向，直接 argmax
            logits = outs[self._logits_idx][0, -1]
            for bid in self.bad_ids:
                logits[bid] = -np.inf
            nxt = int(np.argmax(logits))
            if nxt == self.eos_id:
                break
            out_ids.append(nxt)
            for name, idx in self._dec_slots:
                cache[name] = outs[idx]
            if step == 0:
                for name, idx in self._enc_slots:
                    cache[name] = outs[idx]
            tok = nxt
        return self.decode(out_ids)

    def rss(self):
        return poc.process_rss_bytes(__import__("os").getpid())

    def close(self) -> None:
        self.enc = self.dec = None


class OpusBundle:
    """按语向分派到具体的 Marian 单包；官方缺的向走 pivot（两跳）。"""

    def __init__(self, root: Path = OPUS_DIR, threads: int = 4,
                 routes: dict | None = None, max_new_tokens: int = 128,
                 variant: str = "_int8"):
        self.name = f"opus-mt{variant}(ort)"
        self.routes = routes or OPUS_ROUTES
        self.variant = variant
        self.pivoted = sorted(d for d, hops in self.routes.items() if len(hops) > 1)
        self.models: dict[str, MarianEngine] = {}
        self.incomplete: list[str] = []
        for hops in self.routes.values():
            for hop in hops:
                if hop in self.models:
                    continue
                d = Path(root) / hop
                # 两个文件都必须齐 —— 只查 encoder 会在 decoder 还挂着 .part 时就往下走，
                # 然后崩在 MarianEngine 构造函数里（实测踩过）
                if ((d / f"encoder_model{variant}.onnx").is_file()
                        and (d / f"decoder_model_merged{variant}.onnx").is_file()):
                    self.models[hop] = MarianEngine(d, threads=threads,
                                                    max_new_tokens=max_new_tokens,
                                                    variant=variant)
                else:
                    self.incomplete.append(hop)
        missing = sorted({h for hops in self.routes.values() for h in hops
                          if h not in self.models})
        self.missing = missing
        self.load_ms = sum(m.load_ms for m in self.models.values())
        self.package_bytes = sum(m.package_bytes for m in self.models.values())

    def describe(self) -> str:
        lines = [f"已装载 {len(self.models)} 个包：" +
                 "、".join(f"{k}（{m.describe()}）" for k, m in sorted(self.models.items()))]
        if self.missing:
            lines.append(f"⚠️ 缺包 {self.missing}（对应语向会跳过）")
        if self.pivoted:
            lines.append(f"⚠️ 走 pivot 两跳的语向：{self.pivoted}")
        return "\n     ".join(lines)

    def translate(self, row: dict) -> str:
        key = f'{row["source_lang"]}-{row["target_lang"]}'
        hops = self.routes.get(key)
        if not hops:
            raise ValueError(f"OPUS 接线里没有 {key}（现有：{sorted(self.routes)}）")
        for hop in hops:
            if hop not in self.models:
                raise FileNotFoundError(f"{key} 需要 {hop} 包，但它没下全")
        text = row["source"]
        for hop in hops:
            text = self.models[hop].translate_text(text)
        return text

    def rss(self):
        return poc.process_rss_bytes(__import__("os").getpid())

    def close(self) -> None:
        for m in self.models.values():
            m.close()


# ---------------------------------------------------------------- 引擎 B：Qwen / llama-server
class QwenServerEngine:
    """本机 llama-server + 生产 prompt，测的是用户真正会遭遇的那条路径。"""

    name = "qwen25-1.5b-q4km(prod-prompt)"

    def __init__(self, server: Path, model: Path, port: int, threads: int, arch: str = ""):
        self.arch = arch
        t0 = time.perf_counter()
        self.url = f"http://127.0.0.1:{port}"
        self.proc = subprocess.Popen(
            [str(server), "-m", str(model), "--host", "127.0.0.1", "--port", str(port),
             "-c", "2048", "-t", str(threads), "-np", "1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL)
        for _ in range(180):
            if self.proc.poll() is not None:
                raise RuntimeError("llama-server 启动过程中退出")
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(1)
        else:
            raise TimeoutError("llama-server 180s 内未就绪")
        self.load_ms = (time.perf_counter() - t0) * 1000.0
        self.package_bytes = model.stat().st_size
        self._arch = CTQ.gguf_arch(model)
        # 连续翻译时 prompt 前缀（system + few-shot）逐字相同 —— 置 True 让
        # llama-server 复用这段前缀的 KV，省掉每次重复 prefill。端侧等价做法是
        # 原生层手动 llama_kv_cache_seq_rm 回滚到前缀末尾（见报告"提速"一节）。
        self.cache_prompt = False

    def translate(self, row: dict) -> str:
        prompt = CTQ.build_prompt(
            NAME[row["source_lang"]], NAME[row["target_lang"]], row["source"], self._arch)
        payload = {"prompt": prompt, "temperature": 0, "top_k": 1, "n_predict": 256,
                   "cache_prompt": self.cache_prompt, "stop": ["<|im_end|>", "<|im_start|>"]}
        req = urllib.request.Request(
            self.url + "/completion", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        text = result.get("content")
        if text is None:  # 旧版 llama-server 的返回格式
            text = (result.get("choices") or [{}])[0].get("text", "")
        return re.sub(r"</?think>|\[end of text\]", "", text or "").strip()

    def rss(self):
        return poc.process_rss_bytes(self.proc.pid)

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ---------------------------------------------------------------- 评测
def evaluate(engine, cases: list[dict], threshold: float, warmup: int = 1) -> dict:
    """warmup 轮不计入延迟 —— 首句的 IO/页缓存效应不是稳态。"""
    for case in cases[:warmup]:
        engine.translate(case)

    rows, latencies, peak_rss = [], [], 0
    for case in cases:
        t0 = time.perf_counter()
        out = engine.translate(case)
        ms = (time.perf_counter() - t0) * 1000.0
        score = poc.chrf(case["reference"], out)
        key_ok = poc.critical_ok(out, case["critical_terms"])
        rss = engine.rss()
        if rss is not None:
            peak_rss = max(peak_rss, rss)
        latencies.append(ms)
        rows.append({
            "id": case["id"],
            "direction": f'{case["source_lang"]}-{case["target_lang"]}',
            "source": case["source"], "reference": case["reference"], "output": out,
            "latency_ms": round(ms, 2), "chrf": round(score, 4),
            "accuracy_proxy_pass": score >= threshold, "critical_pass": key_ok,
        })

    def summary(selected: list[dict]) -> dict:
        timings = [r["latency_ms"] for r in selected]
        return {
            "count": len(selected),
            "critical_error_rate": round(
                sum(not r["critical_pass"] for r in selected) / len(selected), 4),
            "accuracy_proxy": round(
                sum(r["accuracy_proxy_pass"] for r in selected) / len(selected), 4),
            "mean_chrf": round(sum(r["chrf"] for r in selected) / len(selected), 4),
            "latency_ms": {"p50": round(poc.percentile(timings, .5), 2),
                           "p95": round(poc.percentile(timings, .95), 2),
                           "mean": round(sum(timings) / len(timings), 2)},
        }

    directions = sorted({r["direction"] for r in rows})
    return {
        "engine": engine.name, **summary(rows),
        "load_ms": round(engine.load_ms, 2),
        "peak_rss_bytes": peak_rss or None,
        "package_bytes": getattr(engine, "package_bytes", None),
        "by_direction": {d: summary([r for r in rows if r["direction"] == d])
                         for d in directions},
        "rows": rows,
    }


def build_server(cmake: str, jobs: int, arch: str) -> Path:
    """编 llama-server（与 check-translation-quality 同样的 cmake 口径，只换 target）。"""
    build_dir = WORK / f"build-server-{arch_tag(arch)}"
    binary = build_dir / "bin" / "llama-server"
    if binary.is_file():
        return binary
    WORK.mkdir(parents=True, exist_ok=True)
    print(f"  编译 llama-server（{arch}，首次约 1~3 分钟）…", flush=True)
    subprocess.run(
        [cmake, "-S", str(CTQ.LLAMA_SRC), "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release",
         "-DGGML_NATIVE=OFF", f"-DGGML_CPU_ARM_ARCH={arch}",
         "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF", "-DLLAMA_BUILD_TOOLS=ON",
         "-DLLAMA_BUILD_SERVER=ON", "-DLLAMA_CURL=OFF", "-DGGML_METAL=OFF",
         "-DGGML_ACCELERATE=OFF", "-DGGML_BLAS=OFF", "-DGGML_OPENMP=OFF"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    subprocess.run([cmake, "--build", str(build_dir), "--target", "llama-server", "-j", str(jobs)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    if not binary.is_file():
        raise RuntimeError(f"llama-server 编译失败，见 {build_dir}")
    return binary


def main() -> int:
    ap = argparse.ArgumentParser(description="NMT(M2M-100 int8/ORT) vs Qwen2.5 桌面门禁")
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--nmt-kind", choices=("m2m", "opus"), default="m2m",
                    help="m2m=通用多语 M2M-100；opus=专向 OPUS-MT（可 pivot）")
    ap.add_argument("--nmt-dir", type=Path, default=NMT_DIR)
    ap.add_argument("--opus-dir", type=Path, default=OPUS_DIR)
    ap.add_argument("--variant", default="_int8",
                    help="ONNX 档位后缀：_int8 / _fp16 / _q4 / 空串（fp32）")
    ap.add_argument("--route", action="append", default=[], metavar="DIR=HOP[+HOP]",
                    help="覆盖/新增语向接线，如 --route zh-ja=zh-ja（直译）或 "
                         "--route ja-zh=ja-en+en-zh（两跳）。可重复。")
    ap.add_argument("--routes-only", action="store_true",
                    help="只保留 --route 指定的语向（默认与内置接线合并）")
    ap.add_argument("--nmt-only", action="store_true", help="只跑 NMT（不编 llama-server）")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--arch", default=CTQ.DEFAULT_ARCH, help="Qwen 侧的 GGML_CPU_ARM_ARCH")
    ap.add_argument("--accuracy-threshold", type=float, default=.45)
    ap.add_argument("--qwen-model", type=Path,
                    default=ROOT / "models" / "qwen25-1.5b-q4km-zh-ja-ko-en-yue.vtmodel")
    ap.add_argument("--port", type=int, default=18092)
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()

    cases = load_cases(args.dataset)
    dirs = sorted({f'{c["source_lang"]}-{c["target_lang"]}' for c in cases})
    print(f"数据集 {args.dataset.name}：{len(cases)} 条，方向 {dirs}\n")

    routes = None
    if args.route:
        parsed = {}
        for spec in args.route:
            if "=" not in spec:
                raise SystemExit(f"--route 格式应为 DIR=HOP[+HOP]，收到 {spec!r}")
            key, hops = spec.split("=", 1)
            parsed[key.strip()] = tuple(h.strip() for h in hops.split("+") if h.strip())
        routes = parsed if args.routes_only else {**OPUS_ROUTES, **parsed}
        print(f"语向接线覆盖：{sorted(parsed)}\n")

    results, engines = [], []
    try:
        nmt = (OpusBundle(args.opus_dir, threads=args.threads, routes=routes,
                          variant=args.variant)
               if args.nmt_kind == "opus"
               else M2M100Engine(args.nmt_dir, threads=args.threads))
        print(f"NMT 就绪（{args.nmt_kind}）：{nmt.describe()}\n    装载 {nmt.load_ms:.0f} ms\n")
        engines.append(nmt)
        if not args.nmt_only:
            cmake = CTQ.find_cmake()
            server = build_server(cmake, args.jobs, args.arch)
            model = CTQ.resolve_model(args.qwen_model)
            print(f"  Qwen 模型：{model}（arch={CTQ.gguf_arch(model) or '?'}）", flush=True)
            engines.append(QwenServerEngine(server, model, args.port, args.threads,
                                            args.arch))
        for engine in engines:
            print(f"跑 {engine.name} …", flush=True)
            results.append(evaluate(engine, cases, args.accuracy_threshold))
    finally:
        for engine in engines:
            try:
                engine.close()
            except Exception:
                pass

    # ---------------- 输出
    print("\n### 译文对照\n")
    names = [r["engine"] for r in results]
    for case in cases:
        direction = f'{case["source_lang"]}-{case["target_lang"]}'
        print(f'**{case["id"]}**（{direction}）：{case["source"]}')
        print(f'  参考：{case["reference"]}')
        for res in results:
            row = next(r for r in res["rows"] if r["id"] == case["id"])
            mark = "✓" if row["critical_pass"] else "✗"
            print(f'  {mark} {res["engine"]:34} {row["output"]}')
        print()

    print("### 汇总\n")
    print("| 引擎 | 关键错误率 | 平均 chrF | P50 ms | P95 ms | 包体 | 装载 ms |")
    print("|---|---|---|---|---|---|---|")
    for res in results:
        pkg = res["package_bytes"]
        pkg_txt = f'{pkg / 1e9:.2f} GB' if pkg else "-"
        lat = res["latency_ms"]
        print(f'| {res["engine"]} | {res["critical_error_rate"]:.2%} | {res["mean_chrf"]:.4f} | '
              f'{lat["p50"]:.0f} | {lat["p95"]:.0f} | {pkg_txt} | {res["load_ms"]:.0f} |')
    print()

    report = {"schema_version": 1, "dataset": str(args.dataset), "results": results}
    if len(results) == 2:
        qwen, nmt = results[1], results[0]
        gates = {d: poc.gate(qwen["by_direction"][d], nmt["by_direction"][d])
                 for d in nmt["by_direction"] if d in qwen["by_direction"]}
        overall = poc.gate(qwen, nmt)
        report["gates"] = gates
        report["gate_overall"] = overall
        print("### 门禁（NMT 相对 Qwen）\n")
        print("| 方向 | 关键错误率降幅 | P95 降幅 | 是否 promote |")
        print("|---|---|---|---|")
        for d, g in gates.items():
            print(f'| {d} | {g["critical_error_reduction"]:.2%} | '
                  f'{g["p95_latency_reduction"]:.2%} | {"✅ 是" if g["promote_opus"] else "❌ 否"} |')
        print(f'\n**全体**：关键错误率降幅 {overall["critical_error_reduction"]:.2%}，'
              f'P95 降幅 {overall["p95_latency_reduction"]:.2%}，'
              f'promote={overall["promote_opus"]}')
        print(f'\n判据：{overall["rule"]}')

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入 {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
