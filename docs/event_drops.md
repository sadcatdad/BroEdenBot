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
retain their award amount, resolved appearance, image, and claim window; edited
role rules and campaign limits apply to future
claims. **Resume** starts a fresh interval. **End**, confirmed by typing `END`,
permanently completes the campaign and closes live drops. Scheduled end does the
same. History and scores remain available. Only unused drafts can be deleted;
duplication creates a fresh draft with configuration/images but no scores or dates.

## Drop Variants (Rare Drops)

In a saved campaign, open **Drop Variants** or the Drop Variants section of its
editor. **Enable Drop Variants** creates an enabled Default Variant using the
campaign's current point value and inherited appearance. Standard campaigns
remain unchanged until this is enabled. Variant edits follow the existing
configuration lock: pause active/scheduled campaigns first, and duplicate a
completed campaign to reuse its setup. Save changes to standard campaign settings
before opening a variant editor.

Each variant has a name, optional freeform rarity label, positive selection
weight, whole-number reward, enabled status, display order, and optional
appearance overrides. One enabled Default Variant is required while variants
are on. Relative weights can total any positive value: weights 90/8/2 give
90%/8%/2%; weights 50/5/1 give about 89.3%/8.9%/1.8%. Each automatic or random
manual drop makes an independent weighted draw, so these are probabilities,
not a guaranteed sequence. Disabled and deleted variants are excluded.
NaN, infinity, negative weights, and enabled zero-weight variants are rejected.
Rarity labels and colors never affect selection logic.

Use the override checkboxes to replace only selected campaign appearance fields:
emoji, embed title/body/color, thumbnail, and button label/emoji/style. Unchecked
fields inherit their campaign settings. A checked, empty description, thumbnail,
or emoji override intentionally clears that field. **Show reward value in drop**
defaults on for new variants; **Show rarity label on drop** defaults off. Standard
drops without variants retain their original appearance without a reward line.

Image behavior may inherit the campaign pool, use one specific image, draw from
a variant image pool, or use no image. Select existing campaign assets or upload
through the same safe Events image validator. Variant uploads are stored in
`event_drop_assets` with `campaign_pool=0`, so they cannot appear in standard
campaign drops accidentally. Associations live in `event_drop_variant_assets`;
no image bytes are stored in variant rows. Pools support up to ten images and
campaigns support up to thirty non-deleted variants. Weights are finite values
up to one billion; rewards remain 1–1,000,000 points per claim.

The live preview selector shows saved variants with inheritance applied. The
variant editor previews unsaved overrides, reward visibility, image choices, and
normalized probabilities as you change fields. On an active or paused campaign's
detail page, **Send Drop Now** draws from campaign weights. **Rare Drop Now**
requires you to choose an enabled variant (for example, Golden Fish or Legendary
Salmon) and always sends that variant, bypassing the weighted draw. Choose a
random eligible destination or a specific allowlisted channel. Both controls
leave the automatic schedule unchanged. Enable Drop Variants first to use
Rare Drop Now; disabled variants cannot be forced.
History records random versus forced selection; claim eligibility, one claim
per member, unlimited different claimers, campaign point limits, expiration,
and persistent Discord buttons use the same Event Drops flow. There is no
separate rare-drop leaderboard or claim-winner rule. Full-award limits still
apply: a member with three points of capacity cannot claim a five-point drop.

At reservation, each drop stores its variant ID, name, rarity, selected image,
and exact point value. `event_drop_snapshots` freezes resolved appearance,
collectible names, reward/rarity visibility, and the claim-window duration.
The expiration timestamp is assigned on the first send attempt and retained
across channel fallback. Later campaign/variant edits, disabling, or deletion
do not change an existing drop's snapshot, message, or rewards. Claim rows
continue storing actual awarded points; confirmations name the selected variant,
show points earned, and report the updated campaign total.

**Duplicate** copies a variant's settings and image associations under a new ID.
Duplicating a campaign copies all variant configuration and remaps its assets,
but copies no claims, drops, or dates. Unused variants can be deleted; used
variants are soft-deleted and retained through historical references. Disable
instead if you may want to re-enable the variant. Select another enabled default
before disabling/deleting the current default, or switch the campaign back to
Standard Drops. Switching modes retains variant configuration for later reuse.

The existing history table shows variant, rarity, points per claim, and forced
manual selection. Open a participant's name to inspect claims and points by
variant. **Variant results & detailed exports** contains delivered-drop counts,
claims, awarded points, and average claims (including zero-claim drops). Grouping
uses the names/rarity recorded at drop creation; renamed variants may therefore
appear on separate historical rows. The existing participant CSV columns stay
unchanged. Detailed claim CSV adds campaign, drop, variant ID/name, rarity,
channel, user, points, and UTC claim time; drop CSV includes the selected variant,
reward, manual/automatic mode, selection method, posting time, claims, and status.
All variant routes, assets, participant details, and exports retain
`event_drops.manage`, CSRF protection for mutations, and safe CSV escaping.

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
- `utils/event_drop_variants.py`: reusable weighted selection, appearance
  inheritance, and the additive version 2 migration.
- `cogs/event_drops.py`: 15-second worker, Discord channel checks, embeds,
  persistent `DynamicItem` buttons, message cleanup, and command adapters.
  Dynamic components register during cog loading before the worker starts;
  reconnects do not create extra loops. Active buttons route by durable drop ID.
- `dashboard/event_drops_routes.py`: authenticated/CSRF-protected Garden forms,
  queued manual sends, safe asset delivery, results, exports, and meaningful
  dashboard audit entries. No Discord client runs in the dashboard.
- `event_drop_campaigns`, `event_drop_channels`, `event_drop_roles`,
  `event_drop_assets`, `event_drops`, `event_drop_claims`, `event_drop_members`,
  `event_drop_worker`, `event_drop_schema`, `event_drop_variants`,
  `event_drop_variant_assets`, and `event_drop_snapshots` are additive tables. Unique
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
Version 2 adds `event_drop_variants`, `event_drop_variant_assets`, and
`event_drop_snapshots`; campaign/asset/drop columns and variant indexes are added
in one SQLite transaction. The original version 1 schema and all feature data
are retained. Existing campaigns default to `variants_enabled=0`, legacy drops
are labelled Standard Drop, and stored claim/drop points and image references
are untouched. Version 1 did not record full historical appearance, so the
migration freezes its best-known campaign appearance for those legacy rows;
it does not edit their Discord messages. Migration retries are idempotent.
Stop both old processes before migrating and restart both on the new code;
version 1 workers do not write the snapshots required by version 2. Keep the existing Python environment and pinned discord.py 2.7.1;
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
.venv/bin/python -B -m unittest discover -s tests -p test_event_drop_variants.py -v
.venv/bin/python -B -m unittest discover -s tests
```

Tests cover races across independent SQLite connections, user caps, lifecycle,
downtime, competing workers, persistent components, channel filtering, uncertain
sends/recovery, expiration/deletion, migrations, and protected Garden workflows.

For a live Phase 2 smoke test, enable Normal/Golden/high-value variants in an
explicit test channel, force each through Drop Now, and confirm 1 + 5 + 10
produces a 16-point total. Restart with a rare drop still live, then claim it
and verify expiry. Pause, edit the variant reward, and verify an older live drop
retains its original award while the next drop uses the new reward. Verify the
participant breakdown and detailed exports before launching a public campaign.
