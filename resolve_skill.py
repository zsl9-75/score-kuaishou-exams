#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


ENTRYPOINTS = (
    "manifest_runtime.py",
    "run_assessment.py",
    "compose_score_images.py",
)


def skill_root() -> Path:
    location = Path(__file__).resolve().parent
    for candidate in (location, location.parent):
        if (candidate / "SKILL.md").is_file():
            return candidate
    raise RuntimeError("resolve_skill.py旁边或上一级目录没有SKILL.md")


def resolve_entrypoint(root: Path, name: str) -> Path:
    candidates = (root / "scripts" / name, root / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError(f"缺少正式入口：scripts/{name} 或 {name}")


def resolve_payload() -> dict[str, object]:
    root = skill_root()
    entrypoints = {Path(name).stem: str(resolve_entrypoint(root, name)) for name in ENTRYPOINTS}
    parents = {str(Path(path).parent) for path in entrypoints.values()}
    scripts_dir = str((root / "scripts").resolve())
    layout = "standard" if parents == {scripts_dir} else ("flat" if parents == {str(root)} else "mixed")
    return {
        "schema_version": 1,
        "status": "ready",
        "skill_root": str(root),
        "skill_file": str((root / "SKILL.md").resolve()),
        "layout": layout,
        "entrypoints": entrypoints,
    }


def main() -> int:
    try:
        payload = resolve_payload()
    except (OSError, RuntimeError) as exc:
        print(json.dumps({
            "schema_version": 1,
            "status": "stopped",
            "reason": str(exc),
            "next_action": "重新安装完整Skill；不要临时复制或改写评分脚本",
        }, ensure_ascii=False))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
