from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_config.py"
SPEC = importlib.util.spec_from_file_location("tradie_prepare_config", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ConfigTests(unittest.TestCase):
    def test_exact_model_and_allowlist_are_materialised(self):
        template = "default: __OPENROUTER_MODEL__\nplatforms:\n  telegram:\n    allow_from: []\n"
        rendered = MODULE.render(template, "example/model", "123,456")
        self.assertIn("default: example/model", rendered)
        self.assertIn("- '123'", rendered)
        self.assertIn("- '456'", rendered)
        self.assertNotIn("__OPENROUTER_MODEL__", rendered)

    def test_auto_route_is_rejected(self):
        with self.assertRaises(ValueError):
            MODULE.render("default: __OPENROUTER_MODEL__", "openrouter/auto")


if __name__ == "__main__":
    unittest.main()
