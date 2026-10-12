"""`check-translation-quality.py` 的契约测试。

这个脚本是**评测工具**，它的 bug 形态通常是「静默给出好看的结论」——
比如把「没有判据」记成「完全正确」、把 `--shots 5` 悄悄钳成 3 之后照样报
「对照完成」。所以下面的用例重点全在**失败必须是响的**上。

跑法：
    python3 -m unittest discover -s scripts/tests -v
    python3 scripts/tests/test_translation_quality.py
"""

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-translation-quality.py"


def _load_dashed(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


CTQ = _load_dashed("ctq_test", SCRIPT)
REAL_DATASET = ROOT / "evaluation" / "zh_en_ja_sample.jsonl"


def _write(dirpath: Path, rows: list) -> Path:
    path = dirpath / "d.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                    encoding="utf-8")
    return path


class PromptContract(unittest.TestCase):
    """prompt 模板表之间的一致性。任何一处漂移都会让「评的不是生产路径」。"""

    def test_name_matches_native_both_ways(self):
        self.assertEqual(set(CTQ.NAME.values()), set(CTQ.NATIVE),
                         "NAME 的语言码与 NATIVE 的显示名必须一一对应")
        self.assertEqual({v: k for k, v in CTQ.NAME.items()}, CTQ.REV)

    def test_rule_covers_every_language(self):
        self.assertEqual(set(CTQ.RULE), set(CTQ.NATIVE),
                         "每个语言都要有字符集级约束，否则脚本拦不住混入源语言")

    def test_few_shot_examples_cover_the_five_main_languages(self):
        for group in CTQ.EXAMPLES:
            self.assertEqual(set(group), {"Chinese", "Japanese", "Korean",
                                          "Cantonese", "English"})

    def test_shots_is_inside_examples(self):
        self.assertTrue(1 <= CTQ.SHOTS <= len(CTQ.EXAMPLES))

    def test_qwen_branch_has_no_think_block(self):
        """Qwen2.5 没有 thinking 概念 —— 补了会被当正文输出（见脚本注释）。"""
        prompt = CTQ.build_prompt("Chinese", "Japanese", "你好。", "qwen2")
        self.assertNotIn("<think>", prompt)
        self.assertIn("<|im_start|>assistant\n", prompt)
        self.assertTrue(prompt.endswith("<|im_start|>assistant\n"))

    def test_shots_controls_prefix_length(self):
        src = CTQ.build_prompt("Chinese", "Japanese", "你好。", "qwen2")
        saved = CTQ.SHOTS
        try:
            CTQ.SHOTS = 1
            fewer = CTQ.build_prompt("Chinese", "Japanese", "你好。", "qwen2")
        finally:
            CTQ.SHOTS = saved
        self.assertLess(len(fewer), len(src))
        # 正文部分必须完全一致 —— 否则「逐用例相减」这件事就不成立了
        self.assertTrue(src.endswith("<|im_start|>user\n你好。<|im_end|>\n<|im_start|>assistant\n"))
        self.assertTrue(fewer.endswith("<|im_start|>user\n你好。<|im_end|>\n<|im_start|>assistant\n"))


class DatasetLoading(unittest.TestCase):
    def test_real_dataset_loads(self):
        cases = CTQ.load_dataset(REAL_DATASET)
        self.assertEqual(len(cases), 20)
        self.assertEqual(sorted({c["dir"] for c in cases}),
                         ["en-zh", "ja-zh", "zh-en", "zh-ja"])
        for case in cases:
            self.assertEqual(set(case), {"id", "dir", "src", "dst", "src_code", "dst_code",
                                         "source", "reference", "critical_terms"})
            self.assertIn(case["src"], CTQ.NATIVE)
            self.assertIn(case["dst"], CTQ.NATIVE)
            self.assertNotEqual(case["src"], case["dst"])
            # 英文名与语言码必须同时带着：Qwen 分支要前者，Gemma 分支要后者
            self.assertEqual(CTQ.NAME[case["src_code"]], case["src"])
            self.assertEqual(CTQ.NAME[case["dst_code"]], case["dst"])
            self.assertTrue(case["reference"])
        with_critical = [c for c in cases if c["critical_terms"]]
        self.assertGreater(len(with_critical), 0, "评测集必须带 critical_terms 才测得出关键项错误")

    def test_unknown_language_code_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write(Path(d), [{"id": "a", "source_lang": "zh", "target_lang": "xx",
                                     "source": "x", "reference": "y"}])
            with self.assertRaisesRegex(ValueError, "未知语言码 'xx'"):
                CTQ.load_dataset(path)

    def test_identical_direction_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write(Path(d), [{"id": "a", "source_lang": "zh", "target_lang": "zh",
                                     "source": "x", "reference": "y"}])
            with self.assertRaisesRegex(ValueError, "源语言与目标语言相同"):
                CTQ.load_dataset(path)

    def test_missing_field_names_the_field(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write(Path(d), [{"id": "a", "source_lang": "zh", "target_lang": "en",
                                     "source": "x"}])
            with self.assertRaisesRegex(ValueError, r"缺少字段 \['reference'\]"):
                CTQ.load_dataset(path)

    def test_empty_dataset_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "empty.jsonl"
            path.write_text("# 只有注释\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "是空的"):
                CTQ.load_dataset(path)


class BuiltinCases(unittest.TestCase):
    def test_shape_and_no_reference(self):
        cases = CTQ.builtin_cases(["zh2ja"])
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["dir"], "zh-ja")
        self.assertEqual(cases[0]["src"], "Chinese")
        self.assertEqual(cases[0]["src_code"], "zh")
        self.assertEqual(cases[0]["dst_code"], "ja")
        # 内置例刻意没有参考译文 —— 别偷偷补一个，否则会得出假的质量结论
        self.assertEqual(cases[0]["reference"], "")
        self.assertEqual(cases[0]["critical_terms"], [])

    def test_ids_match_cases_table(self):
        self.assertEqual([c["id"] for c in CTQ.builtin_cases(list(CTQ.CASES))],
                         list(CTQ.CASES))


class EngineDispatch(unittest.TestCase):
    """🔴 两个引擎的 prompt 毫无共同点，绝不能拿一套打通。

    这个包现在同时躺着 Qwen2.5 与 TranslateGemma，而 `default_targets()` 会把
    **两者都**收进来 —— 一旦分派错了，译文会乱，但输出长得像「这个模型质量差」。
    """

    REAL_QWEN = ROOT / "models" / "qwen25-1.5b-q4km-zh-ja-ko-en-yue.vtmodel"
    REAL_GEMMA = ROOT / "models" / "translategemma-4b-text-q3_k_m.vtmodel"

    def test_gemma_prompt_matches_translator_cpp(self):
        """逐字对齐 translator.cpp:76-77 / PromptBuilder.swift:91-98。"""
        p = CTQ.build_gemma_prompt("zh", "ja", "你好。")
        prefix = ("<bos><start_of_turn>user\n"
                  "You are a professional Chinese (zh) to Japanese (ja) translator. "
                  "Your goal is to accurately convey the meaning and nuances of the original "
                  "Chinese text while adhering to Japanese grammar, vocabulary, and cultural "
                  "sensitivities.\n"
                  "Produce only the Japanese translation, without any additional explanations "
                  "or commentary. Please translate the following Chinese text into Japanese:\n\n\n")
        self.assertTrue(p.startswith(prefix))
        self.assertEqual(p[len(prefix):], "你好。<end_of_turn>\n<start_of_turn>model\n")

    def test_gemma_prompt_has_no_few_shot(self):
        """Gemma 分支是单轮 —— 不该出现任何 ChatML/few-shot 痕迹。"""
        p = CTQ.build_gemma_prompt("zh", "en", "你好。")
        for marker in ("<|im_start|>", "<|im_end|>", "你好。<|im_end|>",
                       "This is a book.", "nearest hospital"):
            self.assertNotIn(marker, p)
        self.assertNotIn("Cantonese", p)

    def test_qwen_prompt_is_unchanged_by_gemma_support(self):
        p = CTQ.build_prompt("Chinese", "Japanese", "你好。", "qwen2")
        self.assertIn("<|im_start|>system\n", p)
        self.assertIn("最近的医院在哪里？", p)   # few-shot 第 3 组

    def test_render_prompt_dispatches_on_engine(self):
        case = {"src": "Chinese", "dst": "Japanese", "src_code": "zh", "dst_code": "ja",
                "source": "你好。"}
        self.assertEqual(CTQ.render_prompt(CTQ.ENGINE_GEMMA, case, "gemma3"),
                         CTQ.build_gemma_prompt("zh", "ja", "你好。"))
        self.assertEqual(CTQ.render_prompt(CTQ.ENGINE_QWEN, case, "qwen2"),
                         CTQ.build_prompt("Chinese", "Japanese", "你好。", "qwen2"))
        # 两者的 prompt 必须真的不同 —— 这是「不能一套打通」的实证
        self.assertNotEqual(CTQ.render_prompt(CTQ.ENGINE_GEMMA, case, "gemma3"),
                            CTQ.render_prompt(CTQ.ENGINE_QWEN, case, "qwen2"))

    def test_unknown_engine_is_refused_not_defaulted(self):
        """判不出引擎必须报错。以前会默认按 Qwen 处理 —— 那是最坏的行为。"""
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "mystery.gguf"
            fake.write_bytes(b"GGUF" + b"\x00" * 32)   # 头都不完整 → 猜不出来
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    CTQ.require_engine(fake, fake, "")
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):   # --engine 写错也要拦住
                    CTQ.require_engine(fake, fake, "llama-llama")

    def test_engine_is_read_from_package_manifest(self):
        """真包上验证：两个包判出来的引擎必须不同，且不是靠猜的。"""
        if not self.REAL_QWEN.is_file() or not self.REAL_GEMMA.is_file():
            self.skipTest("models/ 下没有两个翻译包（只在准备了包的工作区跑）")
        qwen, why_q = CTQ.require_engine(self.REAL_QWEN, self.REAL_QWEN)
        gemma, why_g = CTQ.require_engine(self.REAL_GEMMA, self.REAL_GEMMA)
        self.assertEqual((qwen, gemma), (CTQ.ENGINE_QWEN, CTQ.ENGINE_GEMMA))
        self.assertEqual((why_q, why_g), ("manifest", "manifest"))   # 不是猜的


class TokenizeAlignment(unittest.TestCase):
    """🔴 探针的 tokenize 必须与 App 一致，否则测的不是生产路径。

    `llama-completion` 走 `common_tokenize(ctx, prompt, /*add_special*/ true, true)`
    —— 会**额外补一个 BOS**；两端 App 走 `llama_tokenize(..., add_special=false, ...)`。
    对 `add_bos_token=false` 的模型（Qwen2.5）两者等价，所以这个坑一直没露头；
    TranslateGemma 的 gguf 是 `add_bos_token=True`，探针就会喂
    `<bos><bos><start_of_turn>…`，实测译文前面直接多出垃圾 token
    （`'disputa, トイレはどこですか？'`），而端上没这个问题。

    修法：给它 `--override-kv tokenizer.ggml.add_bos_token=bool:false`。
    """

    QWEN = ROOT / "models" / "qwen25-1.5b-q4km-zh-ja-ko-en-yue.vtmodel"
    GEMMA = ROOT / "models" / "translategemma-4b-text-q3_k_m.vtmodel"

    def _gguf(self, pack):
        if not pack.is_file():
            self.skipTest(f"{pack.name} 不在 models/ 下")
        try:
            return CTQ.resolve_model(pack)
        except SystemExit:
            self.skipTest(f"{pack.name} 解包失败")

    def test_qwen_needs_no_override(self):
        """Qwen 的 add_bos_token=false → 探针的 add_special 是空操作，本来就一致。"""
        self.assertFalse(CTQ.gguf_add_bos(self._gguf(self.QWEN)))

    def test_gemma_needs_the_override(self):
        """Gemma 的 add_bos_token=true → 不加覆盖就会多喂一个 BOS。"""
        self.assertTrue(CTQ.gguf_add_bos(self._gguf(self.GEMMA)))

    def test_override_is_a_wellformed_kv_pair(self):
        self.assertEqual(CTQ.BOS_OVERRIDE,
                         ("--override-kv", "tokenizer.ggml.add_bos_token=bool:false"))

    def test_argv_carries_the_override(self):
        argv = CTQ.completion_argv(Path("llama-completion"), Path("m.gguf"),
                                   "PROMPT", 2, CTQ.BOS_OVERRIDE)
        self.assertIn("--override-kv", argv)
        self.assertIn("tokenizer.ggml.add_bos_token=bool:false", argv)
        # -no-cnv 是另一个必需的开关（否则 prompt 会被 chat template 再套一次）
        self.assertIn("-no-cnv", argv)
        self.assertEqual(argv[argv.index("-t") + 1], "2")

    def test_argv_passes_prompt_as_argument_not_file(self):
        """🔴 `-f` 会吃掉 prompt 末尾的一个 `\\n`，而 App 的 suffix 末尾就有那个 `\\n`。

        实测对照（同一串）：结尾 1 个 `\\n` 时 `-p` 43 token / `-f` **42** token。
        症状：Gemma 恒定吐垃圾前缀（`'disputa, トイレはどこですか？'`），看着像模型不行。
        """
        argv = CTQ.completion_argv(Path("b"), Path("m"), "PROMPT\n", 2)
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "PROMPT\n")   # 末尾换行原样带过去
        self.assertNotIn("-f", argv)                                # 绝不用文件
        self.assertIn("--no-escape", argv)                          # 别把真换行当转义序列

    def test_argv_without_override_has_none(self):
        argv = CTQ.completion_argv(Path("b"), Path("m"), "P", 2)
        self.assertNotIn("--override-kv", argv)

    def test_gguf_meta_reads_both_types(self):
        """解析器同时要能取 string 与 bool —— 只取一种会让 add_bos 恒为 False。"""
        gguf = self._gguf(self.QWEN)
        meta = CTQ.gguf_meta(gguf, {"general.architecture",
                                    "tokenizer.ggml.add_bos_token"})
        self.assertEqual(meta.get("general.architecture"), "qwen2")
        self.assertIs(meta.get("tokenizer.ggml.add_bos_token"), False)

    def test_gguf_meta_on_garbage_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "x.gguf"
            fake.write_bytes(b"NOT-GGUF" + b"\x00" * 64)
            self.assertEqual(CTQ.gguf_meta(fake, {"general.architecture"}), {})
            self.assertFalse(CTQ.gguf_add_bos(fake))


