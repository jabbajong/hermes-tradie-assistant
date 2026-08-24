from __future__ import annotations

import unittest

from tradie_assistant.pricing import PricingNotReady, calculate_quote


def rate_card(**overrides):
    value = {
        "labour_rate_cents_per_hour": 10_000,
        "minimum_charge_cents": 15_000,
        "callout_fee_cents": 5_000,
        "travel_rate_cents_per_km": 100,
        "included_travel_km": 10,
        "materials_markup_percent": 20,
        "after_hours_multiplier": 1.5,
        "rounding_increment_cents": 100,
        "prices_include_gst": False,
        "configured": True,
    }
    value.update(overrides)
    return value


class PricingTests(unittest.TestCase):
    def test_exclusive_gst_quote_is_deterministic(self):
        quote = calculate_quote(
            rate_card(),
            {"estimated_hours": 2, "travel_km": 15, "materials_cents": 10_000, "after_hours": False},
            gst_registered=True,
        )
        self.assertEqual(quote.subtotal_cents, 37_500)
        self.assertEqual(quote.gst_cents, 3_750)
        self.assertEqual(quote.total_cents, 41_250)
        self.assertEqual([line.code for line in quote.lines], ["callout", "labour", "travel", "materials"])

    def test_inclusive_gst_reports_component_without_adding_gst(self):
        quote = calculate_quote(
            rate_card(prices_include_gst=True),
            {"estimated_hours": 2, "travel_km": 15, "materials_cents": 10_000, "after_hours": False},
            gst_registered=True,
        )
        self.assertEqual(quote.total_cents, 37_500)
        self.assertEqual(quote.gst_cents, 3_409)
        self.assertEqual(quote.subtotal_cents, 34_091)

    def test_minimum_rounding_and_after_hours_are_applied(self):
        quote = calculate_quote(
            rate_card(callout_fee_cents=0, minimum_charge_cents=15_050, rounding_increment_cents=500),
            {"estimated_hours": 0.5, "travel_km": 0, "materials_cents": 0, "after_hours": True},
            gst_registered=False,
        )
        self.assertEqual(quote.total_cents, 15_500)
        self.assertIn("minimum", [line.code for line in quote.lines])
        self.assertIn("rounding", [line.code for line in quote.lines])

    def test_missing_hours_never_produces_a_price(self):
        with self.assertRaises(PricingNotReady):
            calculate_quote(rate_card(), {"estimated_hours": None}, gst_registered=True)

    def test_unconfigured_rate_card_is_rejected(self):
        with self.assertRaises(PricingNotReady):
            calculate_quote(rate_card(configured=False), {"estimated_hours": 1}, gst_registered=False)


if __name__ == "__main__":
    unittest.main()
