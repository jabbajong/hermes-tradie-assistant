# Hermes Tradie Assistant — Customer Setup Walkthrough

This guide explains what the system does, how to install it once on your
server, and the exact steps to onboard each new customer (a trade business)
onto the private Telegram bot.

---

## 1. What it does

Hermes Tradie Assistant is a **private, multi-business Telegram bot** for
Australian tradies. It turns forwarded customer enquiries into reviewable
quote drafts:

1. A staff member forwards or pastes a customer enquiry into Telegram with
   `/new` (photos optional).
2. The raw enquiry is **saved to a private SQLite database first** — before
   any AI call — so a lead can never silently disappear.
3. An AI model (via a locked-down OpenRouter guard) extracts structured job
   details: service type, suburb, urgency, estimated hours, travel,
   materials, hazards.
4. **Pricing is deterministic.** The model never does arithmetic and never
   sees rates. Quotes are calculated in code from the business's own
   versioned rate card (labour, call-out, travel, markup, after-hours
   multiplier, rounding, GST).
5. Nothing is ever sent to the customer. A human reviews the draft and locks
   it with `/approve QUOTE_ID v=N`, then copies the text themselves.

### Safety boundaries (built in, not optional)

- Every database query is scoped to the Telegram user's active workspace —
  one business can never see another business's leads, rates or quotes.
- Immediate-hazard phrases (gas leak, asbestos, live wires, …), multiple jobs
  in one message, or missing quantities **block automatic quoting**.
- If the AI provider fails, the lead stays saved as `needs_review`.
- `/approve` only locks the reviewed draft. It does not message the customer,
  book anything, or promise availability.
- The OpenRouter guard runs on loopback only, pins one exact model, forces
  Zero Data Retention (`zdr: true`, `data_collection: "deny"`), disables
  provider fallbacks, enforces daily usage ceilings, and logs no request
  content. The real OpenRouter key never enters the bot container.

### The commands

| Command | Who | What it does |
| --- | --- | --- |
| `/setup Business Name \| gst=yes` | New owner | Creates an isolated business workspace |
| `/setup invite=TOKEN` | Invited staff | Joins an existing workspace |
| `/revise ratecard labour=120 callout=90 minimum=150 travel=1.20 included_km=10 markup=20 after_hours=1.5 rounding=1 inclusive_gst=no` | Owner | Saves a new immutable rate-card version |
| `/new <forwarded enquiry>` | Owner + staff | Saves a lead, extracts details, drafts a quote if complete |
| `/lead` / `/lead LEAD_ID` | Owner + staff | Lists recent leads / shows one lead |
| `/quote LEAD_ID` | Owner + staff | Prepares or displays the deterministic quote draft |
| `/approve QUOTE_ID v=N` | Owner + staff | Locks exactly the reviewed quote version |
| `/revise lead LEAD_ID hours=2 travel_km=15 materials=80 after_hours=no service=plumbing` | Owner + staff | Corrects lead facts and re-readies it for quoting |
| `/invite staff` | Owner | Creates a one-use, 24-hour staff invite token |
| `/switch WORKSPACE_SLUG` | Members of multiple workspaces | Changes the active workspace |
| `/export` | Owner | Writes a private JSON export of the workspace |
| `/delete lead LEAD_ID` | Owner | Deletes customer content (and its images); the audit event is retained |
| `/help` | Everyone | Shows the command summary |

---

## 2. Architecture at a glance

```text
Private Telegram users (allowlisted Telegram IDs)
  -> Hermes gateway container (per-profile, no public port)
      -> tenant-aware command hook (hooks/tradie-commands)
          -> SQLite database        (workspace/state/tradie-assistant.sqlite3)
          -> private image storage  (workspace/state/media, 30-day retention)
          -> deterministic pricing engine (versioned rate cards)
      -> loopback OpenRouter guard (127.0.0.1:18082, host systemd service)
          -> the exact configured multimodal model on a ZDR endpoint
```

- One bot deployment serves **many businesses**; tenant isolation is enforced
  in the database layer. Onboarding a new customer does **not** require a new
  deployment — only a Telegram allowlist entry plus the customer's own
  `/setup` (see section 5).
- The container mounts the profile read-write and the shared Hermes runtime
  read-only. It has no Docker socket and publishes no web port, DNS entry or
  Traefik route.
- All state lives under the profile directory (default
  `/opt/hermes-agents/tradie-assistant`): the SQLite database, private media,
  exports and the guard's budget ledger.

---

## 3. Prerequisites (one time)

Before installing you need:

- A Linux server (the README targets Contabo; any Docker + systemd host works)
  with Docker installed.
