#!/usr/bin/env python3
"""Loopback-only OpenRouter proxy enforcing a fixed model and ZDR policy."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from tradie_assistant.provider import PRIVACY_POLICY, ProviderError, preflight_openrouter, validate_model_id
from tradie_assistant.runtime import atomic_write_json


LOCAL_ZONE = ZoneInfo("Australia/Perth")
UPSTREAM_HOST = "openrouter.ai"
CHAT_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"
HEALTH_PATH = "/healthz"


@dataclass(frozen=True)
class GuardConfig:
    upstream_base_url: str
    listen_host: str
    listen_port: int
    requests_per_minute: int
    daily_provider_calls: int
    max_output_tokens_per_call: int
    daily_reserved_output_tokens: int
    max_request_bytes: int
    request_timeout_seconds: int
    require_image_input: bool

    @classmethod
    def load(cls, path: Path) -> "GuardConfig":
        data = json.loads(path.read_text(encoding="utf-8"))
        upstream = str(data.get("upstream_base_url", "")).rstrip("/")
        parsed = urlsplit(upstream)
        if parsed.scheme != "https" or parsed.hostname != UPSTREAM_HOST or parsed.path != "/api/v1":
            raise ValueError("upstream_base_url must be exactly https://openrouter.ai/api/v1")

        def positive(name: str, default: int) -> int:
            value = int(data.get(name, default))
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            return value

        host = str(data.get("listen_host", "127.0.0.1"))
        if host not in {"127.0.0.1", "::1"}:
            raise ValueError("listen_host must remain loopback-only")
        return cls(
            upstream_base_url=upstream,
            listen_host=host,
            listen_port=positive("listen_port", 18082),
            requests_per_minute=positive("requests_per_minute", 10),
            daily_provider_calls=positive("daily_provider_calls", 300),
            max_output_tokens_per_call=positive("max_output_tokens_per_call", 4096),
            daily_reserved_output_tokens=positive("daily_reserved_output_tokens", 500_000),
            max_request_bytes=positive("max_request_bytes", 15_000_000),
            request_timeout_seconds=positive("request_timeout_seconds", 180),
            require_image_input=bool(data.get("require_image_input", True)),
        )


class BudgetLedger:
    def __init__(self, path: Path, config: GuardConfig):
        self.path = path
        self.config = config
        self.lock = threading.Lock()

    @staticmethod
    def today() -> str:
        return datetime.now(timezone.utc).astimezone(LOCAL_ZONE).date().isoformat()

    def _read(self) -> dict[str, Any]:
        empty = {"date": self.today(), "provider_calls": 0, "request_timestamps": [], "reserved_output_tokens": 0}
        if not self.path.exists():
            return empty
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("provider budget state is unreadable") from exc
        if not isinstance(state, dict):
            raise RuntimeError("provider budget state is invalid")
        if state.get("date") != self.today():
            return empty
        state["request_timestamps"] = [float(value) for value in state.get("request_timestamps", [])]
        state["provider_calls"] = int(state.get("provider_calls", 0))
        state["reserved_output_tokens"] = int(state.get("reserved_output_tokens", 0))
        return state

    def reserve(self, output_tokens: int) -> tuple[bool, str, int]:
        now = time.time()
        with self.lock:
            state = self._read()
            recent = [value for value in state["request_timestamps"] if now - value < 60]
            if state["provider_calls"] >= self.config.daily_provider_calls:
                return False, "daily provider-call ceiling reached", 3600
            if len(recent) >= self.config.requests_per_minute:
                return False, "per-minute request ceiling reached", max(1, int(60 - (now - recent[0])))
            if state["reserved_output_tokens"] + output_tokens > self.config.daily_reserved_output_tokens:
                return False, "daily output-token ceiling reached", 3600
            state["provider_calls"] += 1
            state["request_timestamps"] = [*recent, now]
            state["reserved_output_tokens"] += output_tokens
            atomic_write_json(self.path, state)
            return True, "", 0


def enforce_privacy_payload(payload: dict[str, Any], model: str, max_output_tokens: int) -> tuple[dict[str, Any], int]:
    validate_model_id(model)
    if "models" in payload or "fallbacks" in payload:
        raise ValueError("model fallback fields are not allowed")
    protected = dict(payload)
    protected["model"] = model
    protected["provider"] = dict(PRIVACY_POLICY)
    token_keys = [key for key in ("max_tokens", "max_completion_tokens") if key in protected]
    requested: list[int] = []
    for key in token_keys:
        try:
            value = int(protected[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer") from exc
        bounded = max(1, min(value, max_output_tokens))
        protected[key] = bounded
        requested.append(bounded)
    if not requested:
        protected["max_tokens"] = max_output_tokens
        requested.append(max_output_tokens)
    return protected, max(requested)


def local_token_matches(authorization: str, expected_token: str) -> bool:
    supplied = authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""
    return bool(supplied and expected_token and hmac.compare_digest(supplied, expected_token))


class GuardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        handler,
        config: GuardConfig,
        ledger: BudgetLedger,
        model: str,
        upstream_api_key: str,
        local_guard_token: str,
    ):
        super().__init__(address, handler)
        self.guard_config = config
        self.ledger = ledger
        self.model = model
        self.upstream_api_key = upstream_api_key
        self.local_guard_token = local_guard_token
        self.opener = build_opener(ProxyHandler({}))


class GuardHandler(BaseHTTPRequestHandler):
    server: GuardServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json_response(self, status: int, payload: dict[str, Any], *, retry_after: int = 0) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if retry_after:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(body)

    def _raw_response(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _upstream_url(self, path: str) -> str:
        parsed = urlsplit(self.path)
        return urlunsplit(("https", UPSTREAM_HOST, f"/api{path}", parsed.query, ""))

    def _forward(self, method: str, upstream_path: str, body: bytes = b"") -> None:
        authorization = self.headers.get("Authorization", "").strip()
        if not local_token_matches(authorization, self.server.local_guard_token):
            self._json_response(401, {"error": {"message": "provider authorization is required"}})
            return
        headers = {
            "Authorization": f"Bearer {self.server.upstream_api_key}",
            "Accept": self.headers.get("Accept", "application/json"),
            "User-Agent": "hermes-tradie-assistant-guard/0.1",
        }
        if body:
            headers["Content-Type"] = "application/json"
        request = Request(self._upstream_url(upstream_path), data=body or None, headers=headers, method=method)
        try:
            with self.server.opener.open(request, timeout=self.server.guard_config.request_timeout_seconds) as response:
                response_body = response.read(16 * 1024 * 1024)
                self._raw_response(int(response.status), response.headers.get("Content-Type", "application/json"), response_body)
        except HTTPError as exc:
            self._raw_response(int(exc.code), exc.headers.get("Content-Type", "application/json"), exc.read(256 * 1024))
        except (OSError, URLError, TimeoutError):
            self._json_response(502, {"error": {"message": "provider unavailable; no fallback is configured"}})

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == HEALTH_PATH:
            self._json_response(200, {"ok": True, "privacy": "zdr", "fallbacks": False, "model": self.server.model})
            return
        if path != MODELS_PATH:
            self._json_response(404, {"error": {"message": "path not allowed"}})
            return
        self._forward("GET", MODELS_PATH)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != CHAT_PATH:
            self._json_response(404, {"error": {"message": "path not allowed"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > self.server.guard_config.max_request_bytes:
            self._json_response(413, {"error": {"message": "request is too large"}})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json_response(400, {"error": {"message": "request must be JSON"}})
            return
        if not isinstance(payload, dict):
            self._json_response(400, {"error": {"message": "request must be a JSON object"}})
            return
        try:
            protected, reserve = enforce_privacy_payload(
                payload, self.server.model, self.server.guard_config.max_output_tokens_per_call
            )
        except (ProviderError, ValueError) as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        allowed, reason, retry_after = self.server.ledger.reserve(reserve)
        if not allowed:
            self._json_response(429, {"error": {"message": reason, "type": "rate_limit_error"}}, retry_after=retry_after)
            return
        self._forward("POST", CHAT_PATH, json.dumps(protected, separators=(",", ":")).encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--skip-preflight", action="store_true")
    args = parser.parse_args()
    config = GuardConfig.load(args.config)
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    model = validate_model_id(os.environ.get("OPENROUTER_MODEL", "").strip())
    local_guard_token = os.environ.get("TRADIE_ASSISTANT_GUARD_TOKEN", "").strip()
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is required")
    if len(local_guard_token) < 32:
        raise SystemExit("TRADIE_ASSISTANT_GUARD_TOKEN must contain at least 32 characters")
    if not args.skip_preflight:
        try:
            preflight_openrouter(api_key, model, base_url=config.upstream_base_url, require_image=config.require_image_input)
        except ProviderError as exc:
            raise SystemExit(f"OpenRouter privacy preflight failed: {exc}") from exc
    ledger = BudgetLedger(args.state, config)
    server = GuardServer(
        (config.listen_host, config.listen_port),
        GuardHandler,
        config,
        ledger,
        model,
        api_key,
        local_guard_token,
    )
    print(
        f"Tradie Assistant OpenRouter guard listening on {config.listen_host}:{config.listen_port}; "
        f"model={model}; ZDR required; fallbacks disabled",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
