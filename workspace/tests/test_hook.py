from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


HANDLER = Path(__file__).resolve().parents[2] / "hooks" / "tradie-commands" / "handler.py"
SPEC = importlib.util.spec_from_file_location("tradie_commands_hook", HANDLER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class HookTests(unittest.TestCase):
    def test_session_identity_and_setup_command_are_tenant_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "assistant.sqlite3")
            environment = {
                "TRADIE_ASSISTANT_DB": database,
                "OPENROUTER_API_KEY": "",
                "OPENROUTER_MODEL": "",
            }
            with patch.dict(os.environ, environment, clear=False):
                asyncio.run(
                    MODULE.handle(
                        "session:start",
                        {"platform": "telegram", "user_id": "100", "session_id": "session-1", "chat_id": "100"},
                    )
                )
                result = asyncio.run(
                    MODULE.handle(
                        "command:setup",
                        {
                            "platform": "telegram",
                            "user_id": "100",
                            "session_id": "session-1",
                            "command": "setup",
                            "args": "Alpha Plumbing | gst=yes",
                        },
                    )
                )
            self.assertEqual(result["decision"], "handled")
            self.assertIn("Workspace created", result["message"])

    def test_non_telegram_event_is_ignored(self):
        result = asyncio.run(MODULE.handle("command:help", {"platform": "discord", "user_id": "100"}))
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