- The shared Hermes runtime installed at `/usr/local/lib/hermes-agent`
  (Python venv + `hermes_cli`). The container mounts this read-only.
- The `hermes-telegram-menu` helper at `/usr/local/sbin/` (used by the
  gateway unit's `ExecStartPost`).
- A **Telegram bot token** from @BotFather.
- The **Telegram user IDs** of everyone who will use the bot (each user can
  get theirs from @userinfobot).
- An **OpenRouter account and API key**, with account privacy controls set to
  disable prompt logging and training use (a second privacy boundary on top
  of the guard).
- An **exact model slug** (for example `author/model-name`) that is
  multimodal (text + image input) and available on an OpenRouter Zero Data
  Retention endpoint. `openrouter/auto` and floating aliases are rejected by
  design. `workspace/scripts/provider_preflight.py` verifies this for you.

---

## 4. One-time server installation

Run these once per server. All profile paths below assume the profile lives
at `/opt/hermes-agents/tradie-assistant`.

### 4.1 Create the host user and profile directory

```bash
sudo useradd --system --home /opt/hermes-agents/tradie-assistant --shell /usr/sbin/nologin hermes-tradie-assistant
sudo mkdir -p /opt/hermes-agents/tradie-assistant
sudo chown hermes-tradie-assistant:hermes-tradie-assistant /opt/hermes-agents/tradie-assistant
```

### 4.2 Copy the tested profile and record its release manifest

Copy this repository's contents into `/opt/hermes-agents/tradie-assistant`,
then record what you deployed:

```bash
cd /opt/hermes-agents/tradie-assistant
python3 workspace/scripts/release_check.py --write release-manifest.json --tests passed
```

Keep the manifest — it is the hash record of exactly what is running.

### 4.3 Create the two secret files (root-owned, mode 600, never printed)

Generate one random local guard token and use it in **both** files:

```bash
GUARD_TOKEN=$(openssl rand -hex 32)   # note it down for the two files below
```

`/etc/hermes/tradie-assistant.env` (gateway container — start from
`.env.example`):

```text
OPENROUTER_API_KEY=<the random guard token, NOT the real provider key>
OPENROUTER_MODEL=<exact author/model-slug>
OPENROUTER_BASE_URL=http://127.0.0.1:18082/v1
TELEGRAM_BOT_TOKEN=<token from @BotFather>
TELEGRAM_ALLOWED_USERS=<comma-separated Telegram user IDs>
HERMES_TELEGRAM_DISABLE_FALLBACK_IPS=true
TRADIE_ASSISTANT_DB=/opt/hermes-agents/tradie-assistant/workspace/state/tradie-assistant.sqlite3
TRADIE_ASSISTANT_MEDIA_ROOT=/opt/hermes-agents/tradie-assistant/workspace/state/media
TRADIE_ASSISTANT_MEDIA_MAX_BYTES=10485760
TRADIE_ASSISTANT_MEDIA_RETENTION_DAYS=30
```

`/etc/hermes/tradie-assistant-openrouter.env` (host guard service — start
from `.openrouter-guard.env.example`):

```text
OPENROUTER_API_KEY=<the REAL OpenRouter provider key>
OPENROUTER_MODEL=<same exact author/model-slug>
TRADIE_ASSISTANT_GUARD_TOKEN=<the same random guard token>
```

```bash
sudo chmod 600 /etc/hermes/tradie-assistant.env /etc/hermes/tradie-assistant-openrouter.env
sudo chown root:root /etc/hermes/tradie-assistant.env /etc/hermes/tradie-assistant-openrouter.env
```

The real provider key exists only in the host-side guard file and is never
mounted into the container.

### 4.4 Build the container image

```bash
cd /opt/hermes-agents/tradie-assistant
sudo docker build -t nadirstack/hermes-tradie-assistant:0.1.0 -f container/Dockerfile .
```

### 4.5 Install the systemd units

```bash
sudo cp systemd/hermes-tradie-assistant-openrouter-guard.service /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/hermes-gateway@tradie-assistant.service.d
sudo cp systemd/hermes-gateway@tradie-assistant.service.d/*.conf /etc/systemd/system/hermes-gateway@tradie-assistant.service.d/
sudo systemctl daemon-reload
```

The gateway unit refuses to start until
`/opt/hermes-agents/tradie-assistant/.telegram-ready` exists, and it always
starts after (and requires) the guard service.

### 4.6 Run the local validation suite

No provider key is needed:

```bash
cd /opt/hermes-agents/tradie-assistant
PYTHONPATH=workspace/src python3 -m unittest discover -s workspace/tests -v
python3 -m compileall -q workspace/src workspace/scripts hooks
```

### 4.7 Run the provider preflight

With the real key and model in your environment (read-only checks):

```bash
set -a; . /etc/hermes/tradie-assistant-openrouter.env; set +a
python3 workspace/scripts/provider_preflight.py --config workspace/config/provider_guard.json
```

This must print `preflight passed:` with `image` in the modalities and at
least one ZDR endpoint, or the guard will refuse to start later.

### 4.8 Start the guard and prove the Telegram flow

```bash
sudo systemctl enable --now hermes-tradie-assistant-openrouter-guard.service
curl -s http://127.0.0.1:18082/healthz   # expect {"ok":true,...}
```

Then run one **synthetic Telegram workspace flow** end to end (the required
proof that messages, session binding and media handoff work): create a test
workspace, save a lead, prepare and approve a quote from an allowlisted test
Telegram account. Only when that passes:

```bash
sudo touch /opt/hermes-agents/tradie-assistant/.telegram-ready
sudo systemctl enable --now hermes-gateway@tradie-assistant.service
```

### 4.9 First-customer policy

Onboard **one** real business first. Add a second only after reviewing the
cross-tenant behaviour and the first workspace's audit trail. Keep the
previous image, profile archive and a SQLite copy before any future upgrade.

---

## 5. Setting up each new customer (repeatable)

Once the server is running, each new trade business takes about 10 minutes.
Everything in this section is repeatable per customer — no redeployment.

### Step A — Allow their Telegram user ID (operator, on the server)

The Telegram allowlist is the outer server gate. Ask the business owner for
their Telegram user ID (they can get it from @userinfobot), then:

```bash
sudoedit /etc/hermes/tradie-assistant.env
# append the new ID to TELEGRAM_ALLOWED_USERS (comma-separated)
sudo systemctl restart hermes-gateway@tradie-assistant.service
```

### Step B — The owner creates their workspace (in Telegram)

The owner messages the bot:

```text
/setup Dave's Plumbing | gst=yes
```

- `gst=yes` if the business is GST-registered, otherwise `gst=no`.
- The bot replies with the workspace name and slug (e.g.
  `daves-plumbing-a1b2c3`). Note the slug — it is used for `/switch`.

### Step C — The owner saves their rate card (in Telegram)

```text
/revise ratecard labour=120 callout=90 minimum=150 travel=1.20 included_km=10 markup=20 after_hours=1.5 rounding=1 inclusive_gst=no
```

Every value is required:

| Field | Meaning | Example above |
| --- | --- | --- |
| `labour` | Hourly labour rate, dollars | $120/h |
| `callout` | Call-out fee, dollars | $90 |
| `minimum` | Minimum total charge, dollars | $150 |
| `travel` | Per-km travel rate, dollars | $1.20/km |
| `included_km` | Free kilometres before travel is charged | 10 km |
| `markup` | Materials markup, percent | 20% |
| `after_hours` | Labour multiplier for after-hours jobs | 1.5× |
| `rounding` | Round the quote up to this increment, dollars | $1 |
| `inclusive_gst` | `yes` if the figures above already include GST | `no` |

Each save creates a **new immutable version** — existing quotes never change.
GST handling: if the workspace is GST-registered and the rate card is
exclusive, 10% GST is added; if the rate card is inclusive, the GST component
is reported without adding it again. Non-registered workspaces get no GST.

### Step D — Invite staff (optional, in Telegram)

1. The owner runs `/invite staff` and receives a one-use, 24-hour token.
2. **Operator step:** add the staff member's Telegram user ID to
   `TELEGRAM_ALLOWED_USERS` and restart the gateway (as in Step A). The
   allowlist is the outer gate; the invite is the inner tenant membership.
3. The staff member sends `/setup invite=<TOKEN>` to the bot.

### Step E — Smoke-test the workspace (in Telegram)

Send one realistic enquiry and check the full loop:

```text
/new Hi, need a plumber in Fremantle to replace a hot water unit, about 2 hours work, roughly $350 in parts, during business hours
```

Then `/lead` to confirm it saved, `/quote <LEAD_ID>` to confirm deterministic
pricing, and `/approve <QUOTE_ID> v=1` to confirm the approval lock. If the
bot replies with `Needs: ...`, that is the system working as designed —
supply the missing facts with `/revise lead`.

### Per-customer checklist

- [ ] Owner's Telegram user ID added to `TELEGRAM_ALLOWED_USERS`, gateway restarted
- [ ] Owner ran `/setup <Business Name> | gst=yes|no`
- [ ] Owner ran `/revise ratecard ...` with all nine values
- [ ] Staff invited (`/invite staff`), allowlisted, and joined (`/setup invite=...`)
- [ ] Smoke test: `/new` → `/lead` → `/quote` → `/approve` all behave
- [ ] Owner shown `/export` and `/delete lead` for their data obligations

---

## 6. Day-to-day usage

**Capturing work:** forward or paste any enquiry after `/new`. Attach photos
in the same message if the deployment has image intake enabled. Duplicates
are detected by content fingerprint and return the existing lead instead of
creating a second one.

**What the statuses mean:**

- `ready` — everything needed for a quote is present; a draft was made.
- `needs_review` — something is missing; the reply lists exactly what
  (`Needs: ...`). Fix it with `/revise lead ...`.
- `manual_required` — an immediate-hazard phrase or emergency was detected.
  No quote is calculated; handle the safety issue directly.
- `quoted` / `approved` — a draft exists / a human locked version N.

**Quoting:** `/quote LEAD_ID` shows the latest draft or calculates one from
the current rate card and lead facts. Revising a lead with `/revise lead`
supersedes old drafts — approvals are pinned to an exact version
(`/approve QUOTE_ID v=N`), so a stale quote can never be approved
accidentally.

**Multiple businesses:** a user in several workspaces switches with
`/switch WORKSPACE_SLUG`. Every command then applies to that workspace only.

**Data duties:** the owner can pull everything with `/export` (a private JSON
file under `workspace/state/exports/`) and erase a customer's content with
`/delete lead LEAD_ID`, which also removes that lead's stored images. The
audit event recording the deletion is kept.

---

## 7. Operations and maintenance

**Backups.** The state to protect is the SQLite database and private media:

```bash
sqlite3 /opt/hermes-agents/tradie-assistant/workspace/state/tradie-assistant.sqlite3 ".backup /backups/tradie-$(date +%F).sqlite3"
sudo cp -a /opt/hermes-agents/tradie-assistant/workspace/state/media /backups/media-$(date +%F)
```

Automate this (e.g. a daily cron with retention) before onboarding customers —
no backup tooling ships in this repository yet.

**Media retention.** Private images are purged automatically after
`TRADIE_ASSISTANT_MEDIA_RETENTION_DAYS` (default 30) during ingestion, and
`/delete lead` removes a lead's images immediately.

**Provider budget.** The guard enforces per-minute, per-day call and output
token ceilings (`workspace/config/provider_guard.json`). When a ceiling is
hit, quoting pauses but leads are still saved. Raise the ceilings in the
config and restart the guard if a busy customer needs more headroom.

**Logs.** Both services log to the journal:
`journalctl -u hermes-tradie-assistant-openrouter-guard.service` and
`journalctl -u hermes-gateway@tradie-assistant.service`. The guard
deliberately never logs request content.

**Upgrades.**

1. Preserve the previous image, a copy of the profile and a SQLite backup.
2. Deploy the new profile, regenerate the manifest with
   `workspace/scripts/release_check.py --write release-manifest.json --tests passed`.
3. Re-run the unit tests and provider preflight.
4. Restart the guard, then the gateway.

**Adding another customer later.** Repeat section 5 only — no server changes
beyond the allowlist entry.

---

## 8. Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| Bot ignores a user | Their Telegram user ID is not in `TELEGRAM_ALLOWED_USERS`; add it and restart the gateway |
| Gateway service will not start | `/opt/hermes-agents/tradie-assistant/.telegram-ready` is missing, or the guard service is down |
| Guard service will not start | Preflight failed: the model lacks image input or a ZDR endpoint, or env values are missing/mismatched between the two env files |
| Every lead says "provider processing failed" | Guard not listening on 127.0.0.1:18082, or the guard token in the two env files differs |
| Quotes suddenly stop but leads still save | Daily provider-call or token ceiling reached; check `provider_guard.json` limits and the guard journal |
| "Rate card setup is incomplete" | The workspace has no `/revise ratecard` version yet |
| "Only the workspace owner can do that" | Rate cards, invites, exports and deletes are owner-only |
| Quote version conflict on approve | The lead or rate card changed; re-check `/quote LEAD_ID` and approve the new version number |

---

## 9. Known limitations (as of v0.1.0)

- No automated backup tooling, log-based monitoring, or schema migration
  framework ships in the repository — put backups in place yourself (section
  7) before real customers.
- The synthetic Telegram end-to-end pilot (deployment gate step 4.8) is a
  manual procedure, not an automated test in `workspace/tests/`.
- Telegram commands are not rate-limited; only provider calls are.
- Image intake depends on the Hermes gateway staging attachments into the
  profile inbox; until the synthetic pilot proves that handoff, have staff
  describe photos in text.