class Scoring(unittest.TestCase):
    def test_missing_reference_is_none_not_zero(self):
        case = {"reference": "", "critical_terms": [["airport"]]}
        self.assertEqual(CTQ.score(case, "译得再好也没用"), {"chrf": None, "critical": None})

    def test_empty_critical_terms_is_none_not_true(self):
        """`critical_ok(text, [])` 恒为 True —— 直接上报会伪装成「零错误」。"""
        self.assertEqual(CTQ.score({"reference": "r", "critical_terms": []}, "anything"),
                         {"chrf": 0.0, "critical": None})

    def test_identical_text_scores_perfect(self):
        case = {"reference": "Where is the airport?", "critical_terms": [["airport"]]}
        got = CTQ.score(case, "Where is the airport?")
        self.assertEqual(got["chrf"], 1.0)
        self.assertTrue(got["critical"])

    def test_wrong_negation_fails_critical(self):
        case = {"reference": "I do not want to go.",
                "critical_terms": [["not", "don't", "do not"], ["airport"]]}
        self.assertFalse(CTQ.score(case, "I want to go to the airport.")["critical"])

    def test_empty_hypothesis_is_detected(self):
        case = {"reference": "Where is the airport?", "critical_terms": [["airport"]]}
        got = CTQ.score(case, "")
        self.assertEqual(got["chrf"], 0.0)
        self.assertFalse(got["critical"], "空译文/超时必须判为失败，不能被跳过")


