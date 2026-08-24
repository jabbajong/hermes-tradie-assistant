"""Tenant-scoped SQLite persistence for leads, evidence and quotes."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .runtime import utc_now


class AccessDenied(PermissionError):
    pass


class ConflictError(RuntimeError):
    pass


class NotFoundError(LookupError):
    pass


class ClosingConnection(sqlite3.Connection):
    """Commit or roll back, then release the SQLite file on every platform."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'AUD' CHECK (currency = 'AUD'),
    gst_registered INTEGER NOT NULL CHECK (gst_registered IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    telegram_user_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('owner', 'staff')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS active_workspaces (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_bindings (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    chat_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_updates (
    update_key TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_card_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    version INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, version)
);

CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    created_by TEXT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL CHECK (status IN ('needs_review', 'ready', 'quoted', 'manual_required', 'deleted')),
    source_fingerprint TEXT NOT NULL,
    source_text TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    missing_json TEXT NOT NULL,
    model_status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (workspace_id, source_fingerprint)
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    lead_id TEXT NOT NULL REFERENCES leads(id),
    kind TEXT NOT NULL CHECK (kind IN ('image', 'text')),
    private_path TEXT,
    sha256 TEXT NOT NULL,
    media_type TEXT,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, lead_id, sha256)
);

CREATE TABLE IF NOT EXISTS quote_drafts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    lead_id TEXT NOT NULL REFERENCES leads(id),
    rate_card_version_id INTEGER NOT NULL REFERENCES rate_card_versions(id),
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'approved', 'superseded')),
    lead_version INTEGER NOT NULL,
    subtotal_cents INTEGER NOT NULL,
    gst_cents INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    lines_json TEXT NOT NULL,
    assumptions_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (workspace_id, lead_id, version)
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    quote_id TEXT NOT NULL REFERENCES quote_drafts(id),
    quote_version INTEGER NOT NULL,
    approved_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, quote_id, quote_version)
);

