from __future__ import annotations

import unittest
from pathlib import Path


PROFILE = Path(__file__).resolve().parents[2]


class DeploymentFileTests(unittest.TestCase):
    def test_container_has_only_profile_and_shared_runtime_mounts(self):
        content = (
            PROFILE
            / "systemd"
            / "hermes-gateway@tradie-assistant.service.d"
            / "container.conf"
        ).read_text(encoding="utf-8")
        self.assertIn("/opt/hermes-agents/tradie-assistant:/opt/hermes-agents/tradie-assistant:rw", content)
        self.assertIn("/usr/local/lib/hermes-agent:/usr/local/lib/hermes-agent:ro", content)
        self.assertNotIn("docker.sock", content)
        self.assertNotIn("--publish", content)

    def test_real_provider_key_is_kept_in_separate_guard_environment(self):
        guard = (PROFILE / "systemd" / "hermes-tradie-assistant-openrouter-guard.service").read_text(encoding="utf-8")
        gateway = (
            PROFILE
            / "systemd"
            / "hermes-gateway@tradie-assistant.service.d"
            / "container.conf"
        ).read_text(encoding="utf-8")
        self.assertIn("/etc/hermes/tradie-assistant-openrouter.env", guard)
        self.assertIn("/etc/hermes/tradie-assistant.env", gateway)
        self.assertNotIn("tradie-assistant-openrouter.env", gateway)


if __name__ == "__main__":
    unittest.main()
