from __future__ import annotations

import unittest
from unittest.mock import patch

from tradie_assistant.provider import ProviderError, preflight_openrouter, privacy_request_payload


class ProviderTests(unittest.TestCase):
    def test_every_request_contains_fail_closed_privacy_policy(self):
        payload = privacy_request_payload(
            "example/model",
            [{"role": "user", "content": "test"}],
            max_tokens=100,
        )
        self.assertEqual(
            payload["provider"],
            {
                "zdr": True,
                "data_collection": "deny",
                "allow_fallbacks": False,
                "require_parameters": True,
            },
        )

    def test_automatic_model_routes_are_rejected(self):
        with self.assertRaises(ProviderError):
            privacy_request_payload("openrouter/auto", [], max_tokens=100)

    @patch("tradie_assistant.provider._get_json")
    def test_preflight_requires_model_modalities_and_zdr_endpoint(self, get_json):
        get_json.side_effect = [
            {
                "data": {
                    "id": "example/model",
                    "architecture": {"input_modalities": ["text", "image"]},
                }
            },
            {
                "data": [
                    {
                        "model_id": "example/model",
                        "provider_name": "Provider A",
                        "supported_parameters": ["max_tokens", "response_format"],
                    }
                ]
            },
        ]
        result = preflight_openrouter("secret", "example/model")
        self.assertEqual(result["zdr_provider_count"], 1)
        self.assertEqual(result["zdr_providers"], ["Provider A"])

    @patch("tradie_assistant.provider._get_json")
    def test_preflight_fails_when_zdr_route_disappears(self, get_json):
        get_json.side_effect = [
            {
                "data": {
                    "id": "example/model",
                    "architecture": {"input_modalities": ["text", "image"]},
                }
            },
            {"data": []},
        ]
        with self.assertRaisesRegex(ProviderError, "no Zero Data Retention"):
            preflight_openrouter("secret", "example/model")

    @patch("tradie_assistant.provider._get_json")
    def test_preflight_fails_when_image_input_is_missing(self, get_json):
        get_json.return_value = {
            "data": {"id": "example/model", "architecture": {"input_modalities": ["text"]}}
        }
        with self.assertRaisesRegex(ProviderError, "does not accept image"):
            preflight_openrouter("secret", "example/model")

    @patch("tradie_assistant.provider._get_json")
    def test_preflight_fails_when_zdr_route_cannot_honor_json_output(self, get_json):
        get_json.side_effect = [
            {
                "data": {
                    "id": "example/model",
                    "architecture": {"input_modalities": ["text", "image"]},
                }
            },
            {
                "data": [
                    {
                        "model_id": "example/model",
                        "provider_name": "Provider A",
                        "supported_parameters": ["max_tokens"],
                    }
                ]
            },
        ]
        with self.assertRaisesRegex(ProviderError, "structured quote extraction"):
            preflight_openrouter("secret", "example/model")


if __name__ == "__main__":
    unittest.main()
