"""Runtime paths and atomic helpers for the Tradie Assistant profile."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def workspace_root() -> Path:
    configured = os.environ.get("TRADIE_ASSISTANT_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def database_path() -> Path:
    configured = os.environ.get("TRADIE_ASSISTANT_DB", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return workspace_root() / "state" / "tradie-assistant.sqlite3"


def media_root() -> Path:
    configured = os.environ.get("TRADIE_ASSISTANT_MEDIA_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return workspace_root() / "state" / "media"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def atomic_write_text(path: Path, value: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
        try:
            os.chmod(temporary, mode)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n", mode=mode)
