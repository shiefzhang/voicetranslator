"""Reproducible Qwen vs OPUS-MT zh<->en evaluation.

The input is JSONL.  Each row contains id, source_lang, target_lang, source,
reference and optional critical_terms.  A critical term is a list of accepted
surface forms; at least one form must occur in the translation.

This intentionally runs outside the Android app: Qwen stays the production
default until the report proves that OPUS-MT meets the promotion gate.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODELS = {
    "en-zh": "Helsinki-NLP/opus-mt-en-zh",
    "zh-en": "Helsinki-NLP/opus-mt-zh-en",
}
NATIVE = {"zh": "Chinese", "en": "English"}


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    lo, hi = math.floor(rank), math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def _ngrams(text: str, n: int) -> Counter[str]:
    compact = "".join(text.lower().split())
    return Counter(compact[i:i+n] for i in range(max(0, len(compact)-n+1)))


def chrf(reference: str, hypothesis: str) -> float:
    """Dependency-free chrF-like score in [0, 1], averaged over n=1..6."""
    scores = []
    for n in range(1, 7):
        ref, hyp = _ngrams(reference, n), _ngrams(hypothesis, n)
        if not ref and not hyp:
            scores.append(1.0)
            continue
        overlap = sum((ref & hyp).values())
        precision = overlap / max(1, sum(hyp.values()))
        recall = overlap / max(1, sum(ref.values()))
        beta2 = 4.0
        scores.append((1 + beta2) * precision * recall /
                      max(1e-12, beta2 * precision + recall))
    return statistics.fmean(scores)


def critical_ok(text: str, groups: list[list[str]]) -> bool:
    lowered = text.casefold()
    return all(any(term.casefold() in lowered for term in alternatives)
               for alternatives in groups)


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        row = json.loads(line)
        required = {"id", "source_lang", "target_lang", "source", "reference"}
        missing = required - row.keys()
        if missing:
            raise ValueError(f"{path}:{line_no}: missing {sorted(missing)}")
        direction = f'{row["source_lang"]}-{row["target_lang"]}'
        if direction not in DEFAULT_MODELS:
            raise ValueError(f"{path}:{line_no}: PoC only supports zh-en/en-zh")
        row.setdefault("critical_terms", [])
        cases.append(row)
    if not cases:
        raise ValueError("dataset is empty")
    return cases


def process_rss_bytes(pid: int) -> int | None:
    try:
        import psutil
        proc = psutil.Process(pid)
        return proc.memory_info().rss + sum(
            (child.memory_info().rss for child in proc.children(recursive=True)), 0)
    except (ImportError, OSError):
        return None


class QwenEngine:
    name = "qwen"

    def __init__(self, server: Path, model: Path, port: int, external_url: str | None = None):
        load_started = time.perf_counter()
        self.url = external_url.rstrip("/") if external_url else f"http://127.0.0.1:{port}"
        self.proc = None if external_url else subprocess.Popen([
                str(server.resolve()), "-m", str(model.resolve()), "--host", "127.0.0.1",
                "--port", str(port), "-c", "2048", "-t", "3", "--parallel", "1",
                "--no-warmup"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(120):
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError("Qwen server exited during startup")
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(1)
        else:
            raise TimeoutError("Qwen server was not ready after 120 seconds")
        self.load_ms = (time.perf_counter() - load_started) * 1000
        self.package_bytes = model.stat().st_size

    def translate(self, row: dict) -> str:
        src, dst = NATIVE[row["source_lang"]], NATIVE[row["target_lang"]]
        payload = {"messages": [
            {"role": "system", "content": f"Translate from {src} into {dst}. Output only the faithful translation. Preserve names, numbers and negations. Do not explain."},
            {"role": "user", "content": row["source"]}],
            "temperature": 0, "max_tokens": 256}
        request = urllib.request.Request(self.url + "/v1/chat/completions",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
        return result["choices"][0]["message"]["content"].strip()

    def rss(self) -> int | None:
        return process_rss_bytes(self.proc.pid) if self.proc is not None else None

    def close(self):
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class OpusEngine:
    name = "opus-mt"

    def __init__(self, model_ids: dict[str, str]):
        try:
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError("Install scripts/requirements-translation-poc.txt first") from exc
        self.models = {}
        load_started = time.perf_counter()
        snapshots = []
        for direction, model_id in model_ids.items():
            tokenizer = AutoTokenizer.from_pretrained(model_id)
            model = AutoModelForSeq2SeqLM.from_pretrained(model_id)
            model.eval()
            self.models[direction] = (tokenizer, model)
            try:
                from huggingface_hub import snapshot_download
                snapshots.append(Path(snapshot_download(model_id, local_files_only=True)))
            except Exception:
                pass
        self.load_ms = (time.perf_counter() - load_started) * 1000
        files = {f.resolve() for root in snapshots for f in root.rglob("*") if f.is_file()}
        self.package_bytes = sum(f.stat().st_size for f in files)

    def translate(self, row: dict) -> str:
        import torch
        tokenizer, model = self.models[f'{row["source_lang"]}-{row["target_lang"]}']
        inputs = tokenizer(row["source"], return_tensors="pt")
        with torch.inference_mode():
            output = model.generate(**inputs, num_beams=4, max_new_tokens=256)
        return tokenizer.decode(output[0], skip_special_tokens=True).strip()

    def rss(self) -> int | None:
        return process_rss_bytes(os.getpid())

    def close(self):
        self.models.clear()


def evaluate(engine, cases: list[dict], accuracy_threshold: float) -> dict:
    warmup_ms = {}
    for direction in ("zh-en", "en-zh"):
        case = next((c for c in cases if f'{c["source_lang"]}-{c["target_lang"]}' == direction), None)
        if case:
            started = time.perf_counter()
            engine.translate(case)
            warmup_ms[direction] = round((time.perf_counter() - started) * 1000, 2)
    rows, latencies, peak_rss = [], [], 0
    for case in cases:
        started = time.perf_counter()
        output = engine.translate(case)
        elapsed = (time.perf_counter() - started) * 1000
        score = chrf(case["reference"], output)
        key_ok = critical_ok(output, case["critical_terms"])
        rss = engine.rss()
        if rss is not None:
            peak_rss = max(peak_rss, rss)
        latencies.append(elapsed)
        rows.append({"id": case["id"], "direction": f'{case["source_lang"]}-{case["target_lang"]}',
                     "source": case["source"], "reference": case["reference"],
                     "output": output, "latency_ms": round(elapsed, 2),
                     "chrf": round(score, 4), "accuracy_proxy_pass": score >= accuracy_threshold,
                     "critical_pass": key_ok, "manual": None})
    def summary(selected: list[dict]) -> dict:
        timings = [r["latency_ms"] for r in selected]
        return {"count": len(selected),
                "accuracy_proxy": round(sum(r["accuracy_proxy_pass"] for r in selected) / len(selected), 4),
                "critical_error_rate": round(sum(not r["critical_pass"] for r in selected) / len(selected), 4),
                "latency_ms": {"p50": round(percentile(timings, .5), 2),
                               "p95": round(percentile(timings, .95), 2)}}
    by_direction = {direction: summary([r for r in rows if r["direction"] == direction])
                    for direction in ("zh-en", "en-zh")
                    if any(r["direction"] == direction for r in rows)}
    return {"engine": engine.name, **summary(rows),
            "load_ms": round(engine.load_ms, 2), "warmup_ms": warmup_ms,
            "by_direction": by_direction,
            "peak_rss_bytes": peak_rss or None,
            "package_bytes": engine.package_bytes or None, "rows": rows}


def gate(qwen: dict, opus: dict) -> dict:
    base_error = qwen["critical_error_rate"]
    error_reduction = ((base_error - opus["critical_error_rate"]) / base_error
                       if base_error else 0.0)
    latency_reduction = ((qwen["latency_ms"]["p95"] - opus["latency_ms"]["p95"])
                         / max(qwen["latency_ms"]["p95"], 1e-9))
    return {"critical_error_reduction": round(error_reduction, 4),
            "p95_latency_reduction": round(latency_reduction, 4),
            "promote_opus": error_reduction >= .20 or latency_reduction >= .35,
            "rule": "critical error reduction >=20% OR P95 latency reduction >=35%",
            "note": "Promotion still requires manual blind-review completion and no package/license regression."}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT/"evaluation/zh_en_asr_sample.jsonl")
    parser.add_argument("--qwen-server", type=Path, default=ROOT/".tools/llama-win/llama-server.exe")
    parser.add_argument("--qwen-model", type=Path, default=ROOT/"model-work/translation/model.gguf")
    parser.add_argument("--port", type=int, default=18091)
    parser.add_argument("--qwen-url", help="Use an already running OpenAI-compatible Qwen server")
    parser.add_argument("--accuracy-threshold", type=float, default=.45)
    parser.add_argument("--output", type=Path, default=ROOT/"dist/verification/translation-poc.json")
    parser.add_argument("--opus-zh-en", default=DEFAULT_MODELS["zh-en"])
    parser.add_argument("--opus-en-zh", default=DEFAULT_MODELS["en-zh"])
    args = parser.parse_args()
    cases = load_cases(args.dataset)
    if any(sum(c["source_lang"] == lang for c in cases) < 200 for lang in ("zh", "en")):
        print("WARNING: fewer than 200 cases per direction; report is pre-validation only", file=sys.stderr)
    results, engines = [], []
    try:
        engines.append(QwenEngine(args.qwen_server, args.qwen_model, args.port, args.qwen_url))
        engines.append(OpusEngine({"zh-en": args.opus_zh_en, "en-zh": args.opus_en_zh}))
        for engine in engines:
            results.append(evaluate(engine, cases, args.accuracy_threshold))
    finally:
        for engine in engines:
            engine.close()
    report = {"schema_version": 1, "dataset": str(args.dataset),
              "dataset_sha256": __import__("hashlib").sha256(args.dataset.read_bytes()).hexdigest(),
              "status": "formal" if all(sum(c["source_lang"] == lang for c in cases) >= 200
                                          for lang in ("zh", "en")) else "pre_validation",
              "results": results,
              "gates": {direction: gate(results[0]["by_direction"][direction],
                                         results[1]["by_direction"][direction])
                        for direction in results[0]["by_direction"]}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "gates": report["gates"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
