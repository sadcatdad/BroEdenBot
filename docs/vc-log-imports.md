# Historical VC log imports

## Purpose

BroEdenBot tracks live voice-channel sessions only while the bot is online.
The historical VC importer reconstructs older sessions from messages in the
server's VC log channel and stores them separately from live sessions.

The importer is intentionally conservative:

- It streams DiscordChatExporter JSON messages with `ijson`.
- It imports only reconstructed sessions that pass the configured sanity
  limits.
- It records source message IDs, source filename, confidence, estimation
  status, and a deterministic dedupe key.
- It never creates active live sessions.
- It does not print raw VC log message contents.
- Imported sessions count toward tracked VC time but not reward-eligible time
  or VC XP pulses. Historical logs cannot prove AFK, alone, mute, or deafen
  eligibility.

## Required export format

Use a **DiscordChatExporter JSON export** of the VC log channel. JSON is
required for this importer because embed titles, descriptions, fields,
footers, mentions, and IDs contain the useful event data. CSV is not supported
for this workflow.

Expected source channel:

- Channel ID: `1278274747913867347`
- Guild ID: `1278253523619807233`

Place the completed export here:

```text
imports/vc_logs/vc-log.json
```

Create the local folder on the Mac with:

```bash
mkdir -p ~/Documents/BroEdenBot/imports/vc_logs
```

Do not commit VC log exports. `imports/vc_logs/` and `exports/vc_logs/` are
ignored by Git.

## Sync the export to the Raspberry Pi

From the Mac:

```bash
rsync -avh --progress \
  ~/Documents/BroEdenBot/imports/vc_logs/ \
  sadcatdad@raspberrypi.local:/home/sadcatdad/BroEdenBot/imports/vc_logs/
```

## Dry run

Run the dry run before every real import:

```bash
cd ~/BroEdenBot
source .venv/bin/activate

python scripts/import_vc_logs.py \
  --folder imports/vc_logs \
  --guild-id 1278253523619807233 \
  --dry-run
```

The dry run parses messages, detects events, reconstructs sessions, checks
existing dedupe keys when `data.db` exists, and prints summaries. It does not
create or modify a database and does not archive files.

If the exporter file is incomplete or malformed, the importer reports a
failure, writes nothing, and leaves the file in place for re-export or repair.

## Real import

After reviewing the dry-run totals:

```bash
python scripts/import_vc_logs.py \
  --folder imports/vc_logs \
  --guild-id 1278253523619807233 \
  --archive-completed \
  --archive-duplicates
```

Successful sessions are written to `data.db` in
`vc_imported_sessions`. Completed files move to
`imports/vc_logs/archive/` when archiving is requested. Duplicate-only files
are archived only when `--archive-duplicates` is also supplied. Failed files
stay where they are.

For one file instead of the whole folder:

```bash
python scripts/import_vc_logs.py \
  --file imports/vc_logs/vc-log.json \
  --guild-id 1278253523619807233 \
  --dry-run
```

## Session controls

Defaults:

```text
--close-open-at-export-end true
--min-session-seconds 10
--max-session-hours 24
```

- `--close-open-at-export-end true` closes sessions still open at the end of
  the export using the last parsed VC event timestamp. These rows are marked
  estimated with `close_reason=closed_at_export_end`.
- Set it to `false` to report but not import open sessions.
- Sessions shorter than the minimum are skipped.
- Sessions longer than the maximum are skipped as suspicious. They are not
  silently capped.

## Supported event patterns

Pattern helpers recognize common Carl-bot and Discord log variants, including:

- joined voice channel
- connected to voice
- left voice channel
- disconnected from voice
- moved voice channels
- switched voice channels
- changed voice channel
- moved or changed from one channel to another

The parser checks message content plus embed titles, descriptions, fields, and
footers. It supports Discord user/channel mentions, Discord channel links,
footer user IDs, semantic `Before`/`After` fields, and name-only descriptions.

Confidence is assigned as:

- `high` — user ID and voice-channel ID are available
- `medium` — user ID is available, but the channel is name-only
- `low` — the user is name-only

## Reconstruction behavior

- Join opens a session.
- Leave closes the active session.
- Move closes the old session and opens the new channel.
- A second join closes the prior session at the new join timestamp and marks
  that closure estimated.
- A leave without an active session is reported as unmatched.
- A move missing its old channel closes any active session.
- A move missing its new channel closes the active session without opening a
  replacement.

## Deduplication

Each session receives a deterministic key built from:

- guild
- user ID or normalized user name
- channel ID or normalized channel name
- join and leave timestamps
- starting and ending source message IDs

`vc_imported_sessions.dedupe_key` has a unique index. Re-running the same export
does not duplicate historical time.

## Verify in Discord

Restart BroEdenBot after the import so the cog can confirm its schema, then
check:

```text
/vcstats user user:@member source:all
/vcstats user user:@member source:imported
/vcstats leaderboard source:all include_left_members:true
/vcstats channel source:imported
/vcstats export source:imported include_left_members:true
```

`source:all` is the default. The available filters are:

- `all` — live plus imported sessions
- `live` — sessions tracked by BroEdenBot while online
- `imported` — reconstructed historical VC log sessions

Name-only historical rows can appear in leaderboard/export results when
`include_left_members:true`, but `/vcstats user` requires a Discord member and
therefore can only match imported rows with a user ID.

## Limitations

- Accuracy depends on the completeness and ordering of the VC log export.
- Missing joins or leaves create unmatched or open sessions.
- Channel renames and name-only channels reduce confidence.
- Name-only user parsing is lower confidence and can merge identical names.
- Sessions that began before the available log history may be unrecoverable.
- The bot cannot infer VC time that was never logged.
- Historical logs do not establish AFK, alone, mute, or deafen state, so
  imported sessions are not reward eligible and do not generate VC XP pulses.
