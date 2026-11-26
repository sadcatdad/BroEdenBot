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
`MESSAGE_CONTEXT_ALLOWED_ROLE_IDS` can use `/context`. By default, replies
are ephemeral except a successful `/context user` evaluation, which posts to
the invoking channel. Search, summarize, timeline, user, and channel accept
`post_to:<text channel>` to deliberately share a successful response in a
selected channel in the same server. The invoker receives a private message
link. `channel` selects source history; `post_to` selects the destination.
Choose a staff channel for archive excerpts and staff summaries: everyone with
View Channel in the destination can read the posted response. Help, status,
validation errors, empty results, and failures remain private. An empty
allowed-role list does not grant administrators access; only configured bot
owners remain authorized.

Channel delivery checks the invoker's current archive access and View Channel
and Send Messages permissions, plus the bot's View Channel, Send Messages, and
Embed Links permissions. It reloads membership and destination overwrites
before retrieval and again before publishing, so access lost during AI
generation prevents posting. Posted results suppress member and role pings.
Restart the bot to synchronize the new slash-command options with Discord.

A successful `/context user` response posts **two embeds in one message**:

1. A member overview with strengths, evidence-supported growth opportunities,
   up to five representative verbatim quotes, limitations, and observed public
   command usage. Quotes from NSFW-marked channels are permitted; secrets,
   staff concerns, moderation history, and other members' content are not.
2. A Community Contribution Score card with five category ratings, earned
   bonuses, supported conduct deductions, the calculation, confidence, and
   archive coverage. Discord's 6,000-character budget is enforced across both
   embeds together, including delivery through `post_to`.

### Community contribution rubric v1

The bot calculates `70 + bonuses - deductions`, clamps the result to 0–100,
rounds the final result half-up, and applies any supported conduct ceiling.
The AI supplies assessments and exact target-message evidence IDs, never the
final total. Missing, malformed, or insufficient evidence displays
**Insufficient evidence**, never a fabricated zero.

| Category | Maximum bonus |
| --- | ---: |
| Connection and conversation | 10 |
| Welcoming and inclusion | 7 |
| Support and helpfulness | 6 |
| Shared experiences and initiative | 4 |
| Constructive conduct | 3 |

Each category earns `maximum × rating / 4`. Ratings are 0 (not observed,
neutral), 1 (one example), 2 (separate exchanges), 3 (recurring on different
dates), or 4 (sustained across weeks). The bot caps ratings to what the cited
source evidence actually supports: rating 2 needs two channel conversation
sessions, rating 3 needs three dates, and rating 4 needs four dates spanning
at least 21 days. Sessions conservatively group messages in the same channel
until there is a gap longer than 30 minutes. At least five interpretable
sessions are needed for a numerical score; messages containing only an
unreadable attachment or URL cannot establish one. They are not misconduct.
A score of 90+ also requires sustained positive evidence across three weeks.

Conduct deductions are -3 for an isolated unaddressed disrespectful exchange,
-10 for repeated antagonism (ceiling 69), and -25 for persistent harm
(ceiling 59). Patterns replace their constituent incidents; overlapping
sessions cannot be charged twice. Persistent harm, serious incidents, and
conduct concerns without retrieved surrounding context withhold the numerical
score for staff review. Their allegations, growth advice, and quotes are not
published as a substitute for review; this does not perform moderation or
create an automatic disciplinary decision.

70–79 denotes healthy participation, 80–89 clearly positive contribution, and
90–100 exceptional sustained contribution. No direct credit is awarded for
volume, XP, reactions, money, popularity, or staff rank. Concise messages,
GIFs, personal sharing, appropriate NSFW participation, civil disagreement,
boundary-setting, and unavailable VC activity do not lower conduct scores.
Growth opportunities require direct evidence. All quotation metadata is
rebuilt from the selected member's actual retrieved messages, and invented or
non-verbatim quotes are rejected.

Confidence is separate from score: low for failed AI batches or less than
seven days of interpretable evidence; high for complete retrieval, at least
21 days and ten sessions; medium otherwise. The card reports capped coverage
and skipped AI batches. Only messages from successful AI batches can support
ratings, deductions, and quotations.

