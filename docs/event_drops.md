# Event Drops

Event Drops is a reusable campaign feature in **The Garden → Events → Event Drops**.
It shares the `events` module switch, SQLite database, Garden authentication,
Discord metadata snapshots, and operational logging. It has no reminder dependency
of its own and adds no runtime packages. Existing `/events` and `/remind event`
commands are unchanged.

## Configure a campaign

1. Open Event Drops and choose **Create campaign**.
2. Set its collectible, points per claim, embed, and button. Unicode and Discord
   custom emoji (`<:name:id>` / `<a:name:id>`) are accepted.
3. Optionally upload up to 10 JPEG, PNG, or WebP images, each at most 8 MiB. The
   existing Events validator strips metadata and converts artwork to a 1600×900
   WebP. One saved image is chosen per drop. Uploaded bytes live in a separate
   normalized asset table in the shared database, so the dashboard and bot do
   not require a shared filesystem or public asset host. Thumbnail URLs must use
   HTTPS. Removed images remain available to historical drop records.
4. Choose fixed or random timing and the claim window. Defaults are 60-minute
   fixed intervals, a 45–90 minute random range, and a 10-minute claim window.
5. Explicitly select individual text channels in the searchable category-labelled
   picker. Categories are only visual labels. Future channels are never enrolled
   automatically. The bot must have View Channel, Send Messages, Embed Links,
   and Read Message History; image campaigns additionally need Attach Files.
   Deleting its own messages does not require Manage Messages. Buttons need no
   separate permission. The bot rechecks permissions before each send.
6. Leave role rules and maximums empty for all human members and unlimited
   campaign points/drops. Eligible roles use “any”; excluded roles take precedence.
   A user maximum must permit the entire configured award, with no partial awards.
7. Save, review the preview, then start. Times display in `SERVER_TIMEZONE`
   (default America/Chicago); scheduling and claim timestamps are UTC epoch values.

A manual start waits one interval for the first automatic drop. A future start
schedules the first drop at that start time. Random intervals are drawn after
successful automatic delivery. **Send Drop Now** can target a random eligible
channel or one specific allowlisted channel, including while paused. It leaves
the automatic schedule alone. The default recent-channel avoidance is three;
if needed, the oldest exclusions are relaxed first.

**Pause** stops future automatic drops, cancels unsent queued work, and lets
posted drops finish. Editing requires draft or paused status. Existing drops
retain their award amount; edited role rules and campaign limits apply to future
claims. **Resume** starts a fresh interval. **End**, confirmed by typing `END`,
permanently completes the campaign and closes live drops. Scheduled end does the
same. History and scores remain available. Only unused drafts can be deleted;
duplication creates a fresh draft with configuration/images but no scores or dates.

## Discord commands

- `/event score [campaign_id]`: ephemeral personal total and rank.
- `/event leaderboard [campaign_id]`: public top 10 without member pings.
- `/eventdrop status|drop|pause|resume [campaign_id]`: lightweight administration
  for configured owners/admin roles or Discord administrators.

Commands default to the current active/paused campaign, or the most relevant
historical/scheduled campaign when none is running. An ID selects a specific
campaign in the same guild. All eligible humans can collect each live drop once;
there is no first-winner or global claimer cap. Successful and duplicate claim
responses are ephemeral. Scores come from individual claim rows.

## Authorization and results

All Event Drops pages, operations, artwork, detailed claimers, and CSV exports
require `event_drops.manage`. Owners/Administrators receive it through the existing
permission catalog. Grant it to an appropriate existing role through **Admin
Dashboard → Access** when Party Captains should operate campaigns. Ordinary
Verified Member event access does not expose participant results or editing.
No new role system or automatic expansion of Party Captain access is introduced.

The campaign page shows worker readiness, last/next drop, live messages, pending
or uncertain delivery, claims, history (50 drops per page), and participants.
Use **Refresh status** to refresh its snapshot. Participant names are refreshed
from the bot's cached, fully loaded guild membership every five minutes; users
who left retain their IDs and scores. CSV includes ID, display name, points,
claim count, and rank, with formula-leading display names escaped.

## Reliability and architecture

