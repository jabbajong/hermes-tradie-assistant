"""Private image validation and content-addressed storage."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


class MediaRejected(ValueError):
    pass


MAGIC_TYPES = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"RIFF", "image/webp", ".webp"),
)


@dataclass(frozen=True)
class StoredImage:
    path: Path
    sha256: str
    media_type: str
    size_bytes: int


def detect_image_type(data: bytes) -> tuple[str, str]:
    for magic, media_type, suffix in MAGIC_TYPES:
        if not data.startswith(magic):
            continue
        if media_type == "image/webp" and (len(data) < 12 or data[8:12] != b"WEBP"):
            continue
        return media_type, suffix
    raise MediaRejected("only JPEG, PNG and WebP images are accepted")


def purge_expired_images(root: Path, *, retention_days: int, now_epoch: float | None = None) -> int:
    """Remove private media files older than the configured retention window."""
    if retention_days < 1:
        raise ValueError("media retention must be at least one day")
    if not root.exists():
        return 0
    cutoff = (time.time() if now_epoch is None else now_epoch) - (retention_days * 86400)
    removed = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    for directory in sorted((item for item in root.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed


def store_image(
    source: Path,
    destination_root: Path,
    *,
    workspace_id: str,
    lead_id: str,
    allowed_source_root: Path,
    max_bytes: int = 10 * 1024 * 1024,
) -> StoredImage:
    source = source.resolve(strict=True)
    allowed_source_root = allowed_source_root.resolve(strict=True)
    if source.is_symlink() or allowed_source_root not in source.parents:
        raise MediaRejected("image must come from the private profile inbox")
    size = source.stat().st_size
    if size <= 0 or size > max_bytes:
        raise MediaRejected("image size is outside the allowed range")
    data = source.read_bytes()
    media_type, suffix = detect_image_type(data)
    digest = hashlib.sha256(data).hexdigest()
    destination = destination_root / workspace_id / lead_id / f"{digest}{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copyfile(source, temporary)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, destination)
    return StoredImage(destination, digest, media_type, size)


def image_data_url(path: Path, media_type: str, *, max_bytes: int) -> str:
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise MediaRejected("stored image exceeds the provider upload limit")
    detected, _ = detect_image_type(data)
    if detected != media_type:
        raise MediaRejected("stored image type no longer matches its record")
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"