### Review coverage and command observations

The default user review is **90 days and 2,500 messages**, adjustable to
**5,000 messages**. When capped, stable sampling distributes retrieval across
weeks and channels, rather than taking only the latest messages. Up to 160
representative or potentially ambiguous source messages receive up to four
nearby messages from their own channel within five minutes, within the same
requested period and archive exclusions. Nearby messages are labeled context
only and cannot become the selected member's contributions or public quotes.
Deleted, webhook, known automation, and unattributable imported Deleted User
rows are excluded from user evaluations. This is not an exhaustive historical
bot classifier. Public AI chunk recaps run at bounded concurrency (three)
with existing AI budget checks; failures report partial coverage or remain
private when no usable evaluation is available.

With archive tracking enabled, `message_context_command_usage` stores minimal
live command metadata: invocation IDs, member/application/channel/parent IDs,
command path, timestamp, observation source/confidence, optional response
message ID, and optional NSFW-access role state. It never stores command
arguments or response contents. Records follow the archive's configured
channels, exclusions, and retention. Tracking starts after this update; it
does not reconstruct historical slash calls from CSVs.

The bot can record its own command invocations, including commands whose
responses are ephemeral. Those private invocation records never appear on
public evaluation cards or feed the scoring model. For other bots, only
visible public responses with Discord interaction metadata are observable.
Follow-ups and repeated gateway events are deduplicated by application and
interaction ID. A later edited response may enrich an unknown command name
without changing the original timestamp or access-role snapshot.

Legacy response metadata can supply the exact command name. Modern metadata
may omit it; those calls remain unknown. Optionally configure
`MESSAGE_CONTEXT_RANK_BOT_IDS` to recognize textual rank-and-level responses
from trusted bots as **inferred rank responses**, separately from exact
`/rank` observations. Image-only rank cards cannot be interpreted. Other bots'
private/ephemeral command uses are unavailable.

The overview reports observed rank checks, the busiest rolling 15-minute
window, and, when `MESSAGE_CONTEXT_NSFW_ACCESS_ROLE_IDS` is configured, how
many occurred without any configured NSFW-access role. This captures access
state at observation time, not a numeric MEE6 level or whose level was queried.
Missing role configuration/member cache yields unknown eligibility. Frequency
alone never deducts points or establishes an NSFW-access-farming motive.

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
MESSAGE_CONTEXT_NSFW_ACCESS_ROLE_IDS=
MESSAGE_CONTEXT_RANK_BOT_IDS=
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
  [limit] [post_to]` performs deterministic local FTS5 or LIKE search. It returns short
  excerpts, timestamps, channels, authors, source labels, and jump links.
- `/context summarize after:<time> [before] [channel] [topic] [style]
  [include_links] [post_to]` produces a neutral staff recap.
- `/context timeline after:<time> [before] [channel] [topic] [granularity] [post_to]`
  builds a chronological narrative.
- `/context user user:<member> [timeframe:<24h|3d|7d|14d|30d|60d|90d>]
  [channel] [include_bots] [max_messages] [post_to]` is an authorized-staff command
  that posts a public overview and a separate rubric scorecard. Defaults are
  90 days and 2,500 messages (maximum 5,000), sampled across the requested
  period/channels when capped. See the rubric and command-observation details
  above. NSFW quotes are permitted; staff/moderation information is not.
- `/context channel channel:<channel> timeframe:<1h|6h|12h|24h|3d|7d|14d|30d>
  [topic] [include_bots] [max_messages] [post_to]` reviews a stored channel timeframe.

Dates accept ISO dates/times plus `yesterday` and `today`. Examples:

```text
/context summarize after:2026-06-21 20:00 before:2026-06-21 23:00
/context timeline after:yesterday before:today
/context search query:"NSFW access"
/context channel channel:#general timeframe:24h post_to:#staff
/context summarize after:yesterday post_to:#staff
```

Gemini commands retrieve locally first, cap the number of rows, summarize
bounded chunks, and combine those chunk summaries. User reviews sample across
the period and channels; other commands retain their most-recent retrieval. The database is never sent
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