- `utils/event_drops.py`: schema, validation, CRUD, transitions, scheduling,
  reservations, claims, and totals. Each transaction uses a dedicated SQLite
  connection with the project's busy timeout and `BEGIN IMMEDIATE`, avoiding
  commits interleaved with unrelated cogs on the bot's shared connection.
- `cogs/event_drops.py`: 15-second worker, Discord channel checks, embeds,
  persistent `DynamicItem` buttons, message cleanup, and command adapters.
  Dynamic components register during cog loading before the worker starts;
  reconnects do not create extra loops. Active buttons route by durable drop ID.
- `dashboard/event_drops_routes.py`: authenticated/CSRF-protected Garden forms,
  queued manual sends, safe asset delivery, results, exports, and meaningful
  dashboard audit entries. No Discord client runs in the dashboard.
- `event_drop_campaigns`, `event_drop_channels`, `event_drop_roles`,
  `event_drop_assets`, `event_drops`, `event_drop_claims`, `event_drop_members`,
  `event_drop_worker`, and `event_drop_schema` are additive tables. Unique
  `(campaign_id, scheduled_at)`, manual request keys, and `(drop_id, user_id)`
  protect occurrences, repeated form submissions, and claims respectively.

A due automatic occurrence is reserved and its next time advanced in the same
transaction. Only one worker can change a pending drop to sending. The library
supplies an enforced stable `eventdrop:<id>` nonce to Discord. Definitive
Forbidden/NotFound rejections can try the next allowlisted channel; uncertain
network results are **never resent**. Reconciliation finds the original bot
message by its unique footer marker. It scans at most 500 messages per pass and
persists its cursor, continuing after five minutes in busy channels. Unknown
receipts remain visible as sending/uncertain until found. This deliberately
favors avoiding duplicates over replaying an uncertain occurrence.

At startup, overdue schedules are skipped, and old unsent automatic reservations
are marked missed. Fixed schedules advance directly to the next future slot;
random schedules draw a future interval. A delayed tick more than 90 seconds
late also skips the occurrence. No backlog is replayed. Ordinary send failures
retain the next interval and a warning; an exception never kills the worker.
Maximum-drop reservations count against the cap before sending; failed/missed
attempts do not consume it, while uncertain attempts remain reserved.

Claims check expiration after acquiring the transaction lock, independent of
whether cleanup has run. Cleanup normally deletes messages within one worker
tick (15 seconds) of expiration; Discord outages can delay deletion, but cannot
extend claim eligibility. Already-deleted messages count as successful cleanup.
Other deletion failures retain the record and retry in five minutes. Claims and
cleanup share the same transactional state. Audit channels do not receive one
message per claim; claim history is stored in SQLite.

## Raspberry Pi deployment

Use the existing service/deployment procedure. No service-unit changes or new
secrets are needed. Bot and dashboard must point to the same persistent
`DATABASE_PATH`; include `events` in `ENABLED_MODULES` when an explicit list is
used. The existing Events Hub also needs `reminders` for its own functionality.

Before upgrading the live database, stop both services during the normal
maintenance window and run the additive migration with an SQLite online backup:

```bash
.venv/bin/python scripts/migrate_event_drops.py \
  --database /absolute/path/to/data.db \
  --backup-dir /absolute/path/to/backups
.venv/bin/python scripts/migrate_event_drops.py \
  --database /absolute/path/to/data.db --validate-only
```

Bot and dashboard startup also apply the same idempotent schema initializer.
Migration uses only new tables/indexes, records version 1, and preserves existing
feature data. Keep the existing Python environment and pinned discord.py 2.7.1;
Python 3.11+ remains recommended by the main README.

Restart the services and check logs for successful cog loading/command sync, then
check worker readiness and live channel metadata in The Garden. Create a small
test campaign with an explicitly selected test channel, claim with two members,
retry a claim, restart during a live drop, and verify expiry/cleanup before
starting the public campaign. The automated tests mock Discord transport;
real guild permissions and production service health require this live check.

## Verification

```bash
.venv/bin/python -B -m unittest discover -s tests -p test_event_drops.py -v
.venv/bin/python -B -m unittest discover -s tests
```

Tests cover races across independent SQLite connections, user caps, lifecycle,
downtime, competing workers, persistent components, channel filtering, uncertain
sends/recovery, expiration/deletion, migrations, and protected Garden workflows.
