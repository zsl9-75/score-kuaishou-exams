#!/usr/bin/env python3
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REQUIRED = {
    "SKILL.md",
    "agents/openai.yaml",
    "references/exam_profiles.json",
    "references/evidence-schema.md",
    "references/manifest.md",
    "scripts/run_assessment.py",
    "scripts/manifest_runtime.py",
    "scripts/build_workbook.py",
    "scripts/ocr_api.py",
    "scripts/ocr_vision.swift",
    "requirements.txt",
}
EXCLUDED_PARTS = {".git", "__pycache__", ".ruff_cache", ".score-cache", "output", "incoming", "dist"}


def package(output: Path) -> Path:
    missing = sorted(item for item in REQUIRED if not (ROOT / item).is_file())
    if missing:
        raise RuntimeError("Skill缺少必需文件：" + ", ".join(missing))
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = ROOT.name
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or any(part in EXCLUDED_PARTS for part in path.relative_to(ROOT).parts):
                continue
            archive.write(path, Path(prefix) / path.relative_to(ROOT))
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        expected = {str(Path(prefix) / item) for item in REQUIRED}
        absent = sorted(expected - names)
        if absent:
            raise RuntimeError("ZIP结构校验失败：" + ", ".join(absent))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="按Codex Skill目录结构打包，禁止扁平化")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / f"{ROOT.name}.zip")
    args = parser.parse_args()
    print(package(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
