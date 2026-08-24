"""Hash the tested profile source without including runtime state or secrets."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path


INCLUDE_ROOTS = (
    ".env.example",
    ".gitignore",
    ".openrouter-guard.env.example",
    "README.md",
    "SOUL.md",
    "config.template.yaml",
    "container",
    "hooks",
    "profile-skills",
    "systemd",
    "workspace/config",
    "workspace/scripts",
    "workspace/src",
    "workspace/tests",
    "workspace/pyproject.toml",
)
EXCLUDED_PARTS = {"state", "inbox", "backups", "__pycache__", ".git"}


def source_files(profile_root: Path) -> list[Path]:
    files: list[Path] = []
    for relative in INCLUDE_ROOTS:
        target = profile_root / relative
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(
                path for path in target.rglob("*") if path.is_file() and not EXCLUDED_PARTS.intersection(path.parts)
            )
    return sorted(set(files))


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def build_manifest(profile_root: Path, *, tests: str) -> dict:
    return {
        "manifest_version": 1,
        "profile": "tradie-assistant",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "tests": tests,
        "files": [
            {"path": path.relative_to(profile_root).as_posix(), "sha256": digest(path)}
            for path in source_files(profile_root)
        ],
    }
