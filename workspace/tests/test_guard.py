from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "openrouter_guard.py"
SPEC = importlib.util.spec_from_file_location("tradie_openrouter_guard", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class GuardTests(unittest.TestCase):
    def config(self, **overrides):
        values = {
            "upstream_base_url": "https://openrouter.ai/api/v1",
            "listen_host": "127.0.0.1",
            "listen_port": 18082,
            "requests_per_minute": 3,
            "daily_provider_calls": 2,
            "max_output_tokens_per_call": 100,
            "daily_reserved_output_tokens": 150,
            "max_request_bytes": 1000,
            "request_timeout_seconds": 10,
            "require_image_input": True,
        }
        values.update(overrides)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(values, handle)
            path = Path(handle.name)
        return MODULE.GuardConfig.load(path)

    def test_guard_overwrites_model_and_provider_policy(self):
        protected, reserve = MODULE.enforce_privacy_payload(
            {
                "model": "wrong/model",
                "messages": [],
                "provider": {"zdr": False, "allow_fallbacks": True},
                "max_tokens": 500,
            },
            "approved/model",
            100,
        )
        self.assertEqual(protected["model"], "approved/model")
        self.assertEqual(protected["provider"]["zdr"], True)
        self.assertEqual(protected["provider"]["data_collection"], "deny")
        self.assertEqual(protected["provider"]["allow_fallbacks"], False)
        self.assertEqual(protected["max_tokens"], 100)
        self.assertEqual(reserve, 100)

    def test_model_fallback_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            MODULE.enforce_privacy_payload({"messages": [], "models": ["other/model"]}, "approved/model", 100)

    def test_guard_requires_exact_local_token(self):
        token = "a" * 32
        self.assertTrue(MODULE.local_token_matches(f"Bearer {token}", token))
        self.assertFalse(MODULE.local_token_matches("Bearer wrong", token))
        self.assertFalse(MODULE.local_token_matches(token, token))

    def test_budget_is_atomic_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = MODULE.BudgetLedger(Path(directory) / "budget.json", self.config())
            self.assertTrue(ledger.reserve(50)[0])
            self.assertTrue(ledger.reserve(50)[0])
            allowed, reason, _ = ledger.reserve(1)
            self.assertFalse(allowed)
            self.assertIn("daily provider-call", reason)

    def test_guard_refuses_non_openrouter_upstream(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump({"upstream_base_url": "https://example.com/api/v1"}, handle)
            path = Path(handle.name)
        with self.assertRaises(ValueError):
            MODULE.GuardConfig.load(path)

    def test_guard_refuses_non_loopback_listener(self):
        with self.assertRaises(ValueError):
            self.config(listen_host="0.0.0.0")


if __name__ == "__main__":
    unittest.main()
