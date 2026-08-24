# Hermes Tradie Assistant

Deployment-ready scaffold for a private, multi-business Hermes Telegram bot
that saves forwarded leads, extracts structured job details, calculates quote
drafts from versioned business rate cards and requires human approval.

This repository contains no API keys, Telegram tokens or customer data.

See [WALKTHROUGH.md](WALKTHROUGH.md) for what the system does and the
step-by-step guide to installing the server and onboarding each customer.

## Safety and privacy boundaries

- Every database query is scoped through the Telegram user's active workspace.
- Workspace owners invite staff with one-use, expiring tokens.
- Raw leads are committed to SQLite before any model call. Provider failure
  leaves the lead in `needs_review`; it cannot silently disappear.
- Pricing is deterministic. The model extracts quantities and writes wording,
  but cannot select hidden rates or perform the quote arithmetic.
- Immediate hazard phrases, multiple jobs and incomplete quantities prevent
  automatic quote creation.
- `/approve` locks an exact quote version. It never sends a customer message,
  confirms a booking or promises availability.
- The OpenRouter guard binds to loopback, fixes the model from
  `OPENROUTER_MODEL`, injects `zdr: true` and `data_collection: "deny"`, disables
  provider fallbacks, enforces usage ceilings and logs no request content.
- The guard refuses to start unless the exact model has text/image capability
  and appears in OpenRouter's current ZDR endpoint list.
- OpenRouter still records request metadata. This design prevents provider
  prompt retention; it is not a zero-telemetry system.

## Runtime shape

```text
Private Telegram users
  -> Hermes gateway container
      -> tenant-aware command hook
          -> SQLite + private image storage
          -> deterministic pricing and quote versions
      -> loopback OpenRouter guard
          -> exact configured multimodal model on a ZDR endpoint
```

The dedicated container mounts only this profile read-write and the shared
Hermes runtime read-only. It has no Docker socket and publishes no web port.

## Product commands

```text
/setup Business Name | gst=yes
/setup invite=INVITE_TOKEN
/revise ratecard labour=120 callout=90 minimum=150 travel=1.20 included_km=10 markup=20 after_hours=1.5 rounding=1 inclusive_gst=no
/revise lead LEAD_ID hours=2 travel_km=15 materials=80 after_hours=no service=plumbing
/new paste or forward the enquiry here
/lead
/lead LEAD_ID
/quote LEAD_ID
/approve QUOTE_ID v=N
/invite staff
/switch WORKSPACE_SLUG
/export
/delete lead LEAD_ID
/help
```

Rate-card dollar values are converted to integer cents. GST registration is a
workspace setting; the rate card separately records whether its inputs already
include GST. Every rate-card save creates a new immutable version.

## Required server secrets and settings

Create two root-owned files with mode `600`:

- `/etc/hermes/tradie-assistant.env` starts from `.env.example`. Its
  `OPENROUTER_API_KEY` is a random local guard token, not the provider key.
- `/etc/hermes/tradie-assistant-openrouter.env` starts from
  `.openrouter-guard.env.example`. It contains the real provider key and the
  same random token in `TRADIE_ASSISTANT_GUARD_TOKEN`.

The gateway file contains:

```text
OPENROUTER_API_KEY=random-local-guard-token
OPENROUTER_MODEL=exact-author/model-slug
OPENROUTER_BASE_URL=http://127.0.0.1:18082/v1
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=comma-separated-Telegram-user-IDs
```

The real OpenRouter key remains in the host-side guard process and is not
mounted into the Hermes container.

An invited staff member must also be added to `TELEGRAM_ALLOWED_USERS` before
the gateway will accept their messages. The Telegram allowlist is the outer
server gate; the invite controls their tenant membership inside the app.

For image intake, Hermes must stage Telegram attachments into the profile's
private `/workspace/inbox` mount and pass the resulting media path to the hook.
The synthetic Telegram pilot is the required proof of that runtime handoff;
until it passes, images remain local and staff should add a manual description.
Private image files expire after `TRADIE_ASSISTANT_MEDIA_RETENTION_DAYS` (30 by
default), and deleting a lead removes its stored image files immediately.

The OpenRouter model must be an exact slug. `openrouter/auto`, floating aliases
and model fallbacks are rejected. Set OpenRouter account privacy controls to
disable prompt logging and input/output use as a second boundary.

## Local validation

No provider key is needed for the unit tests:

```bash
PYTHONPATH=workspace/src python3 -m unittest discover -s workspace/tests -v
python3 -m compileall -q workspace/src workspace/scripts hooks
```

With a privately supplied key and exact model, run the read-only provider
preflight before deployment:

```bash
python3 workspace/scripts/provider_preflight.py --config workspace/config/provider_guard.json
```

## Contabo deployment gate

1. Create `/opt/hermes-agents/tradie-assistant` and a dedicated
   `hermes-tradie-assistant` host user.
2. Copy the tested profile and record its release manifest.
3. Install both root-owned environment files without printing their values.
4. Build `nadirstack/hermes-tradie-assistant:0.1.0` from `container/Dockerfile`.
5. Install the guard unit and the profile-specific Hermes gateway drop-ins.
6. Run the provider preflight, local database tests and one synthetic Telegram
   workspace flow.
7. Create `.telegram-ready`, then start only the guard and this gateway.
8. Onboard one real business first. Add another only after cross-tenant tests
   and the first workspace audit are reviewed.

No DNS, Traefik route or public port is required. Preserve the previous image,
profile archive and SQLite copy before future upgrades.
