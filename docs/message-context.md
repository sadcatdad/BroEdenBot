# Full-server message context archive

BroEdenBot's message context archive is a private staff moderation tool that
stores live Discord message content in a separate `message_context.db`
database. Authorized staff can search stored messages, reconstruct timelines,
and ask Gemini for neutral, evidence-based recaps of selected timeframes.

This is not the activity-stats system. Activity stats store message counts
only. Public `/ask` never opens or searches the message-context database.

## Privacy and access

This feature stores message content. Leave it disabled until server leadership
has approved the channels, access roles, retention policy, and member-facing
privacy disclosures that apply to the server.

Only users listed in `BOT_OWNER_USER_IDS` or members with a role listed in
`MESSAGE_CONTEXT_ALLOWED_ROLE_IDS` can use `/context`. Every command response
is ephemeral except a successful `/context user` evaluation, which the
authorized invoker deliberately posts to the current channel. An empty
allowed-role list does not grant administrators access; only configured bot
owners remain authorized.

The public `/context user` card includes a community-contribution score
(0–100), observed strengths, constructive growth opportunities, and up to five
representative verbatim quotes from the selected member with their archived
channel/date/message links. Quotes from NSFW-marked channels, including NSFW
content, may be shown publicly. It never posts secrets, staff-only concerns,
moderation history, or content written by other members.

The archive stores text and message metadata. It does not download attachment,
image, or video files. It stores attachment counts and filenames only.

## Configuration

```dotenv
MESSAGE_CONTEXT_ENABLED=false
MESSAGE_CONTEXT_CHANNEL_IDS=
MESSAGE_CONTEXT_EXCLUDED_CHANNEL_IDS=
MESSAGE_CONTEXT_ALLOWED_ROLE_IDS=
MESSAGE_CONTEXT_DB_PATH=message_context.db
MESSAGE_CONTEXT_TRACK_DELETES=true
MESSAGE_CONTEXT_TRACK_EDITS=true
MESSAGE_CONTEXT_IGNORE_BOTS=true
MESSAGE_CONTEXT_RETENTION_DAYS=
```

- `MESSAGE_CONTEXT_ENABLED` defaults to `false`. No live content is stored
  while it is false.
- Leave `MESSAGE_CONTEXT_CHANNEL_IDS` empty to track every visible guild text,
  thread, and forum-post message except excluded channels. When populated,
  only listed channels and their threads are tracked.
- `MESSAGE_CONTEXT_EXCLUDED_CHANNEL_IDS` always wins over the include list.
  A listed forum or text parent also excludes its threads.
- `MESSAGE_CONTEXT_ALLOWED_ROLE_IDS` is the staff command allowlist. Bot owners
  are also allowed.
- `MESSAGE_CONTEXT_DB_PATH` defaults to `message_context.db`.
- Edit and delete tracking default to enabled. Deletes mark captured rows;
  they do not erase the stored row.
- Bot messages are ignored by default. Webhook messages are always ignored for
  now.
- Leave `MESSAGE_CONTEXT_RETENTION_DAYS` empty for indefinite retention. A
  positive number enables daily pruning of older rows.

New live and CSV-imported content has obvious token, password, API-key, secret,
and bearer-authorization patterns redacted before storage. Retrieval applies
the filter again for older rows. Untrusted non-Discord URLs are not rendered as
jump links or supplied to Gemini as source links.

Optional Gemini model overrides are `MESSAGE_CONTEXT_MODEL` and
`MESSAGE_CONTEXT_FALLBACK_MODEL`. Otherwise the archive uses the configured
ModAI models and then its built-in defaults.

## Required Message Content Intent

In the Discord Developer Portal:

1. Open the BroEdenBot application.
2. Open **Bot**.
3. Enable **Message Content Intent** under privileged gateway intents.
4. Save the change.
5. Restart or deploy BroEdenBot.
6. Run `/context status`, send a test message in a tracked channel, and run
   `/context status` again. It should report that message content was observed.

The intent is requested in `main.py`, but the Developer Portal setting is also
required. If content is unavailable, the cog logs a warning and continues
without crashing.

## Commands

- `/context help` explains the private archive and its capture boundary.
- `/context status` shows enabled state, observed intent status, tracking mode,
  include/exclude counts, database size, source counts, date coverage, FTS,
  retention, edit tracking, and delete tracking.
- `/context search query:<text> [channel] [user] [after] [before] [source]
  [limit]` performs deterministic local FTS5 or LIKE search. It returns short
  excerpts, timestamps, channels, authors, source labels, and jump links.
