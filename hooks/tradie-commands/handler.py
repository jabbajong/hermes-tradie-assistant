"""Handle the private product commands before the general Hermes agent."""

from __future__ import annotations

import sys
from pathlib import Path


PROFILE_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROFILE_ROOT / "workspace" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from tradie_assistant.commands import dispatch  # noqa: E402
from tradie_assistant.db import Store  # noqa: E402
from tradie_assistant.runtime import database_path  # noqa: E402


PRODUCT_COMMANDS = {
    "help",
    "setup",
    "new",
    "lead",
    "quote",
    "approve",
    "revise",
    "invite",
    "switch",
    "export",
    "delete",
    "commands",
    "whoami",
    "reset",
}


async def handle(event_type: str, context: dict):
    if context.get("platform") != "telegram":
        return None
    store = Store(database_path())
    user_id = str(context.get("user_id") or "").strip()
    session_id = str(context.get("session_id") or context.get("session_key") or "").strip()
    if user_id and session_id and event_type in {"session:start", "session:reset", "agent:start"}:
        store.bind_session(session_id, user_id, str(context.get("chat_id") or ""))
    if not event_type.startswith("command:"):
        return None
    command = str(context.get("command") or event_type.split(":", 1)[1]).casefold().lstrip("/")
    if command not in PRODUCT_COMMANDS:
        return None
    if command in {"commands", "whoami", "reset"}:
        command = "help"
    image_paths = []
    for item in context.get("media_paths") or context.get("attachments") or []:
        candidate = item.get("path") if isinstance(item, dict) else item
        if isinstance(candidate, str) and candidate:
            image_paths.append(Path(candidate))
    message = dispatch(
        command,
        str(context.get("args") or ""),
        user_id,
        store=store,
        image_paths=image_paths,
    )
    return {"decision": "handled", "message": message[:4000]}
