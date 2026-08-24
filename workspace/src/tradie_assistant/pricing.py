"""Deterministic Australian quote calculations.

The model may extract estimated quantities, but it never performs arithmetic or
selects hidden rates. Every monetary value comes from the versioned rate card
and the validated lead fields supplied to this module.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


class PricingNotReady(ValueError):
    """Raised when a quote cannot be calculated without inventing facts."""


@dataclass(frozen=True)
class QuoteLine:
    code: str
    description: str
    amount_cents: int


@dataclass(frozen=True)
class QuoteCalculation:
    lines: tuple[QuoteLine, ...]
    subtotal_cents: int
    gst_cents: int
    total_cents: int
    prices_include_gst: bool
    assumptions: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["lines"] = [asdict(line) for line in self.lines]
        result["assumptions"] = list(self.assumptions)
        return result


REQUIRED_RATE_FIELDS = {
    "labour_rate_cents_per_hour",
    "minimum_charge_cents",
    "callout_fee_cents",
    "travel_rate_cents_per_km",
    "included_travel_km",
    "materials_markup_percent",
    "after_hours_multiplier",
    "rounding_increment_cents",
    "prices_include_gst",
}


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise PricingNotReady(f"{name} must be a number")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PricingNotReady(f"{name} must be a whole number") from exc
    if parsed < 0:
        raise PricingNotReady(f"{name} cannot be negative")
    return parsed


def validate_rate_card(rate_card: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(REQUIRED_RATE_FIELDS.difference(rate_card))
    if missing:
        raise PricingNotReady("rate card is missing: " + ", ".join(missing))
    if not rate_card.get("configured", True):
        raise PricingNotReady("rate card setup is incomplete")

    result = dict(rate_card)
    for name in (
        "labour_rate_cents_per_hour",
        "minimum_charge_cents",
        "callout_fee_cents",
        "travel_rate_cents_per_km",
        "included_travel_km",
        "rounding_increment_cents",
    ):
        result[name] = _non_negative_int(result[name], name)
    if result["rounding_increment_cents"] <= 0:
        raise PricingNotReady("rounding increment must be positive")

    for name in ("materials_markup_percent", "after_hours_multiplier"):
        try:
            result[name] = float(result[name])
        except (TypeError, ValueError) as exc:
            raise PricingNotReady(f"{name} must be numeric") from exc
        if result[name] < 0:
            raise PricingNotReady(f"{name} cannot be negative")
    if result["after_hours_multiplier"] < 1:
        raise PricingNotReady("after-hours multiplier cannot be below 1")
    result["prices_include_gst"] = bool(result["prices_include_gst"])
    result["configured"] = True
    return result


def _money(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _round_up(value: int, increment: int) -> int:
    return int(math.ceil(value / increment) * increment) if value else 0


def calculate_quote(
    rate_card: dict[str, Any],
    lead: dict[str, Any],
    *,
    gst_registered: bool,
) -> QuoteCalculation:
    card = validate_rate_card(rate_card)

    estimated_hours = lead.get("estimated_hours")
    if estimated_hours is None:
        raise PricingNotReady("estimated labour hours are missing")
    try:
        hours = Decimal(str(estimated_hours))
    except Exception as exc:
        raise PricingNotReady("estimated labour hours are invalid") from exc
    if hours <= 0 or hours > Decimal("1000"):
        raise PricingNotReady("estimated labour hours must be between 0 and 1000")

    travel_km = Decimal(str(lead.get("travel_km", 0) or 0))
    materials_cents = _non_negative_int(lead.get("materials_cents", 0) or 0, "materials cost")
    if travel_km < 0 or travel_km > Decimal("5000"):
        raise PricingNotReady("travel distance is outside the supported range")

    multiplier = Decimal(str(card["after_hours_multiplier"] if lead.get("after_hours") else 1))
    labour = _money(hours * Decimal(card["labour_rate_cents_per_hour"]) * multiplier)
    callout = card["callout_fee_cents"]
    chargeable_km = max(Decimal("0"), travel_km - Decimal(card["included_travel_km"]))
    travel = _money(chargeable_km * Decimal(card["travel_rate_cents_per_km"]))
    materials = _money(
        Decimal(materials_cents) * (Decimal("1") + Decimal(str(card["materials_markup_percent"])) / Decimal("100"))
    )

    lines: list[QuoteLine] = []
    if callout:
        lines.append(QuoteLine("callout", "Call-out", callout))
    lines.append(QuoteLine("labour", f"Labour ({hours.normalize()} hours)", labour))
    if travel:
        lines.append(QuoteLine("travel", f"Travel ({chargeable_km.normalize()} chargeable km)", travel))
    if materials:
        lines.append(QuoteLine("materials", "Materials including configured markup", materials))

    raw = sum(line.amount_cents for line in lines)
    if raw < card["minimum_charge_cents"]:
        adjustment = card["minimum_charge_cents"] - raw
        lines.append(QuoteLine("minimum", "Minimum-charge adjustment", adjustment))
        raw += adjustment

    rounded = _round_up(raw, card["rounding_increment_cents"])
    if rounded > raw:
        lines.append(QuoteLine("rounding", "Quote rounding", rounded - raw))
    raw = rounded

    if gst_registered and card["prices_include_gst"]:
        total = raw
        gst = _money(Decimal(total) / Decimal("11"))
        subtotal = total - gst
    elif gst_registered:
        subtotal = raw
        gst = _money(Decimal(subtotal) * Decimal("0.10"))
        total = subtotal + gst
    else:
        subtotal = raw
        gst = 0
        total = raw

    assumptions = [
        "Pricing uses the saved rate card and the lead quantities shown above.",
        "The draft does not confirm availability, site conditions or final material quantities.",
    ]
    if lead.get("after_hours"):
        assumptions.append("The configured after-hours multiplier was applied.")
    return QuoteCalculation(
        lines=tuple(lines),
        subtotal_cents=subtotal,
        gst_cents=gst,
        total_cents=total,
        prices_include_gst=bool(card["prices_include_gst"]),
        assumptions=tuple(assumptions),
    )
