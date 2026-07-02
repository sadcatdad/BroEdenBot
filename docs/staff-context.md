# Private staff context

Staff context is a separate, staff-only store of message content from approved
staff channels. It combines:

- historical DiscordChatExporter CSV imports tagged `imported_csv`
- new allowlisted Discord messages captured after deployment and tagged
  `live_discord`

Both sources use `staff_context.db` and are available only to `/staffai`.
Historical activity statistics remain counts-only in `data.db`; their importer
does not store content. Public `/ask` never opens or searches staff context.

## Privacy warning

This feature stores staff message content. Keep `staff_context.db`, CSV exports,
and staff-context exports on trusted systems. Do not commit or publicly share
them. Git ignores:

```text
staff_context.db
*.db
*.sqlite
imports/staff_context/
exports/staff_context/
```

Live capture is disabled by default and only stores messages from explicitly
configured channel IDs. It ignores DMs, bot messages, webhook messages, and
unconfigured channels. Attachment files are never downloaded; only count and
filename metadata may be stored. Obvious token or environment-secret patterns
are redacted before live or CSV content is stored.

Gemini never receives the whole database. BroEdenBot retrieves a small local
result set, caps context size, and asks Gemini to summarize rather than dump
logs. Staff discussion is historical context, not automatically official
policy; confirm policy against the Rules and Survival Guide.

StaffAI may also retrieve relevant sections from configured staff-only live
Discord knowledge sources. Public `/ask` cannot load staff-only knowledge.

## Environment variables

```dotenv
STAFF_CONTEXT_ENABLED=false
STAFF_CONTEXT_CHANNEL_IDS=
STAFF_CONTEXT_DB_PATH=staff_context.db
STAFF_CONTEXT_TRACK_DELETES=true
STAFF_AI_ALLOWED_ROLE_IDS=
```

- `STAFF_CONTEXT_ENABLED` enables live capture only when `true`. Default:
  `false`.
- `STAFF_CONTEXT_CHANNEL_IDS` is a comma- or space-separated allowlist of
  staff channel IDs. These channels and their threads are captured.
- `STAFF_CONTEXT_DB_PATH` sets the private database path. Relative paths are
  resolved from the project root.
- `STAFF_CONTEXT_TRACK_DELETES` marks stored live messages deleted when Discord
  reports deletion. Default: `true`. It does not hard-delete context.
- `STAFF_AI_ALLOWED_ROLE_IDS` grants `/staffai` access to configured roles.
- `BOT_OWNER_USER_IDS` also grants `/staffai` access.

Optional Gemini model overrides:

```dotenv
STAFF_AI_MODEL=gemini-2.5-flash
STAFF_AI_FALLBACK_MODEL=gemini-2.0-flash
```

Administrator permission alone does not grant staff-context access.

## Choosing tracked channels

Enable live capture only for channels whose staff understand that content will
be retained:

```dotenv
STAFF_CONTEXT_ENABLED=true
STAFF_CONTEXT_CHANNEL_IDS=123456789012345678,234567890123456789
```

Do not configure public member channels unless storing them is an explicit,
reviewed decision. To stop new live storage, set:

```dotenv
STAFF_CONTEXT_ENABLED=false
```

Then restart or deploy the bot. Existing context remains searchable.

## Message Content Intent

BroEdenBot requests Message Content Intent in code. Discord must also allow it:

1. Open the application in the Discord Developer Portal.
2. Open **Bot**.
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**.
4. Save changes.
5. Restart or deploy BroEdenBot.

If the intent is unavailable, the bot does not crash. Startup logs warn about
the problem, and `/staffai help` plus `/staffai status` explain that live text
capture needs the intent.

## Live-message behavior

For an allowlisted message, the bot stores guild, channel, Discord message ID,
author, timestamp, redacted text, content hash, source, attachment count and
filenames, and storage time. Discord message ID provides live deduplication.

Edits update the latest stored content and set `edited_at`; edit history is not
retained. Deletes are marked with `deleted=true` and `deleted_at` when delete
tracking is enabled. The row remains for context continuity.

## Historical CSV imports

Export approved staff channels with DiscordChatExporter CSV columns:

- `AuthorID`
- `Author`
- `Date`
- `Content`
- `Attachments`
- `Reactions`

Place CSV files in:

```text
imports/staff_context/
```

The importer skips `archive`, `archived`, `broken`, and `broken_exports`
folders. Filenames such as
`Bro Eden - staff-chat [123456789].csv` provide channel name and ID. You can
override either value. Any import filename containing `headquarters` or `logs`
(case-insensitive) is treated as staff-channel material and normalized to the
channel name `staff`:

Any import filename containing `Hoarders Island`, `Hoarder's Island`, or
`Hoarder’s Island` is normalized to the channel name `archived`. These channels
are inactive historical channels, viewable only by Admins, rather than active
staff workspaces. Their imported messages remain private and searchable by
authorized `/staffai` users.

```bash
python3 scripts/import_staff_context.py \
  --guild-id YOUR_GUILD_ID \
  --channel-id CHANNEL_ID \
  --channel-name staff-chat \
  --dry-run
```

Import and optionally archive completed files:

```bash
python3 scripts/import_staff_context.py --guild-id YOUR_GUILD_ID

python3 scripts/import_staff_context.py \
  --guild-id YOUR_GUILD_ID \
  --archive-completed
```

The importer streams rows and writes bounded batches. Its dedupe key combines
source filename, row number, timestamp, author ID, and content hash. It inserts
`source=imported_csv` into the same table used by live messages. It never reads
or writes historical activity-statistics tables.

## Commands

- `/staffai help` shows private usage and Message Content Intent guidance.
- `/staffai status` shows live tracking state, configured-channel count, intent
  availability, database size, source counts, latest timestamp, and FTS status.
- `/staffai search <query> [source] [channel_name] [after] [before]` performs
  deterministic local search. `source` is `all`, `imported_csv`, or
  `live_discord`.
- `/staffai ask <question>` retrieves relevant context across both sources,
  then asks Gemini using only capped excerpts.
- `/staffai summarize [channel_name] [after] [before] [topic] [source]`
  summarizes a scoped selection across one or both sources.

All responses are ephemeral and include source/channel/date references where
context is returned. SQLite FTS5 is used when available, with a deterministic
`LIKE` fallback.
