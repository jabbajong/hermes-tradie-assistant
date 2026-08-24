from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tradie_assistant.media import MediaRejected, image_data_url, purge_expired_images, store_image


class MediaTests(unittest.TestCase):
    def test_png_is_content_addressed_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "inbox"
            inbox.mkdir()
            source = inbox / "job.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test-image")
            stored = store_image(
                source,
                root / "media",
                workspace_id="ws_1",
                lead_id="lead_1",
                allowed_source_root=inbox,
                max_bytes=1024,
            )
            self.assertEqual(stored.media_type, "image/png")
            self.assertTrue(stored.path.exists())
            self.assertTrue(image_data_url(stored.path, stored.media_type, max_bytes=1024).startswith("data:image/png;base64,"))

    def test_non_image_and_outside_inbox_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "inbox"
            inbox.mkdir()
            outside = root / "outside.txt"
            outside.write_text("not an image", encoding="utf-8")
            with self.assertRaises(MediaRejected):
                store_image(outside, root / "media", workspace_id="ws", lead_id="lead", allowed_source_root=inbox)

    def test_expired_images_are_removed_but_recent_images_remain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "ws" / "old.png"
            recent = root / "ws" / "recent.png"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"old")
            recent.write_bytes(b"recent")
            old.touch()
            recent.touch()
            old_epoch = 1_700_000_000
            recent_epoch = old_epoch + (29 * 86400)
            import os

            os.utime(old, (old_epoch, old_epoch))
            os.utime(recent, (recent_epoch, recent_epoch))
            removed = purge_expired_images(root, retention_days=30, now_epoch=old_epoch + (31 * 86400))
            self.assertEqual(removed, 1)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())


if __name__ == "__main__":
    unittest.main()
