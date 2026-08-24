from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tradie_assistant.commands import dispatch
from tradie_assistant.db import Store


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "assistant.sqlite3")

    def tearDown(self):
        self.directory.cleanup()

    def test_guided_setup_and_rate_card_create_versions(self):
        setup = dispatch("setup", "Alpha Plumbing | gst=yes", "100", store=self.store)
        self.assertIn("Workspace created", setup)
        args = "ratecard labour=120 callout=90 minimum=150 travel=1.20 included_km=10 markup=20 after_hours=1.5 rounding=1 inclusive_gst=no"
        first = dispatch("revise", args, "100", store=self.store)
        second = dispatch("revise", args.replace("labour=120", "labour=130"), "100", store=self.store)
        self.assertIn("version 1", first)
        self.assertIn("version 2", second)

    def test_new_lead_is_retained_when_provider_is_not_configured(self):
        dispatch("setup", "Alpha Plumbing | gst=no", "100", store=self.store)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "OPENROUTER_MODEL": ""}, clear=False):
            result = dispatch("new", "Customer needs a leaking tap fixed", "100", store=self.store)
        self.assertIn("Lead saved", result)
        self.assertIn("retained for manual review", result)
        self.assertEqual(len(self.store.list_leads("100")), 1)

    def test_owner_export_and_delete_commands_work(self):
        dispatch("setup", "Alpha Plumbing | gst=no", "100", store=self.store)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "OPENROUTER_MODEL": ""}, clear=False):
            dispatch("new", "Private customer details", "100", store=self.store)
        lead = self.store.list_leads("100")[0]
        self.assertIn("removed", dispatch("delete", f"lead {lead['id']}", "100", store=self.store))

    def test_manual_lead_revision_can_make_saved_lead_quote_ready(self):
        dispatch("setup", "Alpha Plumbing | gst=no", "100", store=self.store)
        rate_args = "ratecard labour=120 callout=90 minimum=150 travel=1.20 included_km=10 markup=20 after_hours=1.5 rounding=1 inclusive_gst=no"
        dispatch("revise", rate_args, "100", store=self.store)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "OPENROUTER_MODEL": ""}, clear=False):
            dispatch("new", "Customer needs a leaking tap fixed", "100", store=self.store)
        lead = self.store.list_leads("100")[0]
        revised = dispatch(
            "revise",
            f"lead {lead['id']} hours=2 travel_km=12 materials=80 after_hours=no service=plumbing",
            "100",
            store=self.store,
        )
        self.assertIn("Ready for /quote", revised)
        quoted = dispatch("quote", lead["id"], "100", store=self.store)
        self.assertIn("Quote quote_", quoted)


if __name__ == "__main__":
    unittest.main()