CREATE TABLE IF NOT EXISTS invites (
    token_hash TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    role TEXT NOT NULL CHECK (role = 'staff'),
    created_by TEXT NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL,
    used_by TEXT REFERENCES users(id),
    used_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    actor_user_id TEXT NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_workspace_updated ON leads(workspace_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_quotes_workspace_lead ON quote_drafts(workspace_id, lead_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_audit_workspace_created ON audit_events(workspace_id, created_at DESC);
"""


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:40]
    return normalized or "business"


def _loads(value: str) -> Any:
    return json.loads(value)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def migrate(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @staticmethod
    def _user(connection: sqlite3.Connection, telegram_user_id: str) -> sqlite3.Row:
        value = str(telegram_user_id).strip()
        if not value:
            raise AccessDenied("Telegram identity is required")
        row = connection.execute("SELECT * FROM users WHERE telegram_user_id = ?", (value,)).fetchone()
        if row:
            return row
        user_id = _identifier("usr")
        connection.execute(
            "INSERT INTO users(id, telegram_user_id, created_at) VALUES (?, ?, ?)",
            (user_id, value, utc_now()),
        )
        return connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        workspace_id: str,
        actor_user_id: str,
        action: str,
        entity_type: str,
        entity_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO audit_events(
                   workspace_id, actor_user_id, action, entity_type, entity_id, metadata_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (workspace_id, actor_user_id, action, entity_type, entity_id, json.dumps(metadata or {}), utc_now()),
        )

    @staticmethod
    def _membership(
        connection: sqlite3.Connection,
        telegram_user_id: str,
        *,
        owner_only: bool = False,
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        user = Store._user(connection, telegram_user_id)
        row = connection.execute(
            """SELECT w.*, m.role
               FROM active_workspaces a
               JOIN workspaces w ON w.id = a.workspace_id
               JOIN memberships m ON m.workspace_id = w.id AND m.user_id = a.user_id
               WHERE a.user_id = ? AND w.active = 1 AND m.active = 1""",
            (user["id"],),
        ).fetchone()
        if not row:
            raise AccessDenied("Run /setup to create or join a business workspace")
        if owner_only and row["role"] != "owner":
            raise AccessDenied("Only the workspace owner can do that")
        return user, row, row

    def setup_workspace(self, telegram_user_id: str, name: str, *, gst_registered: bool) -> dict[str, Any]:
        clean_name = " ".join(str(name).split()).strip()
        if len(clean_name) < 2 or len(clean_name) > 100:
            raise ValueError("business name must be 2 to 100 characters")
        with self.connect() as connection:
            user = self._user(connection, telegram_user_id)
            workspace_id = _identifier("ws")
            slug = f"{_slug(clean_name)}-{workspace_id[-6:]}"
            now = utc_now()
            connection.execute(
                "INSERT INTO workspaces(id, slug, name, gst_registered, created_at) VALUES (?, ?, ?, ?, ?)",
                (workspace_id, slug, clean_name, int(gst_registered), now),
            )
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, created_at) VALUES (?, ?, 'owner', ?)",
                (workspace_id, user["id"], now),
            )
            connection.execute(
                "INSERT OR REPLACE INTO active_workspaces(user_id, workspace_id, updated_at) VALUES (?, ?, ?)",
                (user["id"], workspace_id, now),
            )
            self._audit(connection, workspace_id, user["id"], "workspace.created", "workspace", workspace_id)
            return {"id": workspace_id, "slug": slug, "name": clean_name, "gst_registered": bool(gst_registered)}

    def bind_session(self, session_id: str, telegram_user_id: str, chat_id: str = "") -> None:
        if not session_id:
            return
        with self.connect() as connection:
            user = self._user(connection, telegram_user_id)
            connection.execute(
                """INSERT INTO session_bindings(session_id, user_id, chat_id, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     user_id = excluded.user_id, chat_id = excluded.chat_id, updated_at = excluded.updated_at""",
                (session_id, user["id"], str(chat_id or ""), utc_now()),
            )

    def telegram_user_for_session(self, session_id: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT u.telegram_user_id FROM session_bindings s
                   JOIN users u ON u.id = s.user_id WHERE s.session_id = ?""",
                (session_id,),
            ).fetchone()
            if not row:
                raise AccessDenied("The Telegram session is not bound to a user")
            return str(row["telegram_user_id"])

    def active_workspace(self, telegram_user_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            _, workspace, _ = self._membership(connection, telegram_user_id)
            return dict(workspace)

    def switch_workspace(self, telegram_user_id: str, workspace_ref: str) -> dict[str, Any]:
        with self.connect() as connection:
            user = self._user(connection, telegram_user_id)
            row = connection.execute(
                """SELECT w.*, m.role FROM workspaces w
                   JOIN memberships m ON m.workspace_id = w.id
                   WHERE m.user_id = ? AND m.active = 1 AND w.active = 1
                     AND (w.id = ? OR w.slug = ?)""",
                (user["id"], workspace_ref, workspace_ref),
            ).fetchone()
            if not row:
                raise AccessDenied("That workspace is unavailable to this Telegram user")
            connection.execute(
                "INSERT OR REPLACE INTO active_workspaces(user_id, workspace_id, updated_at) VALUES (?, ?, ?)",
                (user["id"], row["id"], utc_now()),
            )
            self._audit(connection, row["id"], user["id"], "workspace.switched", "workspace", row["id"])
            return dict(row)

    def create_invite(self, telegram_user_id: str, *, hours: int = 24) -> str:
        with self.connect() as connection:
            user, workspace, _ = self._membership(connection, telegram_user_id, owner_only=True)
            token = secrets.token_urlsafe(24)
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0).isoformat()
            connection.execute(
                "INSERT INTO invites(token_hash, workspace_id, role, created_by, expires_at) VALUES (?, ?, 'staff', ?, ?)",
                (token_hash, workspace["id"], user["id"], expires),
            )
            self._audit(connection, workspace["id"], user["id"], "invite.created", "workspace", workspace["id"])
            return token

    def accept_invite(self, telegram_user_id: str, token: str) -> dict[str, Any]:
        token_hash = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
        with self.connect() as connection:
            user = self._user(connection, telegram_user_id)
            invite = connection.execute("SELECT * FROM invites WHERE token_hash = ?", (token_hash,)).fetchone()
            if not invite or invite["used_at"]:
                raise AccessDenied("Invite is invalid or already used")
            if datetime.fromisoformat(invite["expires_at"]) <= datetime.now(timezone.utc):
                raise AccessDenied("Invite has expired")
            now = utc_now()
            connection.execute(
                """INSERT INTO memberships(workspace_id, user_id, role, created_at)
                   VALUES (?, ?, 'staff', ?)
                   ON CONFLICT(workspace_id, user_id) DO UPDATE SET role = 'staff', active = 1""",
                (invite["workspace_id"], user["id"], now),
            )
            connection.execute(
                "UPDATE invites SET used_by = ?, used_at = ? WHERE token_hash = ?",
                (user["id"], now, token_hash),
            )
            connection.execute(
                "INSERT OR REPLACE INTO active_workspaces(user_id, workspace_id, updated_at) VALUES (?, ?, ?)",
                (user["id"], invite["workspace_id"], now),
            )
            self._audit(connection, invite["workspace_id"], user["id"], "invite.accepted", "workspace", invite["workspace_id"])
            workspace = connection.execute("SELECT * FROM workspaces WHERE id = ?", (invite["workspace_id"],)).fetchone()
            return dict(workspace)

    def save_rate_card(self, telegram_user_id: str, config: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            user, workspace, _ = self._membership(connection, telegram_user_id, owner_only=True)
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM rate_card_versions WHERE workspace_id = ?",
                (workspace["id"],),
            ).fetchone()[0]
            cursor = connection.execute(
                """INSERT INTO rate_card_versions(workspace_id, version, config_json, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (workspace["id"], version, json.dumps(config, sort_keys=True), user["id"], utc_now()),
            )
            self._audit(connection, workspace["id"], user["id"], "rate_card.created", "rate_card", str(cursor.lastrowid), {"version": version})
            return {"id": cursor.lastrowid, "version": version, "config": config}

    @staticmethod
    def _latest_rate_card(connection: sqlite3.Connection, workspace_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM rate_card_versions WHERE workspace_id = ? ORDER BY version DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["config"] = _loads(result.pop("config_json"))
        return result

    def latest_rate_card(self, telegram_user_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            _, workspace, _ = self._membership(connection, telegram_user_id)
            return self._latest_rate_card(connection, workspace["id"])

    @staticmethod
    def lead_fingerprint(source_text: str, evidence_hashes: Iterable[str] = ()) -> str:
        normalized = " ".join(source_text.casefold().split())
        payload = "\n".join([normalized, *sorted(evidence_hashes)])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def create_lead(
        self,
        telegram_user_id: str,
        source_text: str,
        *,
        update_key: str = "",
        evidence_hashes: Iterable[str] = (),
    ) -> tuple[dict[str, Any], bool]:
        clean_text = source_text.strip()
        if not clean_text:
            raise ValueError("lead text is required")
        if len(clean_text) > 50_000:
            raise ValueError("lead text is too long")
        fingerprint = self.lead_fingerprint(clean_text, evidence_hashes)
        with self.connect() as connection:
            user, workspace, _ = self._membership(connection, telegram_user_id)
            if update_key:
                existing_update = connection.execute(
                    "SELECT workspace_id FROM processed_updates WHERE update_key = ?", (update_key,)
                ).fetchone()
                if existing_update:
                    if existing_update["workspace_id"] != workspace["id"]:
                        raise AccessDenied("update identity belongs to another workspace")
                    row = connection.execute(
                        "SELECT * FROM leads WHERE workspace_id = ? AND source_fingerprint = ?",
                        (workspace["id"], fingerprint),
                    ).fetchone()
                    if row:
                        return self._decode_lead(row), True
            existing = connection.execute(
                "SELECT * FROM leads WHERE workspace_id = ? AND source_fingerprint = ?",
                (workspace["id"], fingerprint),
            ).fetchone()
            if existing:
                return self._decode_lead(existing), True
            now = utc_now()
            lead_id = _identifier("lead")
            fields = {"description": clean_text}
            connection.execute(
                """INSERT INTO leads(
                       id, workspace_id, created_by, status, source_fingerprint, source_text,
                       fields_json, missing_json, model_status, created_at, updated_at
                   ) VALUES (?, ?, ?, 'needs_review', ?, ?, ?, ?, 'pending', ?, ?)""",
                (lead_id, workspace["id"], user["id"], fingerprint, clean_text, json.dumps(fields), json.dumps([]), now, now),
            )
            if update_key:
                connection.execute(
                    "INSERT INTO processed_updates(update_key, workspace_id, created_at) VALUES (?, ?, ?)",
                    (update_key, workspace["id"], now),
                )
            self._audit(connection, workspace["id"], user["id"], "lead.created", "lead", lead_id)
            row = connection.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            return self._decode_lead(row), False

    @staticmethod
    def _decode_lead(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["fields"] = _loads(result.pop("fields_json"))
        result["missing"] = _loads(result.pop("missing_json"))
        return result

    @staticmethod
    def _resolve_entity(
        connection: sqlite3.Connection,
        table: str,
        workspace_id: str,
        reference: str,
    ) -> sqlite3.Row:
        if table not in {"leads", "quote_drafts"}:
            raise ValueError("unsupported entity table")
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE workspace_id = ? AND id LIKE ? ORDER BY updated_at DESC LIMIT 2",
            (workspace_id, f"{reference}%"),
        ).fetchall()
        if not rows:
            raise NotFoundError("item was not found in the active workspace")
        if len(rows) > 1:
            raise ConflictError("ID prefix is ambiguous; use more characters")
        return rows[0]

    def get_lead(self, telegram_user_id: str, reference: str) -> dict[str, Any]:
        with self.connect() as connection:
            _, workspace, _ = self._membership(connection, telegram_user_id)
            return self._decode_lead(self._resolve_entity(connection, "leads", workspace["id"], reference))

    def list_leads(self, telegram_user_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            _, workspace, _ = self._membership(connection, telegram_user_id)
            rows = connection.execute(
                """SELECT * FROM leads WHERE workspace_id = ? AND status != 'deleted'
                   ORDER BY updated_at DESC LIMIT ?""",
                (workspace["id"], max(1, min(limit, 50))),
            ).fetchall()
            return [self._decode_lead(row) for row in rows]

    def update_lead(
        self,
        telegram_user_id: str,
        lead_reference: str,
        *,
        expected_version: int,
        fields: dict[str, Any],
        missing: list[str],
        status: str,
        model_status: str,
    ) -> dict[str, Any]:
        if status not in {"needs_review", "ready", "manual_required"}:
            raise ValueError("invalid lead update status")
        with self.connect() as connection:
            user, workspace, _ = self._membership(connection, telegram_user_id)
            lead = self._resolve_entity(connection, "leads", workspace["id"], lead_reference)
            cursor = connection.execute(
                """UPDATE leads SET fields_json = ?, missing_json = ?, status = ?, model_status = ?,
                          version = version + 1, updated_at = ?
                   WHERE id = ? AND workspace_id = ? AND version = ?""",
                (json.dumps(fields), json.dumps(missing), status, model_status, utc_now(), lead["id"], workspace["id"], expected_version),
            )
            if cursor.rowcount != 1:
                raise ConflictError("lead changed while it was being processed; reload before retrying")
            self._audit(connection, workspace["id"], user["id"], "lead.updated", "lead", lead["id"], {"status": status})
            row = connection.execute("SELECT * FROM leads WHERE id = ?", (lead["id"],)).fetchone()
            return self._decode_lead(row)

    def add_evidence(self, telegram_user_id: str, lead_reference: str, evidence: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            user, workspace, _ = self._membership(connection, telegram_user_id)
            lead = self._resolve_entity(connection, "leads", workspace["id"], lead_reference)
            evidence_id = _identifier("ev")
            connection.execute(
                """INSERT OR IGNORE INTO evidence(
                       id, workspace_id, lead_id, kind, private_path, sha256, media_type, size_bytes, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (evidence_id, workspace["id"], lead["id"], evidence["kind"], evidence.get("private_path"), evidence["sha256"], evidence.get("media_type"), int(evidence["size_bytes"]), utc_now()),
            )
            self._audit(connection, workspace["id"], user["id"], "evidence.added", "lead", lead["id"], {"sha256": evidence["sha256"]})
            row = connection.execute(
                "SELECT * FROM evidence WHERE workspace_id = ? AND lead_id = ? AND sha256 = ?",
                (workspace["id"], lead["id"], evidence["sha256"]),
            ).fetchone()
            return dict(row)

    def evidence_paths(self, telegram_user_id: str, lead_reference: str) -> list[str]:
        with self.connect() as connection:
            _, workspace, _ = self._membership(connection, telegram_user_id, owner_only=True)
            lead = self._resolve_entity(connection, "leads", workspace["id"], lead_reference)
            rows = connection.execute(
                "SELECT private_path FROM evidence WHERE workspace_id = ? AND lead_id = ? AND private_path IS NOT NULL",
                (workspace["id"], lead["id"]),
            ).fetchall()
            return [str(row["private_path"]) for row in rows]

    def create_quote(
        self,
        telegram_user_id: str,
        lead_reference: str,
        calculation: dict[str, Any],
        *,
        expected_lead_version: int,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            user, workspace, _ = self._membership(connection, telegram_user_id)
            lead = self._resolve_entity(connection, "leads", workspace["id"], lead_reference)
            if int(lead["version"]) != int(expected_lead_version):
                raise ConflictError("lead changed before quote creation; recalculate it")
            rate_card = self._latest_rate_card(connection, workspace["id"])
            if not rate_card:
                raise ConflictError("complete the rate card before preparing a quote")
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM quote_drafts WHERE workspace_id = ? AND lead_id = ?",
                (workspace["id"], lead["id"]),
            ).fetchone()[0]
            connection.execute(
                "UPDATE quote_drafts SET status = 'superseded', updated_at = ? WHERE workspace_id = ? AND lead_id = ? AND status = 'draft'",
                (utc_now(), workspace["id"], lead["id"]),
            )
            quote_id = _identifier("quote")
            now = utc_now()
            connection.execute(
                """INSERT INTO quote_drafts(
                       id, workspace_id, lead_id, rate_card_version_id, version, status, lead_version,
                       subtotal_cents, gst_cents, total_cents, lines_json, assumptions_json,
                       created_by, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    quote_id, workspace["id"], lead["id"], rate_card["id"], version, expected_lead_version,
                    calculation["subtotal_cents"], calculation["gst_cents"], calculation["total_cents"],
                    json.dumps(calculation["lines"]), json.dumps(calculation["assumptions"]), user["id"], now, now,
                ),
            )
            connection.execute(
                "UPDATE leads SET status = 'quoted', version = version + 1, updated_at = ? WHERE id = ? AND workspace_id = ?",
                (now, lead["id"], workspace["id"]),
            )
            self._audit(connection, workspace["id"], user["id"], "quote.created", "quote", quote_id, {"version": version})
            return self.get_quote_by_id(connection, workspace["id"], quote_id)

    @staticmethod
    def get_quote_by_id(connection: sqlite3.Connection, workspace_id: str, quote_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM quote_drafts WHERE workspace_id = ? AND id = ?", (workspace_id, quote_id)
        ).fetchone()
        if not row:
            raise NotFoundError("quote was not found")
        result = dict(row)
        result["lines"] = _loads(result.pop("lines_json"))
        result["assumptions"] = _loads(result.pop("assumptions_json"))
        return result

    def latest_quote(self, telegram_user_id: str, lead_reference: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            _, workspace, _ = self._membership(connection, telegram_user_id)
            lead = self._resolve_entity(connection, "leads", workspace["id"], lead_reference)
            row = connection.execute(
                "SELECT id FROM quote_drafts WHERE workspace_id = ? AND lead_id = ? ORDER BY version DESC LIMIT 1",
                (workspace["id"], lead["id"]),
            ).fetchone()
            return self.get_quote_by_id(connection, workspace["id"], row["id"]) if row else None

    def approve_quote(self, telegram_user_id: str, quote_reference: str, *, expected_version: int) -> dict[str, Any]:
        with self.connect() as connection:
            user, workspace, _ = self._membership(connection, telegram_user_id)
            quote = self._resolve_entity(connection, "quote_drafts", workspace["id"], quote_reference)
            if int(quote["version"]) != int(expected_version):
                raise ConflictError("quote version does not match the reviewed version")
            if quote["status"] == "superseded":
                raise ConflictError("a superseded quote cannot be approved")
            now = utc_now()
            connection.execute(
                "UPDATE quote_drafts SET status = 'approved', updated_at = ? WHERE id = ? AND workspace_id = ?",
                (now, quote["id"], workspace["id"]),
            )
            connection.execute(
                """INSERT OR IGNORE INTO approvals(id, workspace_id, quote_id, quote_version, approved_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (_identifier("approval"), workspace["id"], quote["id"], expected_version, user["id"], now),
            )
            self._audit(connection, workspace["id"], user["id"], "quote.approved", "quote", quote["id"], {"version": expected_version})
            return self.get_quote_by_id(connection, workspace["id"], quote["id"])

    def delete_lead(self, telegram_user_id: str, lead_reference: str) -> None:
        with self.connect() as connection:
            user, workspace, _ = self._membership(connection, telegram_user_id, owner_only=True)
            lead = self._resolve_entity(connection, "leads", workspace["id"], lead_reference)
            connection.execute(
                "UPDATE leads SET status = 'deleted', source_text = '', fields_json = '{}', missing_json = '[]', version = version + 1, updated_at = ? WHERE id = ? AND workspace_id = ?",
                (utc_now(), lead["id"], workspace["id"]),
            )
            connection.execute(
                "UPDATE quote_drafts SET status = 'superseded', updated_at = ? WHERE workspace_id = ? AND lead_id = ? AND status = 'draft'",
                (utc_now(), workspace["id"], lead["id"]),
            )
            connection.execute(
                "DELETE FROM evidence WHERE workspace_id = ? AND lead_id = ?",
                (workspace["id"], lead["id"]),
            )
            self._audit(connection, workspace["id"], user["id"], "lead.deleted", "lead", lead["id"])

    def export_workspace(self, telegram_user_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            user, workspace, _ = self._membership(connection, telegram_user_id, owner_only=True)
            memberships = connection.execute(
                """SELECT u.telegram_user_id, m.role, m.active, m.created_at
                   FROM memberships m JOIN users u ON u.id = m.user_id
                   WHERE m.workspace_id = ?""",
                (workspace["id"],),
            ).fetchall()
            leads = connection.execute(
                "SELECT * FROM leads WHERE workspace_id = ? ORDER BY created_at", (workspace["id"],)
            ).fetchall()
            quotes = connection.execute(
                "SELECT * FROM quote_drafts WHERE workspace_id = ? ORDER BY created_at", (workspace["id"],)
            ).fetchall()
            rate_cards = connection.execute(
                "SELECT * FROM rate_card_versions WHERE workspace_id = ? ORDER BY version", (workspace["id"],)
            ).fetchall()
            self._audit(connection, workspace["id"], user["id"], "workspace.exported", "workspace", workspace["id"])
            return {
                "schema_version": 1,
                "exported_at": utc_now(),
                "workspace": {key: workspace[key] for key in ("id", "slug", "name", "currency", "gst_registered", "created_at")},
                "memberships": [dict(row) for row in memberships],
                "rate_cards": [{**dict(row), "config": _loads(row["config_json"])} for row in rate_cards],
                "leads": [self._decode_lead(row) for row in leads],
                "quotes": [self.get_quote_by_id(connection, workspace["id"], row["id"]) for row in quotes],
            }
