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
EXPECTED = {"zh", "ja", "ko", "en"}
DEFAULT_MODELS = [
    ROOT / "dist/models/qwen25-3b-zh-ja-ko-en.vtmodel",
    ROOT / "dist/models/sensevoice-zh-ja-ko-en.vtmodel",
    ROOT / "dist/models/translategemma-4b-text-q4_k_m.vtmodel",
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
    return {
        "file": str(path),
        "id": manifest.get("id"),
        "name": manifest.get("name"),
        "kind": manifest.get("kind"),
        "engine": manifest.get("engine"),
        "declaredLanguages": languages,
        "matchesCurrentAppLanguageSet": declared == EXPECTED,
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
