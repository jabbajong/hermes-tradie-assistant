from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tradie_assistant.db import ConflictError, NotFoundError, Store


RATE_CARD = {
    "labour_rate_cents_per_hour": 10_000,
    "minimum_charge_cents": 10_000,
    "callout_fee_cents": 0,
    "travel_rate_cents_per_km": 0,
    "included_travel_km": 0,
    "materials_markup_percent": 0,
    "after_hours_multiplier": 1.5,
    "rounding_increment_cents": 100,
    "prices_include_gst": False,
    "configured": True,
}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "assistant.sqlite3")
        self.a = self.store.setup_workspace("100", "Alpha Plumbing", gst_registered=True)
        self.b = self.store.setup_workspace("200", "Beta Electrical", gst_registered=False)

    def tearDown(self):
        self.directory.cleanup()

    def test_cross_tenant_lead_access_is_denied(self):
        lead, _ = self.store.create_lead("100", "Fix leaking tap")
        with self.assertRaises(NotFoundError):
            self.store.get_lead("200", lead["id"])

    def test_invited_staff_can_access_only_joined_workspace(self):
        lead, _ = self.store.create_lead("100", "Replace hot water unit")
        token = self.store.create_invite("100")
        joined = self.store.accept_invite("300", token)
        self.assertEqual(joined["id"], self.a["id"])
        self.assertEqual(self.store.get_lead("300", lead["id"])["id"], lead["id"])
        with self.assertRaises(NotFoundError):
            beta_lead, _ = self.store.create_lead("200", "Replace switch")
            self.store.get_lead("300", beta_lead["id"])

    def test_same_enquiry_is_deduplicated_across_workspace_staff(self):
        first, duplicate = self.store.create_lead("100", "Fix leaking tap")
        self.assertFalse(duplicate)
        token = self.store.create_invite("100")
        self.store.accept_invite("300", token)
        second, duplicate = self.store.create_lead("300", "  FIX   leaking TAP  ")
        self.assertTrue(duplicate)
        self.assertEqual(second["id"], first["id"])

    def test_optimistic_lead_update_rejects_stale_writer(self):
        lead, _ = self.store.create_lead("100", "Paint one room")
        self.store.update_lead(
            "100",
            lead["id"],
            expected_version=1,
            fields={"summary": "Paint room"},
            missing=[],
            status="ready",
            model_status="complete",
        )
        with self.assertRaises(ConflictError):
            self.store.update_lead(
                "100",
                lead["id"],
                expected_version=1,
                fields={"summary": "Stale edit"},
                missing=[],
                status="ready",
                model_status="complete",
            )

    def test_quote_approval_locks_exact_version(self):
        self.store.save_rate_card("100", RATE_CARD)
        lead, _ = self.store.create_lead("100", "Install tap")
        lead = self.store.update_lead(
            "100",
            lead["id"],
            expected_version=1,
            fields={"service_type": "plumbing", "estimated_hours": 1},
            missing=[],
            status="ready",
            model_status="complete",
        )
        calculation = {
            "subtotal_cents": 10_000,
            "gst_cents": 1_000,
            "total_cents": 11_000,
            "lines": [{"code": "labour", "description": "Labour", "amount_cents": 10_000}],
            "assumptions": ["Draft"],
        }
        quote = self.store.create_quote("100", lead["id"], calculation, expected_lead_version=lead["version"])
        with self.assertRaises(ConflictError):
            self.store.approve_quote("100", quote["id"], expected_version=99)
        approved = self.store.approve_quote("100", quote["id"], expected_version=quote["version"])
        self.assertEqual(approved["status"], "approved")

    def test_delete_scrubs_customer_content_but_keeps_audit(self):
        lead, _ = self.store.create_lead("100", "Customer at 1 Test Street")
        self.store.delete_lead("100", lead["id"])
        deleted = self.store.get_lead("100", lead["id"])
        self.assertEqual(deleted["status"], "deleted")
        self.assertEqual(deleted["source_text"], "")
        export = self.store.export_workspace("100")
        self.assertEqual(export["leads"][0]["fields"], {})


if __name__ == "__main__":
    unittest.main()
