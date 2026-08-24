"""Saved-before-AI lead intake and deterministic quote orchestration."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .db import Store
from .media import StoredImage, image_data_url, purge_expired_images, store_image
from .pricing import PricingNotReady, calculate_quote, validate_rate_card
from .provider import OpenRouterClient, ProviderError


IMMEDIATE_HAZARDS = (
    "gas leak",
    "smell gas",
    "live wire",
    "electric shock",
    "electrical fire",
    "asbestos",
    "structural collapse",
    "collapsed roof",
    "house fire",
    "major flooding",
    "sewage overflow",
)

REQUIRED_FOR_AUTO_QUOTE = ("service_type", "estimated_hours")


def _clean_extraction(value: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    allowed = {
        "customer_name",
        "service_type",
        "summary",
        "suburb",
        "urgency",
        "estimated_hours",
        "travel_km",
        "materials_cents",
        "after_hours",
        "hazards",
        "multiple_jobs",
        "image_notes",
    }
    fields = {key: value.get(key) for key in allowed}
    fields["summary"] = str(fields.get("summary") or "").strip()[:1000]
    for key in ("customer_name", "service_type", "suburb", "urgency"):
        if fields.get(key) is not None:
            fields[key] = str(fields[key]).strip()[:200] or None
    for key in ("hazards", "image_notes"):
        raw = fields.get(key)
        fields[key] = [str(item).strip()[:200] for item in raw[:20] if str(item).strip()] if isinstance(raw, list) else []
    fields["multiple_jobs"] = bool(fields.get("multiple_jobs"))
    if fields.get("after_hours") is not None:
        fields["after_hours"] = bool(fields["after_hours"])

    missing = value.get("missing_information")
    missing_list = [str(item).strip()[:200] for item in missing[:20] if str(item).strip()] if isinstance(missing, list) else []
    for key in REQUIRED_FOR_AUTO_QUOTE:
        if fields.get(key) is None and key not in missing_list:
            missing_list.append(key.replace("_", " "))
    if fields.get("travel_km") is None:
        fields["travel_km"] = 0
    if fields.get("materials_cents") is None:
        missing_list.append("confirmed material cost")
    if fields.get("after_hours") is None:
        missing_list.append("whether the job is after-hours")
    return fields, sorted(set(missing_list))


def _has_immediate_hazard(source_text: str, fields: dict[str, Any]) -> bool:
    combined = " ".join([source_text, *fields.get("hazards", [])]).casefold()
    return any(term in combined for term in IMMEDIATE_HAZARDS) or fields.get("urgency") == "emergency"


class TradieAssistantService:
    def __init__(
        self,
        store: Store,
        *,
        provider: OpenRouterClient | None = None,
        media_root: Path | None = None,
        inbox_root: Path | None = None,
        max_media_bytes: int = 10 * 1024 * 1024,
        media_retention_days: int = 30,
    ):
        self.store = store
        self.provider = provider
        self.media_root = media_root
        self.inbox_root = inbox_root
        self.max_media_bytes = max_media_bytes
        if media_retention_days < 1:
            raise ValueError("media retention must be at least one day")
        self.media_retention_days = media_retention_days

    def ingest(
        self,
        telegram_user_id: str,
        source_text: str,
        *,
        update_key: str = "",
        image_paths: list[Path] | None = None,
    ) -> dict[str, Any]:
        lead, duplicate = self.store.create_lead(telegram_user_id, source_text, update_key=update_key)
        if duplicate:
            return {"lead": lead, "duplicate": True, "quote": self.store.latest_quote(telegram_user_id, lead["id"])}

        stored_images: list[StoredImage] = []
        if image_paths:
            if not self.media_root or not self.inbox_root:
                raise ValueError("private image storage is not configured")
            workspace = self.store.active_workspace(telegram_user_id)
            purge_expired_images(
                self.media_root / workspace["id"],
                retention_days=self.media_retention_days,
            )
            for image_path in image_paths:
                stored = store_image(
                    image_path,
                    self.media_root,
                    workspace_id=workspace["id"],
                    lead_id=lead["id"],
                    allowed_source_root=self.inbox_root,
                    max_bytes=self.max_media_bytes,
                )
                stored_images.append(stored)
                self.store.add_evidence(
                    telegram_user_id,
                    lead["id"],
                    {
                        "kind": "image",
                        "private_path": str(stored.path),
                        "sha256": stored.sha256,
                        "media_type": stored.media_type,
                        "size_bytes": stored.size_bytes,
                    },
                )

        if not self.provider:
            updated = self.store.update_lead(
                telegram_user_id,
                lead["id"],
                expected_version=lead["version"],
                fields={"description": source_text},
                missing=["provider extraction is unavailable"],
                status="needs_review",
                model_status="unavailable",
            )
            return {"lead": updated, "duplicate": False, "quote": None}

        try:
            image_urls = [image_data_url(item.path, item.media_type, max_bytes=self.max_media_bytes) for item in stored_images]
            extraction = self.provider.extract_lead(source_text, image_urls)
            fields, missing = _clean_extraction(extraction)
        except ProviderError:
            updated = self.store.update_lead(
                telegram_user_id,
                lead["id"],
                expected_version=lead["version"],
                fields={"description": source_text},
                missing=["provider processing failed; review the saved lead manually"],
                status="needs_review",
                model_status="failed",
            )
            return {"lead": updated, "duplicate": False, "quote": None}

        if _has_immediate_hazard(source_text, fields):
            status = "manual_required"
            if "immediate safety review" not in missing:
                missing.append("immediate safety review")
        elif fields.get("multiple_jobs"):
            status = "needs_review"
            missing.append("split the enquiry into separate jobs")
        elif missing:
            status = "needs_review"
        else:
            status = "ready"
        updated = self.store.update_lead(
            telegram_user_id,
            lead["id"],
            expected_version=lead["version"],
            fields=fields,
            missing=sorted(set(missing)),
            status=status,
            model_status="complete",
        )
        quote = None
        if status == "ready":
            try:
                quote = self.prepare_quote(telegram_user_id, updated["id"])
            except PricingNotReady:
                pass
        return {"lead": updated, "duplicate": False, "quote": quote}

    def prepare_quote(self, telegram_user_id: str, lead_reference: str) -> dict[str, Any]:
        lead = self.store.get_lead(telegram_user_id, lead_reference)
        if lead["status"] == "manual_required":
            raise PricingNotReady("manual safety review is required")
        if lead["fields"].get("multiple_jobs"):
            raise PricingNotReady("split multiple jobs before pricing")
        rate_card = self.store.latest_rate_card(telegram_user_id)
        if not rate_card:
            raise PricingNotReady("rate card setup is incomplete")
        workspace = self.store.active_workspace(telegram_user_id)
        calculation = calculate_quote(
            rate_card["config"],
            lead["fields"],
            gst_registered=bool(workspace["gst_registered"]),
        )
        return self.store.create_quote(
            telegram_user_id,
            lead["id"],
            calculation.as_dict(),
            expected_lead_version=lead["version"],
        )

    def save_rate_card(self, telegram_user_id: str, config: dict[str, Any]) -> dict[str, Any]:
        return self.store.save_rate_card(telegram_user_id, validate_rate_card(config))

    def delete_lead(self, telegram_user_id: str, lead_reference: str) -> int:
        lead = self.store.get_lead(telegram_user_id, lead_reference)
        paths = self.store.evidence_paths(telegram_user_id, lead["id"])
        removed = 0
        if self.media_root:
            root = self.media_root.resolve()
            for private_path in paths:
                path = Path(private_path).resolve()
                if root not in path.parents:
                    continue
                try:
                    path.unlink()
                    removed += 1
                except FileNotFoundError:
                    pass
        self.store.delete_lead(telegram_user_id, lead["id"])
        return removed


def text_evidence_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