class Summarize(unittest.TestCase):
    def test_medians_skip_missing_entries(self):
        cases = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        rows = {"a": {"chrf": 1.0, "critical": True, "prompt_tokens": "100", "prefill_ms": "1000.0"},
                "b": {"chrf": 0.0, "critical": False, "prompt_tokens": "", "prefill_ms": ""},
                "c": {"chrf": 0.5, "critical": True, "prompt_tokens": "140", "prefill_ms": "1400.0"}}
        s = CTQ.summarize(cases, rows)
        self.assertEqual(s["prompt_tokens"], 140)          # 去掉空项后取中位，不填 0
        self.assertEqual(s["ms_n"], 2)
        self.assertEqual(s["critical_errors"], 1)
        self.assertEqual(s["critical_n"], 3)
        self.assertAlmostEqual(s["mean_chrf"], 0.5)

    def test_all_missing_reports_zero_denominator(self):
        """不能把「没有判据」说成「零错误」—— 分母必须一起报出来给人看。"""
        cases = [{"id": "a"}]
        rows = {"a": {"chrf": None, "critical": None, "prompt_tokens": "", "prefill_ms": ""}}
        s = CTQ.summarize(cases, rows)
        self.assertIsNone(s["mean_chrf"])
        self.assertEqual(s["chrf_n"], 0)
        self.assertEqual(s["critical_n"], 0)   # 调用方要据此拒绝下结论


