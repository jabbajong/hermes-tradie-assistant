#!/usr/bin/env python3
"""Local operator CLI for the tenant-aware Tradie Assistant commands."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from tradie_assistant.commands import dispatch
from tradie_assistant.db import Store
from tradie_assistant.runtime import database_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command")
    parser.add_argument("args", nargs="*", default=[])
    parser.add_argument("--telegram-user-id", default="")
    parser.add_argument("--image", action="append", default=[], type=Path)
    parsed = parser.parse_args()
    store = Store(database_path())
    telegram_user_id = parsed.telegram_user_id.strip()
    if not telegram_user_id:
        session_id = os.environ.get("HERMES_SESSION_ID", "").strip()
        telegram_user_id = store.telegram_user_for_session(session_id)
    print(
        dispatch(
            parsed.command,
            " ".join(parsed.args),
            telegram_user_id,
            store=store,
            image_paths=parsed.image,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
