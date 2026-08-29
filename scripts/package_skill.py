#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if (SCRIPT_DIR.parent / "SKILL.md").is_file():
    ROOT = SCRIPT_DIR.parent
    FLAT_LAYOUT = False
elif (SCRIPT_DIR / "SKILL.md").is_file():
    ROOT = SCRIPT_DIR
    FLAT_LAYOUT = True
else:
    ROOT = SCRIPT_DIR.parent
    FLAT_LAYOUT = False
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
OPTIONAL_PACKAGED = {"scripts/package_skill.py", "scripts/test_pipeline.py"}
FLAT_SOURCE_NAMES = {
    "agents/openai.yaml": "openai.yaml",
    "references/exam_profiles.json": "exam_profiles.json",
    "references/evidence-schema.md": "evidence-schema.md",
    "references/manifest.md": "manifest.md",
    "scripts/run_assessment.py": "run_assessment.py",
    "scripts/manifest_runtime.py": "manifest_runtime.py",
    "scripts/build_workbook.py": "build_workbook.py",
    "scripts/ocr_api.py": "ocr_api.py",
    "scripts/ocr_vision.swift": "ocr_vision.swift",
    "scripts/package_skill.py": "package_skill.py",
    "scripts/test_pipeline.py": "test_pipeline.py",
}


def source_path(item: str) -> Path:
    return ROOT / (FLAT_SOURCE_NAMES.get(item, item) if FLAT_LAYOUT else item)


def skill_name() -> str:
    skill_text = source_path("SKILL.md").read_text(encoding="utf-8")
    match = re.search(r"(?m)^name:\s*([a-z0-9-]+)\s*$", skill_text)
    if not match:
        raise RuntimeError("SKILL.md 缺少合法的 name 字段，无法确定稳定包名")
    return match.group(1)


def package(output: Path) -> Path:

    missing = sorted(item for item in REQUIRED if not source_path(item).is_file())
    if missing:
        raise RuntimeError("Skill缺少必需文件：" + ", ".join(missing))
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = skill_name()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(REQUIRED | OPTIONAL_PACKAGED):
            path = source_path(item)
            if path.is_file():
                archive.write(path, Path(prefix) / item)
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        expected = {str(Path(prefix) / item) for item in REQUIRED}
        absent = sorted(expected - names)
        if absent:
            raise RuntimeError("ZIP结构校验失败：" + ", ".join(absent))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="按Codex Skill目录结构打包，禁止扁平化")
    try:
        parser.add_argument("--output", type=Path, default=ROOT / "dist" / f"{skill_name()}.zip")
        args = parser.parse_args()
        print(package(args.output))
        return 0
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
