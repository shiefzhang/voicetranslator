import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from translation_poc import chrf, critical_ok, gate, load_cases, percentile


class TranslationPocTests(unittest.TestCase):
    def test_percentile_interpolates(self):
        self.assertEqual(percentile([10, 20, 30], .5), 20)
        self.assertAlmostEqual(percentile([10, 20], .95), 19.5)

    def test_chrf(self):
        self.assertEqual(chrf("same", "same"), 1.0)
        self.assertLess(chrf("airport", "railway"), .2)

    def test_critical_alternatives(self):
        groups = [["not", "don't"], ["airport"]]
        self.assertTrue(critical_ok("I do not want the airport", groups))
        self.assertFalse(critical_ok("I want the airport", groups))

    def test_gate_is_or(self):
        qwen = {"critical_error_rate": .2, "latency_ms": {"p95": 1000}}
        fast = {"critical_error_rate": .2, "latency_ms": {"p95": 600}}
        accurate = {"critical_error_rate": .1, "latency_ms": {"p95": 1000}}
        self.assertTrue(gate(qwen, fast)["promote_opus"])
        self.assertTrue(gate(qwen, accurate)["promote_opus"])
        unchanged = {"critical_error_rate": .2, "latency_ms": {"p95": 900}}
        self.assertFalse(gate(qwen, unchanged)["promote_opus"])

    def test_dataset_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"data.jsonl"
            path.write_text(json.dumps({"id":"x", "source_lang":"ja", "target_lang":"en",
                "source":"x", "reference":"x"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "only supports"):
                load_cases(path)


if __name__ == "__main__":
    unittest.main()
