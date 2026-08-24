#!/usr/bin/env python3
"""Verify the configured model has the required OpenRouter ZDR route."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from tradie_assistant.provider import ProviderError, preflight_openrouter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    require_image = True
    if args.config:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        require_image = bool(config.get("require_image_input", True))
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    model = os.environ.get("OPENROUTER_MODEL", "").strip()
    if not api_key:
        print("preflight failed: OPENROUTER_API_KEY is missing")
        return 2
    try:
        result = preflight_openrouter(api_key, model, require_image=require_image)
    except ProviderError as exc:
        print(f"preflight failed: {exc}")
        return 1
    print(
        f"preflight passed: model={result['model']} modalities={','.join(result['input_modalities'])} "
        f"zdr_endpoints={result['zdr_provider_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
