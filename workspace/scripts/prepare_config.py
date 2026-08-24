#!/usr/bin/env python3
"""Materialise the Hermes config from an exact environment-selected model."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path


MODEL_ID = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.:-]+$")
PLACEHOLDER = "__OPENROUTER_MODEL__"


def render(template: str, model: str, allowed_users: str = "") -> str:
    if not MODEL_ID.fullmatch(model) or model == "openrouter/auto" or model.startswith("~"):
        raise ValueError("OPENROUTER_MODEL must be an exact author/model slug")
    if template.count(PLACEHOLDER) != 1:
        raise ValueError("config template must contain exactly one model placeholder")
    rendered = template.replace(PLACEHOLDER, model)
    users = [value.strip() for value in allowed_users.split(",") if value.strip()]
    if users:
        marker = "    allow_from: []"
        replacement = "    allow_from:\n" + "\n".join(f"      - '{value}'" for value in users)
        if marker not in rendered:
            raise ValueError("config template is missing the Telegram allowlist marker")
        rendered = rendered.replace(marker, replacement, 1)
    return rendered


def atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    model = os.environ.get("OPENROUTER_MODEL", "").strip()
    allowed_users = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    content = render(args.template.read_text(encoding="utf-8"), model, allowed_users)
    atomic_write(args.output, content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