class ShotsValidation(unittest.TestCase):
    """静默钳制是本仓库点过名的反模式 —— 参数越界必须报错。"""

    def test_out_of_range_exits(self):
        self.assertEqual(CTQ.check_shots(1), 1)
        self.assertEqual(CTQ.check_shots(len(CTQ.EXAMPLES)), len(CTQ.EXAMPLES))
        # die() 会把原因打到 stderr；测试里吞掉，免得刷屏掩盖真正的失败
        with contextlib.redirect_stderr(io.StringIO()):
            for bad in (0, len(CTQ.EXAMPLES) + 1, 99):
                with self.assertRaises(SystemExit):
                    CTQ.check_shots(bad)


class CommandLine(unittest.TestCase):
    """CLI 层的失败路径 —— 这些是「跑了个寂寞但输出『对照完成』」的来源。"""

    def _run(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              capture_output=True, text=True, errors="replace",
                              stdin=subprocess.DEVNULL, cwd=ROOT, timeout=600)

    def test_shots_over_the_limit_is_rejected(self):
        r = self._run("--shots", "5")
        self.assertEqual(r.returncode, 1)
        self.assertIn("--shots 必须在", r.stderr)

    def test_compare_shots_needs_two_distinct_values(self):
        for arg, needle in (("3", "正好两个值"), ("3,3", "相同"),
                            ("a,b", "必须都是整数"), ("7,1", "--shots 必须在")):
            r = self._run("--compare-shots", arg, "--dataset", str(REAL_DATASET))
            self.assertEqual(r.returncode, 1, f"{arg} 应该被拒绝")
            self.assertIn(needle, r.stderr, f"{arg} 的报错文案应点明原因")

    def test_dataset_mode_rejects_broken_input(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write(Path(d), [{"id": "a", "source_lang": "zh", "target_lang": "en",
                                     "source": "x"}])
            r = self._run("--dataset", str(path))
            self.assertEqual(r.returncode, 1)
            self.assertIn("缺少字段", r.stderr)

    def test_dataset_case_filter_must_match_something(self):
        r = self._run("--dataset", str(REAL_DATASET), "--case", "zz-zz")
        self.assertEqual(r.returncode, 1)
        self.assertIn("没有任何用例", r.stderr)

    def test_unknown_builtin_case_lists_the_valid_ones(self):
        r = self._run("--case", "zh2Jp")
        self.assertEqual(r.returncode, 1)
        self.assertIn("未知用例", r.stderr)
        self.assertIn("zh2ja", r.stderr)


class SharedNameSource(unittest.TestCase):
    """`check-nmt-quality.py` 必须复用这里的 NAME，不能再写第二份。"""

    def test_nmt_checker_reuses_this_name_table(self):
        mod = _load_dashed("cnq_test", ROOT / "scripts" / "check-nmt-quality.py")
        # 同一份对象（说明是 `NAME = CTQ.NAME` 而不是又抄了一遍字面量）
        self.assertIs(mod.NAME, mod.CTQ.NAME)
        # 且值与本测试加载到的一致（两个模块实例不同，只能比值）
        self.assertEqual(mod.NAME, CTQ.NAME)


if __name__ == "__main__":
    unittest.main()
