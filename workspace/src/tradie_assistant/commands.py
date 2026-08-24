"""Tenant-aware Telegram command dispatcher."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .db import AccessDenied, ConflictError, NotFoundError, Store
from .pricing import PricingNotReady
from .provider import OpenRouterClient, OpenRouterConfig, ProviderError
from .runtime import atomic_write_json, database_path, media_root, utc_now, workspace_root
from .service import TradieAssistantService


HELP = """Tradie Assistant

- /setup Business Name | gst=yes
- /setup invite=INVITE_TOKEN
- /revise ratecard labour=120 callout=90 minimum=150 travel=1.20 included_km=10 markup=20 after_hours=1.5 rounding=1 inclusive_gst=no
- /revise lead ID hours=2 travel_km=15 materials=80 after_hours=no service=plumbing
- /new paste or forward the job enquiry here
- /lead — recent leads; /lead ID — one lead
- /quote LEAD_ID — prepare or display a draft
- /approve QUOTE_ID v=N — lock the reviewed version
- /invite staff — owner creates a 24-hour invite
- /switch WORKSPACE_SLUG
- /export — owner creates a private JSON export
- /delete lead LEAD_ID — owner removes customer content

Quotes stay inside this private bot. Approval never sends or books anything."""


def _money(value: str) -> int:
    try:
        amount = float(value)
    except ValueError as exc:
        raise ValueError(f"invalid dollar amount: {value}") from exc
    if amount < 0:
        raise ValueError("money values cannot be negative")
    return int(round(amount * 100))


def _bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"yes", "true", "1", "registered", "y"}:
        return True
    if normalized in {"no", "false", "0", "not-registered", "n"}:
        return False
    raise ValueError(f"expected yes or no, got: {value}")


def _pairs(value: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for token in value.split():
        if "=" not in token:
            continue
        key, item = token.split("=", 1)
        pairs[key.strip().casefold()] = item.strip()
    return pairs


def _format_money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _lead_line(lead: dict[str, Any]) -> str:
    fields = lead["fields"]
    label = fields.get("summary") or fields.get("description") or "Untitled enquiry"
    return f"{lead['id'][:13]} — {lead['status']} — {str(label).strip()[:80]}"


def _quote_text(quote: dict[str, Any]) -> str:
    lines = [f"Quote {quote['id'][:14]} v={quote['version']} — {quote['status']}"]
    for item in quote["lines"]:
        lines.append(f"- {item['description']}: {_format_money(int(item['amount_cents']))}")
    lines.extend(
        [
            f"Subtotal: {_format_money(int(quote['subtotal_cents']))}",
            f"GST: {_format_money(int(quote['gst_cents']))}",
            f"Total: {_format_money(int(quote['total_cents']))}",
            "",
            "Assumptions:",
            *[f"- {item}" for item in quote["assumptions"]],
            "",
            f"Review it, then use /approve {quote['id'][:14]} v={quote['version']}.",
            "Approval does not send this quote to the customer.",
        ]
    )
    return "\n".join(lines)


def _service(store: Store) -> TradieAssistantService:
    provider = None
    try:
        provider = OpenRouterClient(OpenRouterConfig.from_env())
    except ProviderError:
        pass
    inbox = workspace_root() / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    return TradieAssistantService(
        store,
        provider=provider,
        media_root=media_root(),
        inbox_root=inbox,
        max_media_bytes=int(os.environ.get("TRADIE_ASSISTANT_MEDIA_MAX_BYTES", 10 * 1024 * 1024)),
        media_retention_days=int(os.environ.get("TRADIE_ASSISTANT_MEDIA_RETENTION_DAYS", 30)),
    )


def dispatch(
    command: str,
    args: str,
    telegram_user_id: str,
    *,
    store: Store | None = None,
    image_paths: list[Path] | None = None,
) -> str:
    command = command.strip().casefold().lstrip("/")
    args = str(args or "").strip()
    store = store or Store(database_path())
    service = _service(store)
    try:
        if command == "help":
            return HELP

        if command == "setup":
            if args.startswith("invite="):
                workspace = store.accept_invite(telegram_user_id, args.split("=", 1)[1])
                return f"Joined {workspace['name']}. It is now your active workspace."
            parts = [part.strip() for part in args.split("|") if part.strip()]
            if not parts:
                return "Usage: /setup Business Name | gst=yes"
            options = _pairs(" ".join(parts[1:]))
            if "gst" not in options:
                return "Include the GST setting: /setup Business Name | gst=yes (or gst=no)"
            workspace = store.setup_workspace(telegram_user_id, parts[0], gst_registered=_bool(options["gst"]))
            return (
                f"Workspace created: {workspace['name']} ({workspace['slug']}).\n"
                "Next, save the rate card with /revise ratecard ..."
            )

        if command == "revise":
            if args.casefold().startswith("lead "):
                parts = args.split()
                if len(parts) < 3:
                    return "Usage: /revise lead LEAD_ID hours=2 travel_km=15 materials=80 after_hours=no service=plumbing"
                lead = store.get_lead(telegram_user_id, parts[1])
                values = _pairs(" ".join(parts[2:]))
                fields = dict(lead["fields"])
                if "hours" in values:
                    fields["estimated_hours"] = float(values["hours"])
                if "travel_km" in values:
                    fields["travel_km"] = float(values["travel_km"])
                if "materials" in values:
                    fields["materials_cents"] = _money(values["materials"])
                if "after_hours" in values:
                    fields["after_hours"] = _bool(values["after_hours"])
                if "service" in values:
                    fields["service_type"] = values["service"].replace("_", " ")[:200]
                if "suburb" in values:
                    fields["suburb"] = values["suburb"].replace("_", " ")[:200]
                missing = []
                for field, label in (
                    ("service_type", "service type"),
                    ("estimated_hours", "estimated labour hours"),
                    ("materials_cents", "confirmed material cost"),
                    ("after_hours", "whether the job is after-hours"),
                ):
                    if fields.get(field) is None:
                        missing.append(label)
                status = "manual_required" if lead["status"] == "manual_required" else ("needs_review" if missing else "ready")
                updated = store.update_lead(
                    telegram_user_id,
                    lead["id"],
                    expected_version=lead["version"],
                    fields=fields,
                    missing=missing,
                    status=status,
                    model_status=lead["model_status"],
                )
                suffix = "Needs: " + "; ".join(missing) if missing else "Ready for /quote."
                return "Lead revised.\n" + _lead_line(updated) + "\n" + suffix
            if not args.casefold().startswith("ratecard "):
                return "Usage: /revise ratecard ... or /revise lead LEAD_ID hours=2 travel_km=15 materials=80 after_hours=no service=plumbing"
            values = _pairs(args.split(" ", 1)[1])
            required = {"labour", "callout", "minimum", "travel", "included_km", "markup", "after_hours", "rounding", "inclusive_gst"}
            missing = sorted(required.difference(values))
            if missing:
                return "Rate card is missing: " + ", ".join(missing)
            config = {
                "labour_rate_cents_per_hour": _money(values["labour"]),
                "callout_fee_cents": _money(values["callout"]),
                "minimum_charge_cents": _money(values["minimum"]),
                "travel_rate_cents_per_km": _money(values["travel"]),
                "included_travel_km": int(values["included_km"]),
                "materials_markup_percent": float(values["markup"]),
                "after_hours_multiplier": float(values["after_hours"]),
                "rounding_increment_cents": _money(values["rounding"]),
                "prices_include_gst": _bool(values["inclusive_gst"]),
                "configured": True,
            }
            saved = service.save_rate_card(telegram_user_id, config)
            return f"Rate card version {saved['version']} saved. Existing quote versions were not changed."

        if command == "new":
            if not args:
                return "Usage: /new paste or forward the complete enquiry after the command"
            result = service.ingest(telegram_user_id, args, image_paths=image_paths)
            lead = result["lead"]
            lines = ["Existing lead matched." if result["duplicate"] else "Lead saved.", _lead_line(lead)]
            if lead["status"] == "manual_required":
                lines.append("Immediate safety review is required. No quote was calculated.")
            if lead["missing"]:
                lines.append("Needs: " + "; ".join(lead["missing"]))
            if result.get("quote"):
                lines.extend(["", _quote_text(result["quote"])])
            elif lead["model_status"] in {"failed", "unavailable"}:
                lines.append("The enquiry is retained for manual review; no price was invented.")
            return "\n".join(lines)

        if command == "lead":
            if not args:
                leads = store.list_leads(telegram_user_id)
                return "Recent leads:\n" + ("\n".join(f"- {_lead_line(item)}" for item in leads) if leads else "- None yet")
            lead = store.get_lead(telegram_user_id, args)
            fields = lead["fields"]
            lines = [
                _lead_line(lead),
                f"Service: {fields.get('service_type') or 'not confirmed'}",
                f"Suburb: {fields.get('suburb') or 'not confirmed'}",
                f"Urgency: {fields.get('urgency') or 'unknown'}",
                f"Estimated hours: {fields.get('estimated_hours') if fields.get('estimated_hours') is not None else 'not confirmed'}",
            ]
            if lead["missing"]:
                lines.append("Needs: " + "; ".join(lead["missing"]))
            return "\n".join(lines)

        if command == "quote":
            if not args:
                return "Usage: /quote LEAD_ID"
            existing = store.latest_quote(telegram_user_id, args)
            if existing and existing["status"] in {"draft", "approved"}:
                return _quote_text(existing)
            return _quote_text(service.prepare_quote(telegram_user_id, args))

        if command == "approve":
            values = _pairs(args)
            reference = args.split()[0] if args else ""
            if not reference or "v" not in values:
                return "Usage: /approve QUOTE_ID v=N"
            quote = store.approve_quote(telegram_user_id, reference, expected_version=int(values["v"]))
            return _quote_text(quote).replace("Review it, then use", "Approved. The reviewed version is locked. Command was")

        if command == "invite":
            if args.casefold() != "staff":
                return "Usage: /invite staff"
            token = store.create_invite(telegram_user_id)
            return f"Staff invite (expires in 24 hours):\n/setup invite={token}\nSend it privately to the intended staff member."

        if command == "switch":
            if not args:
                return "Usage: /switch WORKSPACE_SLUG"
            workspace = store.switch_workspace(telegram_user_id, args)
            return f"Active workspace: {workspace['name']} ({workspace['slug']})"

        if command == "export":
            export = store.export_workspace(telegram_user_id)
            destination = workspace_root() / "state" / "exports" / f"{export['workspace']['slug']}-{utc_now()[:10]}.json"
            atomic_write_json(destination, export)
            return f"Private export prepared: {destination.name}"

        if command == "delete":
            match = re.fullmatch(r"lead\s+(\S+)", args, flags=re.IGNORECASE)
            if not match:
                return "Usage: /delete lead LEAD_ID"
            service.delete_lead(telegram_user_id, match.group(1))
            return "Lead customer content removed. The audit event was retained."

        return HELP
    except (AccessDenied, ConflictError, NotFoundError, PricingNotReady, ValueError) as exc:
        return str(exc)
