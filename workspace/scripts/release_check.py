#!/usr/bin/env python3
"""Build or print a tested Tradie Assistant release manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from tradie_assistant.release import build_manifest
from tradie_assistant.runtime import atomic_write_json, workspace_root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=workspace_root().parent)
    parser.add_argument("--write", type=Path)
    parser.add_argument("--tests", default="not-run")
    args = parser.parse_args()
    manifest = build_manifest(args.root.resolve(), tests=args.tests)
    if args.write:
        atomic_write_json(args.write.resolve(), manifest)
    else:
        print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
