from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tradie_assistant.release import build_manifest


class ReleaseTests(unittest.TestCase):
    def test_manifest_excludes_state_and_secret_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "hermes-tradie-assistant"
            (root / "workspace" / "src").mkdir(parents=True)
            (root / "workspace" / "state").mkdir()
            (root / "workspace" / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "workspace" / "state" / "data.sqlite3").write_text("private", encoding="utf-8")
            (root / ".env.example").write_text("OPENROUTER_API_KEY=\n", encoding="utf-8")
            (root / ".env").write_text("OPENROUTER_API_KEY=secret\n", encoding="utf-8")
            manifest = build_manifest(root, tests="passed")
            paths = {item["path"] for item in manifest["files"]}
            self.assertIn("workspace/src/app.py", paths)
            self.assertIn(".env.example", paths)
            self.assertNotIn("workspace/state/data.sqlite3", paths)
            self.assertNotIn(".env", paths)


if __name__ == "__main__":
    unittest.main()
