from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tradie_assistant.db import Store
from tradie_assistant.provider import ProviderError
from tradie_assistant.service import TradieAssistantService


RATE_CARD = {
    "labour_rate_cents_per_hour": 10_000,
    "minimum_charge_cents": 10_000,
    "callout_fee_cents": 5_000,
    "travel_rate_cents_per_km": 100,
    "included_travel_km": 10,
    "materials_markup_percent": 10,
    "after_hours_multiplier": 1.5,
    "rounding_increment_cents": 100,
    "prices_include_gst": False,
    "configured": True,
}


class FakeProvider:
    def __init__(self, response=None, fail=False):
        self.response = response or {}
        self.fail = fail

    def extract_lead(self, _text, _images):
        if self.fail:
            raise ProviderError("failed")
        return self.response


def complete(**overrides):
    value = {
        "customer_name": "Test Customer",
        "service_type": "plumbing",
        "summary": "Replace leaking tap",
        "suburb": "Perth",
        "urgency": "routine",
        "estimated_hours": 2,
        "travel_km": 15,
        "materials_cents": 10_000,
        "after_hours": False,
        "hazards": [],
        "missing_information": [],
        "multiple_jobs": False,
        "image_notes": [],
    }
    value.update(overrides)
    return value


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "assistant.sqlite3")
        self.store.setup_workspace("100", "Alpha Plumbing", gst_registered=True)
        self.store.save_rate_card("100", RATE_CARD)

    def tearDown(self):
        self.directory.cleanup()

    def test_complete_lead_is_saved_then_quoted(self):
        service = TradieAssistantService(self.store, provider=FakeProvider(complete()))
        result = service.ingest("100", "Tap is leaking in Perth")
        self.assertEqual(result["lead"]["model_status"], "complete")
        self.assertIsNotNone(result["quote"])
        self.assertEqual(result["quote"]["total_cents"], 40_150)

    def test_provider_failure_retains_needs_review_lead(self):
        service = TradieAssistantService(self.store, provider=FakeProvider(fail=True))
        result = service.ingest("100", "Tap is leaking")
        self.assertEqual(result["lead"]["status"], "needs_review")
        self.assertEqual(result["lead"]["model_status"], "failed")
        self.assertIn("Tap is leaking", result["lead"]["fields"]["description"])
        self.assertIsNone(result["quote"])

    def test_immediate_hazard_blocks_quote(self):
        service = TradieAssistantService(self.store, provider=FakeProvider(complete()))
        result = service.ingest("100", "There is a gas leak near the hot water system")
        self.assertEqual(result["lead"]["status"], "manual_required")
        self.assertIsNone(result["quote"])

    def test_multiple_jobs_block_quote(self):
        service = TradieAssistantService(self.store, provider=FakeProvider(complete(multiple_jobs=True)))
        result = service.ingest("100", "Fix tap and rewire shed")
        self.assertEqual(result["lead"]["status"], "needs_review")
        self.assertIn("split the enquiry into separate jobs", result["lead"]["missing"])
        self.assertIsNone(result["quote"])

    def test_duplicate_provider_event_does_not_create_second_quote(self):
        provider = FakeProvider(complete())
        service = TradieAssistantService(self.store, provider=provider)
        first = service.ingest("100", "Replace leaking tap")
        second = service.ingest("100", "  replace LEAKING tap ")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["quote"]["id"], first["quote"]["id"])

    def test_deleting_lead_removes_private_image(self):
        root = Path(self.directory.name)
        inbox = root / "inbox"
        inbox.mkdir()
        source = inbox / "job.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"private-job-image")
        service = TradieAssistantService(
            self.store,
            media_root=root / "media",
            inbox_root=inbox,
        )
        result = service.ingest("100", "Private photo evidence", image_paths=[source])
        evidence_path = Path(self.store.evidence_paths("100", result["lead"]["id"])[0])
        self.assertTrue(evidence_path.exists())
        self.assertEqual(service.delete_lead("100", result["lead"]["id"]), 1)
        self.assertFalse(evidence_path.exists())
        self.assertEqual(self.store.evidence_paths("100", result["lead"]["id"]), [])


if __name__ == "__main__":
    unittest.main()
