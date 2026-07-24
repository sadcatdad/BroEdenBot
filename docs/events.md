# Events in The Garden

## Purpose and boundaries

`/events` is the private, Discord-authenticated **Events** area in The Garden
for current verified
Bro Eden members. It mirrors Discord Scheduled Events; it does not replace
Discord's Interested RSVP. **Open in Discord** is the native RSVP path, while
**DM Reminders** uses BroEdenBot's existing canonical reminder service.

The existing Discord `/events`, `/remind event`, and `/remind subscriptions`
commands are unchanged. The `events` module adds gateway refreshes, a
15-minute reconciliation, and a queued dashboard action worker. FastAPI reads
and writes only the shared SQLite database and never calls Discord or receives
the bot token.

## Access setup

1. Enable Discord OAuth with `identify guilds.members.read` and an exact OAuth
   callback URL.
2. In **Admin Dashboard → Access**, map the server's Verified role to **Verified Events
   Member**. This role contains only `events.view` and `events.subscribe`.
3. Map the Party Captain Discord role to **Party Captain**. It adds
   `events.create` and `events.edit_own`.
4. Owner and Administrator accounts may manage every one-time event. Captains
   may manage only events they submitted through The Garden or Discord events they
   personally created. Every edit/cancel handler re-checks ownership.
5. Anonymous visitors, non-members, pending membership-screening accounts, and
   members without a mapped role are rejected before event data is loaded.

After a Discord role change, sign out and complete Discord login again. Normal
session re-verification also expires according to
`DASHBOARD_DISCORD_REVERIFY_MINUTES`.

## Discord permissions and types

Grant BroEdenBot **Create Events** and **Manage Events**. It also needs access
to the selected Stage or Voice channel (normally **View Channel** and
**Connect**). The authoring form accepts:

- Stage: an existing Stage channel.
- Voice: an existing Voice channel.
- External: a location and required end time.

All are one-time events with a name, optional description, start, optional end,
and optional cover. In **Features → Events**, select an existing private forum,
thread, or text channel as **Event Artwork Storage**. BroEdenBot needs **View
Channel**, **Attach Files**, and the applicable **Send Messages** or **Send
Messages in Threads** permission there. JPEG, PNG, and WebP files are limited
to 8 MiB and safely normalized to a bounded 1600 × 900 WebP. The live bot posts
the normalized image to that destination; forum destinations receive an
automatically named post. The dashboard and downstream renderers use the saved
Discord attachment URL, while temporary bytes remain in SQLite only while the
action is pending/retrying and are cleared after success or permanent failure.
Retries reuse the recorded Discord upload receipt rather than creating a second
forum post. Reconciliation refreshes the attachment URL from its source message
so signed Discord links remain current.
Recurring Discord events are mirrored and subscribable but read-only.
For Discord-created events, reconciliation resolves the creator against the
guild member list and displays their current server nickname when available,
falling back to their Discord display name when no nickname is set or the member
is not cached. Existing mirrored organizer labels refresh when the nickname
changes.

BroEdenBot is the Discord-visible creator for events published through The Garden. The
human Captain is retained as the organizer in the event ownership table, UI,
description credit, action history, and audit trail.

## Synchronization and reminders

Gateway create/update/delete events request an immediate guild refresh. A
15-minute reconciliation recovers missed gateway notifications. Each Discord
event is upserted with a stable `discord_scheduled_event` source key into
`reminder_items`; existing occurrence/subscription/delivery code remains the
only reminder implementation. Reschedules rebuild pending deliveries.
Cancellation or removal cancels future deliveries without deleting history.

Quick Subscribe selects 15 minutes before and start time. Members can customize
6 hours, 1 hour, 15 minutes, and start time in any combination. A private
confirmation DM is queued after a new website subscription. If DMs are blocked,
the subscription remains active and the failed confirmation is visible in
publishing activity/readiness. Delivery retries and operator recovery continue
through the canonical reminder service.

## Storage and action safety

Additive tables are `dashboard_scheduled_events`,
`dashboard_event_ownership`, `dashboard_event_artwork`, `event_dashboard_actions`, and
`dashboard_event_sync_status`. No legacy parallel RiffSupremo reminder tables
are imported. Action idempotency keys prevent browser resubmission from
duplicating Discord writes. Temporary Discord/API failures retry three times;
validation, permission, and terminal failures remain visible. Cancel changes
Discord's event status after typed confirmation; there is no hard-delete UI.

Event pages and status responses require role checks and send `private,
no-store` caching headers. Eligible channel choices are server-rendered from
the existing metadata snapshot; Verified members are not granted the general
Discord metadata API.

## Migration and validation

Before a production upgrade, use an SQLite-aware backup. Then run:

```bash
.venv/bin/python scripts/migrate_events.py --database /path/to/data.db --backup-dir /path/to/backups
.venv/bin/python scripts/migrate_events.py --database /path/to/data.db --validate-only
.venv/bin/python -m unittest -q tests.test_events_hub tests.test_dashboard_rbac tests.test_dashboard_feature_access tests.test_module_loader
sqlite3 /path/to/data.db "PRAGMA quick_check;"
```

Set `ENABLED_MODULES` to retain existing values and include both `events` and
`reminders`. Blank continues to load every cog. The Events readiness panel
reports module/dependency state, role mappings, live sync age, Discord event
permissions, artwork-storage selection/write readiness, eligible channel count,
queued actions, and failures.

## Manual acceptance

1. Sign in as a Verified-only member; verify `/events` works while authoring,
   Settings, and metadata APIs return 403.
2. Quick Subscribe, customize all timing combinations, unsubscribe, and
   resubscribe. Confirm Discord Interested count does not change.
3. Select a private artwork forum in **Features → Events**. As a Captain,
   publish one Stage, Voice, and external event; verify the auto-created artwork
   post, Discord fields/artwork, The Garden image source, and human organizer
   label. Retry a temporary failure and confirm it does not duplicate the post.
4. Edit/reschedule and confirm pending reminder deliveries are rebuilt. Cancel
   after typing `CANCEL` and confirm future deliveries are cancelled.
5. Attempt to edit another Captain's event and a recurring event; both must
   fail closed. Confirm Owner/Administrator can manage the one-time event.
6. Block bot DMs and subscribe; confirm the subscription remains while the
   confirmation action reports its failure.
7. Stop the bot, queue a dashboard action, restart it, and verify recovery.
   Temporarily remove event permissions to exercise bounded retry/failure.

Rollback is code-only: restore the prior revision while keeping the additive
tables. Existing Discord events and canonical reminders continue to exist; do
not delete the new tables during an incident.