- `/context summarize after:<time> [before] [channel] [topic] [style]
  [include_links]` produces a neutral staff recap.
- `/context timeline after:<time> [before] [channel] [topic] [granularity]`
  builds a chronological narrative.
- `/context user user:<member> timeframe:<24h|3d|7d|14d|30d|60d|90d>
  [channel] [include_bots] [max_messages]` is an authorized-staff command
  that posts a public, high-level evaluation of that member's stored
  participation. It uses a 0–100 community-contribution score based on
  observable activity, plus strengths, constructive growth opportunities, and
  up to five representative verbatim quotes with their source links. NSFW
  quotes are permitted; staff/moderation information is not.
- `/context channel channel:<channel> timeframe:<1h|6h|12h|24h|3d|7d|14d|30d>
  [topic] [include_bots] [max_messages]` reviews a stored channel timeframe.

Dates accept ISO dates/times plus `yesterday` and `today`. Examples:

```text
/context summarize after:2026-06-21 20:00 before:2026-06-21 23:00
/context timeline after:yesterday before:today
/context search query:"NSFW access"
```

Gemini commands retrieve locally first, cap the number of rows, summarize
bounded chunks, and combine those chunk summaries. The database is never sent
wholesale. Large selections report that retrieval was capped and should be
narrowed for finer detail.

## Historical CSV import

Place DiscordChatExporter CSV files under `imports/message_context/`. The
importer supports the standard `AuthorID`, `Author`, `Date`, `Content`,
`Attachments`, and `Reactions` columns plus common alternate names. Reactions
are ignored. If an export has no message ID, the importer creates a
deterministic fallback ID.

Dry run:

```bash
.venv/bin/python scripts/import_message_context.py \
  --folder imports/message_context \
  --guild-id YOUR_GUILD_ID \
  --dry-run
```

Real import:

```bash
.venv/bin/python scripts/import_message_context.py \
  --folder imports/message_context \
  --guild-id YOUR_GUILD_ID \
  --archive-completed
```

Use `--channel-id` and `--channel-name` when they cannot be inferred from the
export or its filename. `--archive-duplicates` also archives files containing
only already-imported rows. Failed files stay in place. The importer skips
`archive`, `local_archive`, `broken`, and `repaired` folders.

### Full CSV export strategy

Use the combined terminal importer for full-server CSV coverage:

```bash
.venv/bin/python scripts/import_full_csv_exports.py \
  --folder imports/message_context \
  --guild-id YOUR_GUILD_ID \
  --dry-run

.venv/bin/python scripts/import_full_csv_exports.py \
  --folder imports/message_context \
  --guild-id YOUR_GUILD_ID \
  --archive-completed \
  --archive-duplicates
```

Every CSV contributes content to private `message_context.db`. The same CSV
contributes counts-only `csv_backfill` activity only if its channel ID has not
already been imported successfully from JSON. IDs come from CSV columns or the
filename’s final `[channel_id]`; unknown-ID files remain context-only.

Use `--context-only` to omit activity. `--force-activity` overrides JSON
coverage detection and may duplicate totals. Public `/ask` never uses this
archive.

## Operational test

Use a test configuration such as:

```dotenv
MESSAGE_CONTEXT_ENABLED=true
MESSAGE_CONTEXT_CHANNEL_IDS=
MESSAGE_CONTEXT_EXCLUDED_CHANNEL_IDS=
MESSAGE_CONTEXT_ALLOWED_ROLE_IDS=YOUR_STAFF_ROLE_ID
MESSAGE_CONTEXT_IGNORE_BOTS=true
MESSAGE_CONTEXT_TRACK_EDITS=true
MESSAGE_CONTEXT_TRACK_DELETES=true
```

Then:

1. Run `/context status`.
2. Send a test message in a normal visible channel.
3. Run `/context search query:<part of the test message>`.
4. Run `/context summarize after:<start> before:<end>`.
5. Run `/context timeline after:<start> before:<end>`.
6. Dry-run and then perform a CSV import.
7. Run `/context search source:imported_csv query:<known imported text>`.

## Limitations

- Messages sent before tracking was enabled are unavailable unless imported.
- The bot cannot capture channels it cannot see.
- Content cannot be captured without Message Content Intent.
- Deleted messages can be marked deleted only if they were captured first.
- Edit history is not retained; only the latest captured content is stored.
