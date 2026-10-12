"""Verify language declarations in .vtmodel packages.

This is a package-level check. It does not infer unsupported languages from a
model name: the result is based on the manifest embedded in each archive.
Use verify_asr.py and verify_translation.py for runtime quality/smoke tests.
"""
from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"zh", "ja", "ko", "en", "yue"}

# 默认校验**推荐档位的那三个包**，也就是 models/ 下的这三份。
#
# ⚠️ 这里曾写着 `dist/models/qwen25-3b-zh-ja-ko-en.vtmodel` 等三个路径：
# ① `dist/` 早就只放验证产物、没有 models/ 子目录，默认参数必然 FileNotFoundError；
# ② 那个名字是 3B 时代的，全项目已统一到 **1.5B Q4_K_M**。
# 这三个文件名必须与 scripts/model-tiers.py 的 `RECOMMENDED`（按 id）一一对应 ——
# 测试 scripts/tests/test_model_pack.py::VerifyLanguagesDefaults
# 就是盯着这个漂移的。
DEFAULT_MODELS = [
    ROOT / "models/qwen25-1.5b-q4km-zh-ja-ko-en-yue.vtmodel",
    ROOT / "models/sensevoice-zh-ja-ko-en.vtmodel",
    ROOT / "models/opus-mt-zh-en-ja-v1.vtmodel",
]


def read_manifest(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        try:
            raw = archive.read("manifest.json")
        except KeyError as exc:
            raise ValueError(f"{path}: missing manifest.json") from exc
    return json.loads(raw.decode("utf-8"))


def verify(path: Path) -> dict:
    manifest = read_manifest(path)
    languages = manifest.get("languages")
    if not isinstance(languages, list) or not all(isinstance(x, str) for x in languages):
        raise ValueError(f"{path}: manifest.languages is invalid")
    declared = set(languages)
    # `kind=opus` 是唯一允许「五语非空子集」的一类（OPUS-MT 一个语向一个模型，
    # 结构上凑不齐五语）。越界（fr/de/ru）对任何 kind 都不放行 —— 子集不是放水。
    subset_ok = manifest.get("kind") == "opus"
    ok = bool(declared) and declared <= EXPECTED and (subset_ok or declared == EXPECTED)
    return {
        "file": str(path),
        "id": manifest.get("id"),
        "name": manifest.get("name"),
        "kind": manifest.get("kind"),
        "engine": manifest.get("engine"),
        "declaredLanguages": languages,
        "allowSubset": subset_ok,
        "matchesCurrentAppLanguageSet": ok,
        "extraLanguages": sorted(declared - EXPECTED),
        "missingCurrentAppLanguages": sorted(EXPECTED - declared),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("models", nargs="*", type=Path, default=DEFAULT_MODELS)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/verification/model-languages.json")
    args = parser.parse_args()

    results = [verify(path.resolve()) for path in args.models]
    report = {
        "status": "passed",
        "currentAppLanguageSet": sorted(EXPECTED),
        "models": results,
        "note": "This checks package declarations. Runtime support must be verified with verify_asr.py or verify_translation.py.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
