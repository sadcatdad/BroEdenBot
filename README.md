# BroEdenBot

BroEdenBot is a Discord community bot for Bro Eden. It provides member-facing
server guidance, moderation guidance, staff notes, voice-channel activity
tracking, live statistics, queues, polls, leaderboards, and bank tracking.

The bot loads every Python cog in `cogs/` and synchronizes its application
commands when it starts. If `ENABLED_MODULES` is set, mapped cogs load only
when their module name is enabled; leaving it blank preserves load-all behavior.

For a module-by-module architecture and reliability map, see
[docs/codebase-map.md](docs/codebase-map.md).

## Command notation

- `<input>` means the input is required.
- `[input]` means the input is optional.
- **Ephemeral** responses are visible only to the person who ran the command.
- Legacy queue commands use the `!` prefix.

## Permissions

| Feature | Who can use it |
| --- | --- |
| `/ask` | All server members |
| `/knowledge sources add`, `/knowledge sources list`, `/knowledge sources remove`, `/knowledge sources sync` | Administrators or members with **Manage Server** |
| `/bot help`, `/bot status`, `/bot logs`, `/bot restart`, `/bot deploy` | Users listed in `BOT_OWNER_USER_IDS`; administrators only when `BOT_OWNER_ALLOW_ADMINS=true` |
| `/ai test`, `/ai status`, `/ai kb` commands | Users listed in `BOT_OWNER_USER_IDS`; administrators only when `BOT_OWNER_ALLOW_ADMINS=true` |
| `/staffai help`, `/staffai status`, `/staffai ask`, `/staffai search`, `/staffai summarize` | Roles listed in `STAFF_AI_ALLOWED_ROLE_IDS` or users listed in `BOT_OWNER_USER_IDS` |
| `/context help`, `/context status`, `/context search`, `/context summarize`, `/context timeline`, `/context user`, `/context channel` | Roles listed in `MESSAGE_CONTEXT_ALLOWED_ROLE_IDS` or users listed in `BOT_OWNER_USER_IDS` |
| `/rulecard draft` and **Draft Rule Reminder** message action | Administrators or roles listed in `MODAI_ALLOWED_ROLE_IDS` |
| `/checklist` commands and management controls | Roles listed in `CHECKLIST_ALLOWED_ROLE_IDS` or users listed in `BOT_OWNER_USER_IDS` |
| `/remind personal` | Roles in `REMINDER_PERSONAL_ALLOWED_ROLE_IDS`; blank allows all members. Staff-only targeting and channel delivery remain separately protected. |
| `/remind event` | Roles in `REMINDER_EVENT_ALLOWED_ROLE_IDS`; blank falls back to `REMINDER_ALLOWED_ROLE_IDS` and configured staff/admin roles. |
| `/remind manage` | Roles in `REMINDER_MANAGE_ALLOWED_ROLE_IDS`; blank allows all members to manage their own reminders. `REMINDER_MANAGE_ALL_ROLE_IDS` controls guild-wide management. |
| `/remind subscriptions`, `/events`, and event **Remind Me** buttons | Roles in `REMINDER_SUBSCRIPTIONS_ALLOWED_ROLE_IDS`; blank allows all members. |
| Dashboard `/events` schedule and DM controls | Signed-in current server members mapped to **Verified Events Member** (or a higher event-management dashboard role). |
| Dashboard event create/edit/cancel | **Party Captain** can create and manage their own one-time events. Owner/Administrator can manage all one-time events. Recurring Discord events are read-only. |
| `/remind help`, `/timezone`, `/time` | All server members. |
| ModAI commands and context menus | Administrators or roles listed in `MODAI_ALLOWED_ROLE_IDS` |
| Staff-note commands | Administrators or roles listed in `STAFF_NOTES_ALLOWED_ROLE_IDS` |
| `/staffnote delete` | Administrators only |
| `/staffnote edit` | Administrators or the original note author |
| VC stats commands | Administrators or roles listed in `VCSTATS_ALLOWED_ROLE_IDS` |
| `/vcrewards audit` | Administrators or roles listed in `VCREWARDS_ALLOWED_ROLE_IDS`, falling back to `VCSTATS_ALLOWED_ROLE_IDS` |
| Other VC reward commands | Administrators or roles listed in `VCSTATS_ALLOWED_ROLE_IDS` |
| `/vcstats reset` | Administrators only |
| `/vcrewards pulse` | Administrators only |
| Bank commands | Administrators or roles listed in `BANK_ALLOWED_ROLE_IDS` |
| Stats creation, banner replacement, and refresh commands | Administrators or roles listed in `STATS_ALLOWED_ROLE_IDS` |
| `/stats delete` and `/stats reset` | Administrators only |
| `/poll`, `/queue dashboard`, and member queue actions | All server members |
| Queue lock/unlock/move/remove/pull controls | Administrators or members with **Manage Channels** |
| `/leaderboards`, `/points`, `!points` | All server members |
| `/leaderboard create`, `/leaderboard edit` | Administrators or members with **Manage Server** |
| `/leaderboard add`, `/leaderboard remove` | Configured bot owners, administrators, moderators, or staff roles |
| `/leaderboard delete`, `/leaderboard role ...` | Users listed in `BOT_OWNER_USER_IDS` |
| `/leaderboard reset` | Bot owners, administrators, configured admin roles, or `LEADERBOARD_RESET_ROLE_IDS` |

For staff-restricted features, an empty role-ID environment variable makes the
feature effectively administrator-only. This does not apply to `/ask` or the
`/bot` or `/checklist` groups. Bot management and checklist management are
owner-only by default when their broader access settings are blank.

## Reminder commands

The canonical reminder family is `/remind`. Personal reminders, events,
occurrences, subscriptions, delivery attempts, and audit history live in
`data.db`. A 30-second worker claims due delivery records with database leases,
recovers interrupted claims after restarts, retries temporary Discord failures,
and suppresses stale deliveries outside the configured grace window.

- `/remind personal [destination] [who]` — Create a private one-time or
  recurring reminder. Members default to their own DMs. Server-channel delivery
  and targeting another member retain the existing staff permission check.
- `/remind event` — Configured staff create a public event card titled
  **🎉 EVENT REMINDER** (the event name appears as a subheader) with labeled
  **Remind Me** and **Open Channel** controls. Events support multiple advance
  timings, recurrence, subscriber timing customization, live subscriber counts,
  edits, and cancellation notices. The confirmation preview includes a timezone
  selector so the poster can interpret the **When** text in their own timezone;
  the choice is saved as their `/timezone` for future reminders.
- `/events` — Ephemeral browser of upcoming events for any member (gated by
  `REMINDER_SUBSCRIPTIONS_ALLOWED_ROLE_IDS`). Multi-select to subscribe to
  several events at once (already-subscribed events are flagged), plus a
  **Manage Event Subscriptions** button to change timing or unsubscribe. The
  header uses a built-in card, or a saved Message Studio asset when
  `EVENTS_HEADER_ASSET_ID` is configured (supports `{count}` and `{next_event}`).
- `/remind manage [status] [reminder_type] [recurrence]` — Manage reminders you
  created. Configured staff can manage matching reminders across the guild.
- `/remind subscriptions` — Change timing, restore event defaults, open the
  destination, or unsubscribe from active events.
- `/remind help` — Private member-facing command guidance.
- `/timezone [timezone]` — View or set the IANA timezone used to interpret
  natural-language dates. Final dates use Discord timestamps for each viewer.
- `/time <when>` and staff-only `!time <when>` — Produce Discord timestamp codes.

`/reminder add`, `/reminder manage`, and `/remind subscribe` are temporary
compatibility routes backed by the same canonical service. Set
`ENABLE_LEGACY_REMINDER_COMMANDS=false` after the transition period to disable
them. Existing reminder rows are migrated idempotently at startup and retained
untouched for rollback evidence. Full architecture, dashboard, migration,
manual-test, deployment, and troubleshooting instructions are in
[`docs/reminders.md`](docs/reminders.md).

## Dashboard Events Hub

The authenticated `/events` page mirrors every Discord Scheduled Event and
keeps each event linked to the canonical reminder service. It presents a Bro
Eden-styled next-gathering hero, type filters, month-grouped cards, an HTML
calendar, native **Open in Discord** links, and separate BroEdenBot DM controls.
Quick Subscribe selects 15 minutes before plus start time; members can instead
choose 6 hours, 1 hour, 15 minutes, and/or start time.

Map the server's existing Verified Discord role to **Verified Events Member**
under Dashboard Access. Map the Party Captain role to **Party Captain**. The
dashboard continues to re-check live guild membership, pending membership
screening, and role IDs through Discord OAuth. Captains can publish one-time
Stage, Voice, or external events and edit/cancel only their own. Discord-created
recurring events remain visible and subscribable but read-only. Dashboard writes
are queued in SQLite for the bot process; FastAPI never receives or uses the bot
token. Owners select a private forum, thread, or text channel as **Event Artwork
Storage** under **Features → Events**. Dashboard covers are normalized, posted
there by the bot, and then displayed from the Discord attachment link; the
temporary database bytes are cleared when the action finishes. Full setup,
permissions, migration, validation, and recovery guidance is
in [`docs/events.md`](docs/events.md).

## DISBOARD bump rewards

Verified responses from the configured official DISBOARD bot award
`BUMP_POINTS_PER_SUCCESS` points, trigger the configured reward role, and
schedule a reminder automatically for two hours after the successful `/bump`.
Detection accepts Discord's current interaction metadata and the legacy
interaction shape, but only when the trusted DISBOARD response identifies the
`bump` command and contains a known success message. `BUMP_SUCCESS_ASSET_ID`
selects the Embed/Message Editor asset sent for the confirmation. Its message,
embed card, and first four configured buttons are used, and the bot reserves
the fifth slot for the one-use **Bump Leaderboard** button. The asset supports
`{user.feature}`, `{role.feature}`, `{member}`, `{points}`, and
`{reward_status}`. `!bumpscores` displays the branded Bump Legends board in
ten-row pages.

`BUMP_REMINDER_ASSET_ID` selects the complete asset sent two hours later.
`{user.feature}` identifies the member whose bump triggered the reminder and
`{role.feature}` mentions the configured `BUMP_PING_ROLE_ID`; the compatibility
aliases `{member}` and `{role}` are also supported. Template buttons are used
as configured. The previous automatic **Subscribe to Bump Reminders** button
is no longer added. The built-in reminder message/embed remains the fallback
when no asset is selected or the selected record is unavailable.

The weekly publisher posts the leaderboard to `BUMP_LEADERBOARD_CHANNEL_ID`.
Detection requires Guild Messages and Message Content intents. Reward-role
handoff requires **Manage Roles** with Bro Eden above the configured role.
Installation never renames or deletes pre-existing custom leaderboards that
happen to use historical RiffBot bump-board names.

## Daily message streaks

Qualifying member-accessible messages maintain current and longest daily
streaks in `data.db`. Channels gated behind an ordinary verified/member role
count; private channels accessible only through staff/admin roles do not. Bots,
webhooks, commands, short messages, duplicate messages, private staff channels,
and configured excluded channels or categories do not count. Deleted source
messages are removed and affected streaks are recomputed.

- `!streak` — Shows the caller's current/longest streak and any unread milestone.
- `/streak leaderboard [streak_type]` — Shows current or longest streaks in
  ten-row graphical pages.

Milestones begin at 7, 14, 30, 45, 60, and 100 days and continue at the
configured rolling thresholds. `STREAK_MILESTONE_CHANNEL_ID` can announce each
new milestone with the selected `STREAK_MILESTONE_ASSET_ID` Embed/Message
Editor asset. It supports `{user.feature}`, `{member}`, and `{days}`. Leave the channel blank to keep
milestone details private to the member's streak view.
`STREAK_LEADERBOARD_CHANNEL_ID` receives the persistent weekly tracker;
`STREAK_TIMEZONE` controls day boundaries.

The bot writes a per-guild heartbeat while streak tracking is online. When a
heartbeat gap exceeds `STREAK_RESTORE_GAP_MINUTES`, the next startup queues an
automatic Discord-history scan from the last heartbeat through the restart.
Recovered messages are evaluated with the same public-channel, word-count,
duplicate, bot, webhook, and command rules as live messages, using each
message's original timestamp for the streak date. Requests are durable, capped
by `STREAK_RESTORE_MAX_DAYS` and `STREAK_RESTORE_MAX_MESSAGES`, and return to
the pending queue if the bot stops during processing.

The dashboard has a top-level **Streaks** page with current/longest data,
heartbeat and restore status, an audited add/remove-day correction form, and a
**Restore Streaks** button. The button queues a selected date range even while
the bot is offline; the bot processes it after it returns. Manual changes edit
the qualifying-day source history and recalculate both current and longest
streaks instead of directly overwriting cached totals. Owner/admin write access
and CSRF validation are required; viewer accounts are read-only. A manual
remove remains an exclusion so a later broad history restore cannot silently
re-add that deliberately removed day; a subsequent manual add supersedes it.

## Internal checklist commands

Persistent staff checklists live in `data.db`; posted Discord messages are
synchronized copies rather than the source of truth. All management responses
and controls are private. Posted checklist embeds are visible in their channels.

- `/checklist create <name> [description] [post_channel]` — Create a checklist,
  optionally post it immediately, and open its management panel.
- `/checklist view [checklist]` — Open an active or archived checklist panel, or
  choose an active checklist when no input is supplied.
- `/checklist list [status]` — List active, archived, or deleted checklists with
  progress and post counts. `status` defaults to `active`.
- `/checklist post <checklist> <channel> [update_existing]` — Post a synchronized
  copy; optionally update an existing active copy in that channel.
- `/checklist delete [checklist]` — Confirm a soft deletion and attempt to remove
  all active Discord copies.
- `/checklist rename <checklist> <new_name> [description]` — Rename the
  checklist and optionally replace its description.
- `/checklist archive <checklist> [delete_posts]` — Archive it, retaining and
  updating posts by default.
- `/checklist restore <checklist>` — Restore an archived checklist.
- `/checklist refresh <checklist>` — Refresh every active posted copy and
  reattach persistent controls if Discord retained stale buttons.
- `/checklist export <checklist>` — Privately export all checklist items as CSV.

The panel supports adding, toggling, and soft-deleting items, renaming, posting,
archiving/restoring, and refreshing. Posted checklists include the same
persistent controls; each click is permission-checked and management prompts
remain ephemeral. See
[docs/checklists.md](docs/checklists.md) for storage, synchronization, deletion,
permission, and Discord-limit details. Posting a checklist requires the bot to
have **View Channel**, **Send Messages** (or **Send Messages in Threads**), and
**Embed Links** in the target channel; updating an existing copy also requires
**Read Message History**.

## Bot management and analytics commands

- `/bot help` — Private command guide and terminal-import reminders.
- `/bot status` — Private bot, database, configuration, import-folder,
  systemd, VCXP-role, AI framework, and Git health report.
- `/bot logs [lines]` — Private, redacted recent `broedenbot` systemd logs.
- `/bot restart` — Confirmation-gated systemd restart.
- `/bot deploy` — Confirmation-gated detached deployment.
- `/ai test` — Private owner/admin AI framework smoke test using the default
  routed Gemini model.
- `/ai status` — Private AI framework configuration and budget-spend summary.
- `/ai kb import`, `/ai kb status`, `/ai kb search`, `/ai kb delete` —
  Owner/admin AI Knowledge Base management.
- `/stats activity trends [period] [source] [channel]` — Compares a fixed
  period with the immediately preceding period.
- `/stats activity categories [period] [source] [limit] [channel]` — Groups
  activity using `data/channel_categories.json`.
- `/stats activity heatmap [period] [source] [timezone] [channel]` — Summarizes
  activity by local day, hour, and broad time block.
- `/ask <question>` — Answers from public guidance with private follow-up
  buttons.
- `/staffai help`, `/staffai status`, `/staffai ask`, `/staffai search`,
  `/staffai summarize` — Private imported and live staff-channel context tools.
- `/context help`, `/context status`, `/context search`, `/context summarize`,
  `/context timeline`, `/context channel` — Private full-server message
  archive, search, and timeline tools. Authorized staff can run `/context user`
  to post a deliberately high-level public member evaluation.
- `/rulecard draft` — AI-drafted rule reminder preview for staff review.
- `/vcrewards audit` — Runs a private, read-only VCXP safety audit.

### `/bot` management group

All `/bot` responses are ephemeral. By default, only Discord user IDs listed
in `BOT_OWNER_USER_IDS` can use these commands. Server administrators are not
automatically trusted; set `BOT_OWNER_ALLOW_ADMINS=true` only if that broader
access is intentional.

```dotenv
BOT_OWNER_USER_IDS=YOUR_DISCORD_USER_ID
BOT_OWNER_ALLOW_ADMINS=false
```

- `/bot help` lists the available management commands.
- `/bot status` reports runtime, systemd, Git, loaded cogs, SQLite files,
  historical import metadata, active/archive import-folder counts,
  configured-or-missing environment status, and VCXP role safety. This includes
  configured/missing status for `ACTIVITY_EXCLUDED_ROLE_IDS` and
  `VC_EXCLUDED_ROLE_IDS` without displaying raw values. It never displays
  environment values or secrets. Startup cog failures are listed by filename
  and exception type without exposing stack traces.
- `/bot logs [lines]` reads 50 recent service lines by default and accepts up
  to 200. Obvious credentials and traceback details are removed. Longer output
  is attached as a private text file.
- `/bot restart` requires a requester-only confirmation and launches a
  detached systemd restart.
- `/bot deploy` requires a requester-only confirmation and launches
  `./deploy.sh` as a detached systemd job from the Pi project directory.

Restart and deployment confirmations expire after 60 seconds. The Pi service
account must have narrowly scoped passwordless permission for the documented
`systemd-run` commands.

Historical imports intentionally remain terminal-only because they can be
long-running and require export files to be transferred first:

```text
bedsync        # Mac: transfer exports to the Pi
bedimportdry   # Pi: preview imports
bedimport      # Pi: run imports
```

The default full-CSV workflow syncs `imports/message_context/` and runs
`scripts/import_full_csv_exports.py`. Every CSV feeds private `/context`;
activity is backfilled only for channel IDs without completed JSON activity
imports.

## Member server-help command

### `/ask <question>`

Lets members ask general questions about Bro Eden rules, channels, levels,
events, verification, NSFW access, server features, and support.

- `question` — Required server-related question, up to 1,000 characters.
- The command is available to regular server members and does not require a
  staff role.
- Successful responses are ephemeral by default and visible only to the member
  who used `/ask`.
- The private response is a compact green embed with the submitted question and
  answer in one description. It does not append raw retrieval source names to
  the member-facing answer:

  ```text
  **Question:**
  [submitted question]
  **Answer:**
  [answer]
  ```

- Extra blank lines in Gemini's answer are collapsed to keep the embed compact.
- The member's question is Markdown-escaped before display, and generated
  mentions are not allowed to notify users or roles.
- Cooldown, configuration, channel-restriction, out-of-scope, and staff-only
  redirects are also shown ephemerally.
- Successful answers include `Open Ticket`, `This Helped`, and `Still Confused`
  buttons. Only the member who ran `/ask` can use them. `This Helped` and
  `Still Confused` acknowledge the member privately and then disable the
  feedback buttons on that response. The selection is stored locally for
  dashboard review; it does not publish feedback, alert staff, or retrain
  Gemini.

`/ask` uses Gemini with only `public` sources in the AI Knowledge Base
(`ai_kb_sources` and `ai_kb_chunks`). Import public rules, guides, FAQs, role
guides, or channel guides through `/ai kb import`, the dashboard AI Knowledge
Base editor, or live Discord knowledge sources configured with
`/knowledge sources`.

It does not read `staff_notes.py`, staff-note records, ModAI prompts, moderation
logs, private incident records, imported Discord history, message history,
member activity stats, or other private data. Successful member-facing answers
create a local `ask_feedback` row with the member ID, question, answer, matched
public KB chunks, model metadata, and later `helped`/`confused` button choice.
This review data is used to improve the public Knowledge Base manually.

The prompt requires Gemini to stay grounded in the retrieved public KB chunks,
avoid inventing policies or permissions, and keep replies concise. When no
public KB source matches, `/ask` says it could not find an answer and directs
the member to submit a ticket in <#1300632962127368283>. If public knowledge
does match but Gemini returns an empty response, `/ask` falls back to a concise
excerpt from the matched public knowledge sources instead of showing a blank AI
failure.

Questions involving personal disputes, accusations, harassment, reports,
appeals, staff complaints, moderation actions, rule-enforcement decisions,
private information, attempts to bypass rules, or crisis/legal/medical matters
are not sent to Gemini. The member is privately directed to the support-ticket
channel instead. Clearly unrelated questions are also redirected rather than
answered.

The command uses `AI_MEMBER_COOLDOWN_SECONDS`. If Gemini returns a blocked
response, the private embed says it cannot answer safely. If Gemini fails, the
private embed directs the member to the support-ticket channel. Missing
`GEMINI_API_KEY` configuration is reported ephemerally.

### `/knowledge sources`

Configures Discord text channels, whole forum channels, or individual forum
posts/threads as live knowledge sources. These commands are private and require
**Manage Server** or administrator permission. The bot must have **View
Channel** and **Read Message History** in configured source channels or
threads. Live updates also require Message Content Intent to be enabled in the
Discord Developer Portal and requested by the bot.

- `/knowledge sources add <channel> <source_type> <visibility> <sync_mode>` —
  Add or update a source channel or specific forum post/thread. `source_type`
  can be `public`, `rules`, `survival_guide`, `channel_index`, `bot_commands`,
  `vc_guide`, `events`, or `staff`. `visibility` is `public` or `staff_only`.
  `sync_mode` is `live` for continuous indexing or `manual` for explicit
  backfills only.
- `/knowledge sources list` — Show configured channels, type, visibility,
  sync mode, indexed-entry count, and latest sync/index time.
- `/knowledge sources remove <channel>` — Remove a source and its indexed
  entries from the live knowledge tables and mirrored AI KB chunks.
- `/knowledge sources sync [channel] [limit]` — Backfill existing messages from
  one source or all enabled sources. `limit` defaults to 200 and can scan up to
  1,000 messages per source.

Configured sources are stored in `knowledge_sources`; indexed messages are
stored in `knowledge_entries` and mirrored into `ai_kb_sources` /
`ai_kb_chunks` as `live-discord:<guild_id>:<message_id>` sources. Text messages
store message content. Embeds are converted to readable Markdown using title,
description, and fields. Whole-forum sources index thread/post titles and
thread messages across the forum; specific forum-post sources index only that
selected thread. Useful non-image attachment names and URLs are kept; empty
image-only messages and duplicate normalized content are ignored.

Public `/ask` only reads entries with `visibility=public`.
Staff-only live sources use `visibility=staff_only` and are available only to
private staff surfaces such as `/staffai` and ModAI knowledge search. Staff-only
entries are never returned to member-facing `/ask`.

The Gemini request uses `ASK_MODEL` when configured, otherwise `MODAI_MODEL`,
and then the built-in default. The fallback follows the same order using
`ASK_FALLBACK_MODEL` and `MODAI_FALLBACK_MODEL`. A Gemini 503 response retries
the primary model once before trying the fallback model. Provider failures are
logged using only sanitized metadata such as stage, model, error type, code,
and status.

## AI framework foundation

The shared AI framework lives in `utils/ai_config.py`, `utils/ai_costs.py`,
`utils/ai_service.py`, and `utils/ai_kb.py`.

- Model tiers are `fast`, `default`, and `advanced`. `advanced` falls back to
  `default` unless `AI_ENABLE_ADVANCED_MODEL=true`.
- Gemini 2.5 Flash remains the default model. Flash-Lite is available as the
  fast tier for cheaper/simple future tasks.
- Requests are checked against daily and monthly budget limits before calling
  Gemini. The preflight check estimates worst-case cost from prompt size and
  configured output limit.
- Usage is logged to `ai_usage_logs` with metadata, token counts, estimated
  cost, success/failure, and budget-block state.
- `/ask` feedback is stored in `ask_feedback` so dashboard admins can review
  confusing answers and improve public KB sources.
- Editable AI KB source text is stored in `ai_kb_sources`; searchable chunks
  are stored in `ai_kb_chunks`.
- Live Discord knowledge sources are stored in `knowledge_sources` and
  `knowledge_entries`, then mirrored into the same AI KB chunk tables for
  retrieval.
- General AI prompts and responses are not stored. They are not logged unless
  `AI_LOG_PROMPTS=true` or `AI_LOG_RESPONSES=true`.
- Reusable cooldown helpers are available for future member and staff AI
  commands.

`/ai test` sends a tiny private framework request through the default tier and
reports model, token usage, estimated cost, and daily/monthly spend. `/ai
status` reports configuration and budget totals without showing secrets.

`/ai kb import` accepts pasted text or a `.txt`, `.md`, or `.markdown`
attachment, chunks it, and replaces any existing source with the same name.
`/ai kb search` returns short excerpts only. `/ai kb delete` removes a source
and its chunks.

Phase 1 `/ask` uses only `public` AI KB chunks, sends the top matching context
through the shared AI router, and refuses to invent an answer when no public KB
match exists. Staff-only KB chunks (`staff` or `staff_only`) are available only
to staff tools such as context summaries and rulecard drafting.

The framework accepts task types such as `ask_server_guide`,
`staff_context_user`, `public_context_user`, `staff_context_channel`,
`staff_context_topic`, `rulecard_draft`, `ticket_summary`,
`moderation_classification`, `weekly_recap`, and `onboarding_helper`.

## Private staff-context commands

`/staffai` is a separate staff-only context system backed by
`staff_context.db`. It combines historical CSV rows (`imported_csv`) with
allowlisted post-deployment messages (`live_discord`). Live tracking defaults
to disabled. It does not use or modify the historical activity importer or
activity-statistics tables. Full setup, Message Content Intent, import,
privacy, and operational instructions are in
[docs/staff-context.md](docs/staff-context.md).

Configured parent channels also include their Discord threads. Live and
imported text passes through shared obvious-credential redaction before it is
stored or sent to Gemini.

- `/staffai help` — Private command and Message Content Intent guidance.
- `/staffai status` — Private tracking, intent, database, source-count, latest
  message, and FTS status.
- `/staffai ask <question>` — Locally retrieves relevant imported staff
  and live messages, sends only capped relevant excerpts to Gemini, and
  returns a private answer with channel/date/source references.
- `/staffai search <query> [source] [channel_name] [after] [before]` —
  Deterministic local keyword search with date, author, channel, source, and
  short excerpts.
- `/staffai summarize [channel_name] [after] [before] [topic] [source]` —
  Summarizes a scoped channel, ISO date range, topic, source, or combination.
  At least one channel/date/topic scope is required.

All responses are ephemeral. Access requires a role in
`STAFF_AI_ALLOWED_ROLE_IDS` or a user ID in `BOT_OWNER_USER_IDS`;
administrator permission alone does not grant access. Public `/ask` never
opens or searches `staff_context.db`.

## Full-server message context commands

`/context` is a separate, disabled-by-default staff moderation archive backed
by `message_context.db`. When enabled, an empty channel include list tracks all
visible guild text, thread, and forum-post messages except explicitly excluded
channels. A populated include list restricts capture to those channels and
their threads. It never writes to or reads from the count-only activity stats
tables, and public `/ask` never uses it.

Obvious token, API-key, password, secret, and bearer-authorization patterns are
redacted before new live or CSV content is stored. The same redaction runs
again when older rows are displayed or supplied to Gemini. Search output
Markdown is escaped, and only Discord message URLs become jump links.

- `/context help` — Private command and capture-boundary guidance.
- `/context status` — Tracking mode, intent observation, database/source/date
  coverage, FTS, retention, edit tracking, and delete tracking.
- `/context search <query> [channel] [user] [after] [before] [source] [limit]`
  — Deterministic local search with brief excerpts and jump links.
- `/context summarize <after> [before] [channel] [topic] [style]
  [include_links]` — Chunked Gemini summary of a bounded scope.
- `/context timeline <after> [before] [channel] [topic] [granularity]` —
  Chronological staff narrative.
- `/context user <user> <timeframe> [channel] [include_bots] [max_messages]`
  — Authorized staff can post a public member evaluation with a `0`–`100`
  community-contribution score, strengths, constructive growth opportunities,
  and up to five representative verbatim quotes with source channel/date/jump
  links. NSFW-channel quotes are permitted. It never posts secrets, staff-only
  concerns, moderation history, or other members' content. Scores are
  calibrated with `70`–`79` as generally respectful/genuine and `80`–`89` as
  actively positive or consistently constructive; unavailable VC activity,
  concise messages, GIFs, personal sharing, and NSFW participation are not
  deductions. Timeframes include `24h`, `3d`, `7d`, `14d`, `30d`, `60d`, and
  `90d`.
- `/context channel <channel> <timeframe> [topic] [include_bots] [max_messages]`
  — Channel recap. Timeframes include `1h`, `6h`, `12h`, `24h`, `3d`, `7d`,
  `14d`, and `30d`.

Access requires `MESSAGE_CONTEXT_ALLOWED_ROLE_IDS` or `BOT_OWNER_USER_IDS`.
`/context user` posts its safe public evaluation to the invoking channel; all
other `/context` replies are ephemeral. Full privacy, Message Content Intent, retention,
configuration, import, and test instructions are in
[docs/message-context.md](docs/message-context.md).

## ModAI commands

All ModAI responses are private unless an authorized staff member deliberately
uses the **Send Rule Reminder** button. ModAI provides guidance only. It does
not warn, timeout, kick, ban, delete messages, or otherwise moderate members
automatically.

Private ModAI knowledge comes from the same configured public and staff-only
live Discord knowledge sources. Staff-only sources are not loaded by public
`/ask`.

### `/modai check <text>`

Privately reviews pasted text for possible moderation concerns using Gemini and
the configured Bro Eden knowledge sources.

- `text` — The text staff want reviewed. This is treated as untrusted content
  and is not stored by this command.

The response can include possible concern categories, relevant rules, context
considerations, suggested staff action, a draft response, handling route, and
whether more context is needed.

### `/modai rulesearch <query>`

Performs a local keyword search of configured public and staff-only knowledge
sources. This does not call Gemini.

- `query` — A word or short topic to find, such as `unsolicited DMs`,
  `self-promo`, or `politics`.

### `/modai rulehelp <situation>`

Privately evaluates a described situation against configured Bro Eden knowledge.

- `situation` — A description of the moderation question or behavior staff
  want help evaluating.

The result focuses on possible rule areas, severity, proportionate next steps,
response wording, and whether the matter belongs in public, private, ticket, or
no-action handling.

### `/modai incident <situation> [user] [action_taken] [notes]`

Builds a concise private incident-guidance report.

- `situation` — Required description of what happened.
- `user` — Optional Discord member involved in the incident.
- `action_taken` — Optional description of anything staff have already done.
  This is context for Gemini and does not cause the bot to take that action.
- `notes` — Optional additional staff context.

The output includes an incident summary, relevant rule areas, suggested
severity, discipline-tier guidance when supported, recommended next step,
internal-note draft, member-facing DM draft, and missing-context assessment.
The submitted situation is not stored in the database.

### `/modai ticketdraft <situation> [reporter] [reported_user] [channel_context]`

Drafts a private response for a support ticket or member report.

- `situation` — Required description of the report.
- `reporter` — Optional member who submitted the report.
- `reported_user` — Optional member whose behavior is being reported.
- `channel_context` — Optional explanation of where the issue happened or what
  surrounded it.

The output includes a proposed reporter reply, follow-up questions, an internal
staff-note draft, relevant rule areas, next steps, and a recommended handling
route. The submitted situation is not stored.

### `/modai rulecard <topic> [tone] [users]`

Generates a reusable rule reminder and previews it ephemerally.

- `topic` — Required rule topic, such as `unsolicited DMs`, `self-promo`, or
  `keeping politics civil`.
- `tone` — Optional style:
  - `friendly` — Warm and community-focused. This is the default.
  - `firm` — More direct.
  - `short` — Prioritizes brevity.
  - `detailed` — Adds more explanation.
- `users` — Optional one or more pasted Discord user mentions. Only valid user
  mentions are retained; role mentions and `@everyone` are not allowed.

The preview includes a **Send Rule Reminder** button. Clicking it posts the
reminder embed in the channel where the command was run. If `users` was
provided, those users are mentioned in the public message outside the embed so
they receive a notification.

Only the staff member who generated the card or an administrator can click the
send button. The public reminder always starts with `RULE REMINDER!` and ends
with a link to the rules channel.

### `/rulecard draft <topic_or_issue> [mentioned_user] [tone] [channel] [source_message_link]`

Creates an AI-assisted rule reminder draft using public and staff AI KB chunks.
The preview is ephemeral and includes the public card, private staff note,
matched sources, target channel, and mention behavior. It never posts
automatically.

The preview buttons are `Post without mention`, `Post with mention(s)`, and
`Discard`. The **Draft Rule Reminder** message context menu starts the same
flow from a selected Discord message and defaults the selected message author
as the optional mention target. The selected message is private AI context only
and is not quoted publicly in the posted card.

### `/modai patterncheck <user>`

Reviews existing structured staff records for possible recurring patterns.

- `user` — The member whose stored records should be reviewed.

This command reads only active entries in `staff_notes` and metadata in
`modai_reviews`. It does not scan the member's Discord message history. If
there is insufficient structured history, it says so rather than inventing a
pattern.

## ModAI message context menus

Open a message's context menu in Discord and choose **Apps** to find these
actions.

### `Analyze for Mod Review`

Privately analyzes the selected message with up to five previous messages from
the same channel for context. Nearby bot messages are skipped unless the
selected message itself was posted by a bot.

The prompt includes the selected message's author, channel, timestamp, jump
link, and content. The bot stores limited review metadata for pattern checking,
but it does not store the selected message content.

### `Draft Staff Response`

Privately creates several possible staff replies for the selected message:

- Public-channel response
- Private-DM response
- Softer version
- Firmer version
- Relevant rule area
- Missing-context assessment

It uses the same nearby-message context as the review action. It does not post
the draft publicly or store the selected message content.

## Staff-note commands

Staff notes are manually written private records. They are intended as
lightweight staff memory, not automated surveillance.

### `/staffnote add <user> <note>`

Adds a staff note.

- `user` — Member the note is about.
- `note` — Staff-written note, up to 2,000 characters.

The response includes the new note ID, which is used by the edit and delete
commands.

### `/staffnote view <user>`

Shows the newest active notes for a member, including each note's ID, date,
author, text, and edited date when applicable.

- `user` — Member whose notes should be displayed.

Up to 50 active notes are returned in multiple private embeds.

### `/staffnote edit <note_id> <note>`

Replaces the text of an active note.

- `note_id` — Numeric ID shown by the add, view, or summary command.
- `note` — Complete replacement text, up to 2,000 characters.

Only an administrator or the original note author can edit a note.

### `/staffnote delete <note_id>`

Soft-deletes a note. The database row remains present but no longer appears in
normal views or pattern checks.

- `note_id` — Numeric ID of the active note to remove.

Only administrators can use this command.

### `/staffnote summary <user>`

Shows a concise, non-AI summary of a member's active notes.

- `user` — Member whose notes should be summarized.

The summary includes the note count, date range, and five newest notes.

## Voice-channel stats commands

The VC stats module tracks non-bot members while BroEdenBot is online. Joining,
leaving, and switching voice channels creates live session records in
`data.db`. Older sessions can be reconstructed separately from a
DiscordChatExporter JSON export of the VC log channel; see
[docs/vc-log-imports.md](docs/vc-log-imports.md).

All `/vcstats` and `/vcrewards` responses are ephemeral. The module records all
completed sessions but separately calculates reward-eligible time for future
use. It does not call MEE6, run MEE6 commands, or grant MEE6 XP directly.

A session can earn reward-eligible time when the member was not in the
server's configured AFK channel and was not alone for the entire session.
Self-muted, self-deafened, server-muted, and server-deafened intervals are
subtracted before the five-minute minimum is checked. A best-effort heartbeat
updates active sessions once per minute. Time while the bot is offline is not
counted.

Historical imports are included by default in tracked-time totals. They are not
reward eligible and cannot generate VC XP pulses because old logs cannot prove
AFK, alone, mute, or deafen state.

The `user`, `leaderboard`, `channel`, and `export` commands support:

- `source:all` — Live plus imported sessions. This is the default.
- `source:live` — Sessions tracked by BroEdenBot while online.
- `source:imported` — Sessions reconstructed from the historical VC log.

### `/vcstats user <user> [days] [source]`

Shows a member's total tracked time, reward-eligible time, session count, top
voice channel, and average session length.

- `user` — Member whose VC activity should be displayed.
- `days` — Optional lookback period from 1 to 3,650 days. Defaults to 30.
- `source` — Optional `all`, `live`, or `imported` filter. Defaults to `all`.

### `/vcstats leaderboard [days] [limit] [eligible_only] [include_left_members] [source]`

Shows a wide member leaderboard graphic with avatars, medal ranks, duration
bars, and tracked-time or reward-eligible-time totals.

Ranks members by tracked or reward-eligible VC time.

- `days` — Optional lookback period. Defaults to 30.
- `limit` — Optional number of members from 1 to 25. Defaults to 10.
- `eligible_only` — When true, ranks by reward-eligible time instead of all
  tracked time. Defaults to false.
- `include_left_members` — Defaults to false, so only members currently cached
  in the server are ranked. Set true to include historical users who left.
- `source` — Optional `all`, `live`, or `imported` filter. Defaults to `all`.
  Name-only historical users require `include_left_members:true`.

### `/vcstats current`

Shows members currently tracked in voice channels, including their channel,
current session duration, and available mute/deafen status.

### `/vcstats channel [channel] [days] [source]`

Shows activity for one voice channel or the ten most-used voice channels.

- `channel` — Optional voice channel. Leave blank to show the top channels.
- `days` — Optional lookback period. Defaults to 30.
- `source` — Optional `all`, `live`, or `imported` filter. Defaults to `all`.

### `/vcstats export [days] [user] [channel] [include_left_members] [source]`

Exports completed VC sessions to an ephemeral CSV attachment.

- `days` — Optional lookback period. Defaults to 30.
- `user` — Optional member filter.
- `channel` — Optional voice-channel filter.
- `include_left_members` — Defaults to false. When true, sessions for users who
  left remain in the CSV and `is_current_member` identifies them.
- `source` — Optional `all`, `live`, or `imported` filter. Defaults to `all`.

The CSV includes member and channel identifiers, timestamps, tracked and
counted durations, eligibility, best-effort mute/deafen/alone flags, source,
historical confidence, estimation status, and source filename.

### `/vcstats reset <confirm>`

Clears completed and active VC sessions for the current server when `confirm`
is true. This is administrator-only and does not clear future reward snapshot
tables, imported historical sessions, or VC XP pulse/accounting tables.

### `/vcstats settings`

Shows the current reward-preparation rules, including minimum eligible session
length, AFK and alone-session exclusions, and the muted/deafened XP exclusion.

## VC XP role-pulse commands

The optional VC XP bridge turns active, eligible VC time into Discord role
pulses. A separately configured MEE6 automation watches for that role being
added, awards MEE6 XP, and removes the role afterward. BroEdenBot never
connects to MEE6, grants MEE6 XP directly, or manages VC level roles.

Automatic pulses are disabled by default. When enabled and configured, the bot
checks every five minutes, adds the configured trigger role only after a member
has accumulated `VC_XP_PULSE_MINUTES` eligible VC minutes, and then leaves that
role alone for MEE6 to process. Eligible minutes carry across separate VC
sessions after the VCXP reward-start cutoff. BroEdenBot skips members who are
self-muted, server-muted, self-deafened, server-deafened, bots, or carrying the
server bot role `1282775339566895239`. A member does not receive another pulse
until another full interval of eligible VC time has passed, and the bot skips
the add if the member already has the trigger role.

Discord role adds can fail temporarily when the host loses DNS or network
connectivity. BroEdenBot retries transient connection failures twice within the
same pulse check before recording `add_failed`; an unsuccessful pulse remains
unpaid and is eligible again on the next five-minute check. The Overview page
and `/vcrewards audit` show successful and failed role-add totals for the last
24 hours so staff can distinguish eligibility skips from Discord connectivity
problems. The dashboard labels this condition `Degraded`: automatic pulses are
still enabled, but recent Discord role-add requests failed. Owners and admins
can use **Clear failed XP pulses** to delete only the `add_failed` audit rows
after reviewing them. Clearing failures does not change eligible time, paid
pulse accounting, successful role-add history, or Discord roles.

VC XP has a reward-start cutoff so old tracked history does not create
back-pay pulses. On first startup, if `VCXP_REWARD_START_AT` is not already
set, BroEdenBot stores the current UTC timestamp and only completed sessions
after that timestamp can earn VC XP. Set `VCXP_REWARD_START_AT` manually only
when staff intentionally want a different cutoff.

### `/vcrewards settings`

Shows whether automatic pulses are enabled, the configured trigger role,
reward-start cutoff, eligible minutes per pulse, current configuration status,
and how many VCXP-only excluded roles are configured.

### `/vcrewards audit [include_left_members]`

Runs a read-only safety audit of the role-pulse bridge. It reports whether
VCXP is enabled, whether the trigger role exists and is manageable, pulse
interval, members currently holding the trigger role, and recent pulse
statuses/errors, plus successful and failed role-add totals for the last 24
hours. It never grants or removes roles, calls MEE6, starts a payout, or changes
VCXP accounting.

### `/vcrewards preview [days] [include_left_members]`

Shows eligible VC time in the selected lookback period and cumulative pulse
accounting for up to 25 members.

- `days` — Optional lookback period for displayed eligible time. Defaults to 7.
- `include_left_members` — Defaults to false. Set true to include historical
  users who are no longer in the server.

The earned, paid, and unpaid columns are legacy audit fields from the older
accounting model. Automatic role pulses now use active eligible VC time instead
of this backlog.

### `/vcrewards pulse <user> [pulses]`

Administrator-only test command that runs one or more real trigger-role pulses.
Automatic VC XP must be enabled and the trigger role must be configured.

- `user` — Member who should receive the trigger role.
- `pulses` — Optional number of sequential pulses from 1 to 10. Defaults to 1.

Each pulse attempts to add the configured trigger role and records the attempt
in the pulse log. BroEdenBot does not remove the role after a manual pulse.

### `/vcrewards export [days] [include_left_members]`

Exports eligible time and legacy pulse-accounting columns to an ephemeral CSV
attachment. The export defaults to current members only and includes an
`is_current_member` column.

## Configuring the VC XP trigger role

1. In Discord, create a role such as `VCxp`.
2. Keep the role free of permissions unless your server specifically needs
   them.
3. Place BroEdenBot's highest role above `VCxp` in the server role list.
4. Ensure BroEdenBot has the **Manage Roles** permission.
5. Enable Developer Mode in Discord, right-click `VCxp`, and choose
   **Copy Role ID**.
6. Put that ID in `VCXP_TRIGGER_ROLE_ID`.
7. Leave `VCXP_ENABLED=false` until staff have verified the settings and test
   workflow.

MEE6 automation is configured separately. A typical automation watches for a
member receiving `VCxp`, awards a small amount of XP, and removes `VCxp`.
Configure MEE6 to trigger on the role being added.

### Testing the VC XP bridge

Start with automatic role changes disabled:

```env
VCXP_ENABLED=false
VCXP_TRIGGER_ROLE_ID=
VCXP_REWARD_START_AT=
VC_XP_PULSE_MINUTES=30
```

Restart the bot and run `/vcrewards settings` and `/vcrewards preview`. These
read-only commands should work, while `/vcrewards pulse` should refuse to add a
role.

After creating the `VCxp` role and copying its ID, set:

```env
VCXP_TRIGGER_ROLE_ID=YOUR_COPIED_ROLE_ID
VCXP_ENABLED=true
```

Restart the bot, then run `/vcrewards pulse user:@me pulses:1`. Verify that
`VCxp` appears on the member. MEE6 should award XP and remove `VCxp`; BroEdenBot
will not remove it.

Before leaving automation on, run `/vcrewards audit` and check the local web
dashboard Overview page. The dashboard's VC XP readiness card summarizes the
stored trigger role ID, latest role snapshot name when available, pulse
interval, reward-start cutoff, VCXP-only excluded-role count, legacy backlog
snapshot, and successful/failed role-add activity. A `Degraded` status means
recent role adds failed; use `/vcrewards audit` to see the stored error types.
The Discord audit remains the source of truth for live role hierarchy and
Manage Roles checks.

## Stats commands

Tracked stats pages update when relevant membership changes occur. Role
rosters and reports can contain an **Export Members to CSV** button; authorized
stats users receive that export privately.

All generated stats and leaderboard PNGs share the centralized Bro Eden visual
system: dark dashboard-derived tokens, consistent type and spacing, bounded
text truncation, avatar fallbacks, explicit empty states, and per-file size
validation. Leaderboards use mobile-readable portrait pages with 10 rows;
role rosters use 12 rows. Longer results create additional PNGs instead of
shrinking every row. See [Stats Visual System](docs/stats-visual-system.md) for
profiles, components, diagnostics, tests, and the sample generator.

### `/stats role <role> [channel] [image]`

Creates a tracked graphical roster of everyone who currently has a role.

- `role` — Role whose members should appear.
- `channel` — Optional destination text channel. Defaults to the current
  channel.
- `image` — Optional image attachment used as the roster banner. It must be an
  image and no larger than 8 MB.

After running the command, a modal asks for:

- `Header` — Optional title, up to 100 characters. Defaults to
  `<role name> Members`.
- `Body` — Optional supporting text, up to 500 characters.

### `/stats banner <tracker> <image>`

Replaces the saved banner bytes for an existing tracked role roster and
refreshes the same Discord post in place. The image must be 8 MB or smaller;
the tracker field autocompletes active role rosters in the current server.

### `/stats refresh`

Immediately refreshes every tracked stats page in the server, including role
rosters, role audits, and channel-posted `/stats activity` reports. It has no
inputs. The same tracked pages also refresh automatically once daily at
12:00 UTC.

### `/stats rolecompare <role_1> <role_2> [title] [body] [channel]`

Creates a tracked visual comparison between two roles.

- `role_1` — First role to compare.
- `role_2` — Second role to compare.
- `title` — Optional report title, up to 100 characters. Defaults to
  `<role 1> vs <role 2>`.
- `body` — Optional explanation, up to 500 characters.
- `channel` — Optional destination text channel. Defaults to the current
  channel.

### `/stats missingrole <has_role> <missing_role> [title] [body] [channel]`

Creates a tracked audit of members who have one role but do not have another.

- `has_role` — Role members must currently possess.
- `missing_role` — Role those members must not possess.
- `title` — Optional report title, up to 100 characters. Defaults to
  `Missing <missing role>`.
- `body` — Optional explanation, up to 500 characters.
- `channel` — Optional destination text channel. Defaults to the current
  channel.

### `/stats delete`

Opens a private selection menu listing tracked stats pages. An administrator
can choose one page or choose the option to delete all pages. Deletion removes
the tracked database entry and attempts to delete the associated Discord
message.

### `/stats reset`

Deletes every tracked stats page in the server without presenting the
selection menu. This is an administrator-only bulk operation.

## Stats activity commands

Activity reports use the same permissions as other stats creation and refresh
commands. Visual reports have an optional `channel` parameter:

- Leave `channel` blank to receive the report ephemerally.
- Select a text channel to post the report there for other staff to see. The
  command runner receives an ephemeral confirmation.

Channel-posted activity reports are saved as tracked pages. `/stats refresh`
rebuilds and edits them alongside role rosters, role comparisons, and missing
role reports. The bot also refreshes every tracked stats page automatically
once daily at 12:00 UTC.

Activity reports posted before this tracking upgrade remain ordinary snapshots.
Recreate each old report once with its `channel` option to make it refreshable.

CSV exports remain ephemeral by default because they contain member-level
activity metadata. Staff can explicitly select a `channel` to post an export
there when appropriate; CSV messages are not tracked dashboards.

Text activity is tracked from the time this feature is deployed. The bot stores
hourly counts and basic member/channel metadata only. It does **not** store
message content, deleted-message content, or private DMs. Bot messages are
ignored.

Join and leave events are also tracked going forward, so reports covering
periods before deployment may be incomplete.

Activity reports support these preset `period` choices:

- `7 days`
- `30 days`
- `90 days`
- `365 days`
- `all time`

`all time` removes the date cutoff and includes every available row matching
the selected `source` (`all`, `live`, or `imported`). The older custom `days`
input remains supported. If both `period` and `days` are supplied, `period`
takes priority.

### `/stats activity overview [period] [days] [source] [channel]`

Shows a community overview containing:

- Total tracked messages
- Unique active members
- Joins and leaves
- Top text channel
- Busiest and quietest UTC dates
- Tracked VC time and top VC channel, when available
- A deterministic volume/concentration summary

If neither `period` nor `days` is supplied, the report defaults to 7 days. The
overview displays the actual available data range. All-time reports are labeled
`Period: All time`.

### `/stats activity channels [period] [days] [limit] [source] [channel]`

Shows the top text channels by tracked message count, including unique posters
and percentage of tracked messages. This report renders as a wide ranked PNG
with medal colors, activity bars, and compact metric cards.

- If neither `period` nor `days` is supplied, the report defaults to 7 days.
- `limit` defaults to 10 and supports up to 25.

### `/stats activity trends [period] [source] [channel]`

Compares `7 days`, `30 days`, `90 days`, or `365 days` with the immediately
preceding period of equal length.

- `period` defaults to 30 days; all-time trends are intentionally unsupported.
- `source` defaults to `all` and can be `live` or `imported`.
- Shows message/member percentage changes, growing and declining channels, and
  the current period's busiest and quietest tracked dates.
- If the previous window has no matching rows, current statistics are still
  shown and the comparison is labeled limited.

### `/stats activity categories [period] [source] [limit] [channel]`

Groups activity using `data/channel_categories.json`.

- `period` defaults to 30 days and supports all time.
- `source` defaults to `all`; `limit` defaults to 10 and supports up to 25.
- Unconfigured channels are grouped as `Uncategorized`.
- Channels with `include_in_activity: false` are excluded.
- Each category shows messages, unique members, percentage, and top channel.

### `/stats activity heatmap [period] [source] [timezone] [channel]`

Summarizes hourly activity by local day, hour, top day/hour combinations, and
Overnight/Morning/Afternoon/Evening blocks.

- `period` defaults to 30 days and supports all time.
- `source` defaults to `all`.
- `timezone` accepts an IANA timezone and defaults to `America/Chicago`.
  Invalid names safely fall back to `America/Chicago`.

### `/stats activity quiet [period] [days] [limit] [source] [channel]`

Shows visible text channels with low tracked activity and their last tracked
activity time. This is intended for neutral channel-planning decisions and
does not assess or shame individual members. It renders as a quietest-first
graphic with message totals and last-activity context.

- If neither `period` nor `days` is supplied, the report defaults to 14 days.
- `limit` defaults to 10 and supports up to 25.

### `/stats activity members [period] [days] [limit] [source] [include_left_members] [channel]`

Shows members with the highest tracked message counts. Text and VC activity are
not combined into a synthetic score. It renders as a wide member leaderboard
with avatars, display names, usernames, medal ranks, and message-count bars.

- If neither `period` nor `days` is supplied, the report defaults to 7 days.
- `limit` defaults to 10 and supports up to 25.
- `include_left_members` defaults to false. Current guild members are detected
  from Discord's member cache and their current display names are preferred.
  Set it true to include historical users who left the server.

### `/stats activity vc [period] [days] [limit] [include_left_members] [channel]`

Reads completed sessions from the `vc_sessions` table managed by
`cogs/vc_stats.py`. It shows total tracked time, completed sessions, top voice
channels, and top voice participants in a split-screen PNG leaderboard.

If the VC tracking table is unavailable, the command reports:
`VC activity tracking is not available yet.`

The VC report also supports the preset periods, including all available tracked
VC history with `period:all time`.
Its top-member ranking defaults to current members only; set
`include_left_members:true` to include historical users who left. Aggregate VC
time and channel totals exclude configured VC-excluded users.

### `/stats activity export [period] [days] [include_vc] [source] [include_left_members] [channel]`

Exports a private CSV with a `section` column. Sections can include:

- Overview metrics
- Hourly message metadata
- Channel summaries
- Member summaries
- Joins
- Leaves
- VC sessions, when requested and available

If neither `period` nor `days` is supplied, the export defaults to 7 days.
`include_vc` defaults to true. All-time exports use `all_time` in the filename.
Large exports may exceed Discord's upload limit; if that happens, choose a
shorter period.

User-level export rows default to current members only. Set
`include_left_members:true` to include historical users; user rows include an
`is_current_member` column. Aggregate overview and channel totals exclude
configured activity-excluded users.

### `/stats activity importinfo [limit] [channel]`

Shows recent historical import batches. If posted to a channel, the report is
tracked and updates through `/stats refresh` and the daily automatic refresh.

### Activity database tables

The activity feature creates these tables in `data.db`:

- `stats_message_activity`
- `stats_member_joins`
- `stats_member_leaves`
- `stats_activity_settings`
- `tracked_activity_reports`

`stats_message_activity` uses one row per guild, text channel, member, and UTC
hour, incrementing `message_count` as messages arrive.

### Bot/stat exclusion role

Set these values to exclude bot accounts from message activity and VC stats by
their shared Discord role:

```dotenv
ACTIVITY_EXCLUDED_ROLE_IDS=1282775339566895239
VC_EXCLUDED_ROLE_IDS=1282775339566895239
VCXP_EXCLUDED_ROLE_IDS=
VCXP_EXCLUDED_VOICE_CHANNEL_IDS=
EXCLUDED_VOICE_CHANNEL_IDS=
VC_EXCLUDED_USER_IDS=983091180885643326,716390085896962058
```

Live message activity ignores bot-authored messages, `ACTIVITY_EXCLUDED_USER_IDS`,
and members with any `ACTIVITY_EXCLUDED_ROLE_IDS`. Live VC tracking ignores bots,
`VC_EXCLUDED_USER_IDS`, and members with any `VC_EXCLUDED_ROLE_IDS`; excluded
members do not receive VC rewards or automatic VC XP pulses. Use
`VCXP_EXCLUDED_ROLE_IDS` when a role should be visible in VC stats but should
not earn VC XP pulses. Use `VCXP_EXCLUDED_VOICE_CHANNEL_IDS` when a voice
channel should remain visible in VC stats but should not count toward VC XP
pulses. The Voice dashboard also ignores `VC_EXCLUDED_USER_IDS`,
`EXCLUDED_VOICE_CHANNEL_IDS`, and rows marked with `ignored_at`.

Historical exports usually do not contain role membership. Generate a current
role-member cache before importing or cleaning historical data:

```bash
python scripts/export_excluded_role_members.py --guild-id 1278253523619807233 --role-ids 1282775339566895239 --output data/excluded_bot_role_members.json
```

Use that cache with imports and cleanup:

```bash
python scripts/import_discord_history.py --folder imports/discord_history --guild-id 1278253523619807233 --excluded-user-cache data/excluded_bot_role_members.json
python scripts/import_full_csv_exports.py --folder imports/message_context --guild-id 1278253523619807233 --excluded-user-cache data/excluded_bot_role_members.json
python scripts/import_vc_logs.py --folder imports/vc_logs --guild-id 1278253523619807233 --excluded-user-cache data/excluded_bot_role_members.json
python scripts/cleanup_activity_bots.py --dry-run --excluded-user-cache data/excluded_bot_role_members.json
python scripts/cleanup_vc_bots.py --dry-run --excluded-user-cache data/excluded_bot_role_members.json
```

Real cleanup requires `--yes`; add `--vacuum` after backing up `data.db`.
Cleanup can also resolve members live with `--guild-id` plus configured or
passed role IDs when `DISCORD_TOKEN` is available. It removes rows from stats
tables only and never touches `message_context.db`.

For dashboard-specific VC cleanup, use the non-deleting marker script:

```bash
python scripts/cleanup_voice_sessions.py --dry-run
python scripts/cleanup_voice_sessions.py --apply
```

The script adds `ignored_at` / `ignored_reason` columns when applying and marks
matched rows ignored instead of deleting them. It reports duplicate rows,
invalid/impossible rows, excluded voice channels, excluded bot/user IDs, total
hours affected, and top affected channels/users. Long sessions over the audit
threshold are reported by default; pass `--include-long-sessions` only when you
intentionally want to mark those sessions ignored.

### Historical Discord activity imports

Export Discord channels with DiscordChatExporter CLI as JSON, then save the
exports locally in `imports/discord_history/`. Exported JSON and CSV files may
contain private server history and can be very large, so they must not be
committed to Git. Previously tracked exports should be removed from Git's index
with `git rm --cached` while keeping the local files ignored.

Historical imports may be run repeatedly as more channel exports are added.
The workflow remains supported for ongoing incremental channel exports.

Run a dry run first:

```bash
source .venv/bin/activate
python scripts/import_discord_history.py --folder imports/discord_history --guild-id SERVER_ID --dry-run
```

Then run the real import:

```bash
python scripts/import_discord_history.py --folder imports/discord_history --guild-id SERVER_ID
```

Re-running the importer is safe because previously imported message IDs are
deduplicated. The importer stores activity metadata only, never message
content. JSON exports are streamed one message at a time, activity buckets and
dedupe records are committed every 5,000 messages, and progress is printed
every 10,000 messages. If a later batch fails, the audit record accurately
reports the batches that were already committed. CSV exports remain supported,
including channel metadata supplied on individual rows. SQLite uses WAL mode,
a 60-second busy timeout, and bounded retries for temporary lock contention.
Stop `broedenbot` during especially large imports if lock warnings persist.

Archiving remains opt-in with `--archive-completed`; failed or incomplete files
stay available for repair or re-export.

For full-server CSV coverage, place every channel CSV in
`imports/message_context/`, then run:

```bash
python scripts/import_full_csv_exports.py --folder imports/message_context --guild-id SERVER_ID --dry-run
python scripts/import_full_csv_exports.py --folder imports/message_context --guild-id SERVER_ID --archive-completed --archive-duplicates
```

All CSV content goes to separate staff-only `message_context.db`. Counts-only
activity uses source `csv_backfill` and is added only for channel IDs not
already covered by completed JSON imports. Public `/ask` never uses the private
archive.

Historical imports: see
[docs/historical-imports.md](docs/historical-imports.md) for the complete
DiscordChatExporter workflow, Pi transfer commands, archive handling, aliases,
and troubleshooting.

Historical VC-log imports use a separate JSON workflow documented in
[docs/vc-log-imports.md](docs/vc-log-imports.md).

## Bank commands

Bank commands use the separate `brobank.db` SQLite database.

### `/bank add <user> <amount> <note>`

Records a public contribution and refreshes the configured public bank summary.

- `user` — Member credited with the contribution.
- `amount` — Positive contribution amount.
- `note` — Short, public-safe description of the contribution.
  Maximum 300 characters.

### `/bank expense <amount> <note>`

Records an expense and refreshes the configured public bank summary.

- `amount` — Positive amount spent.
- `note` — Description of what the funds supported.
  Maximum 300 characters.

### `/bank balance`

Privately shows total contributions, total expenses, and the available balance.

### `/bank leaderboard`

Publicly shows the top ten contributors based on public contribution records.

### `/bank refresh`

Creates or updates the public bank summary in the current text channel and
saves that channel/message as the configured summary location.

### `/bank setchannel`

Sets the current text channel as the public bank channel and publishes a fresh
summary there.

### `/bank clear <confirm>`

Administrator-only. Deletes all bank transactions and refreshes the configured
public summary to show the empty ledger when `confirm` is true. This is
destructive and cannot be undone through the bot.

## Leaderboard commands

Viewing leaderboards and point summaries is public. Create/edit uses **Manage
Server**; score changes use configured staff access; deletion and live
milestone-role rules are owner-only. Destructive reset/delete actions require a
private confirmation.

### `/leaderboard create <name> [image]`

Creates a named leaderboard and opens a modal for its description and accent.
An optional image attachment becomes its persisted graphical banner. Existing
leaderboards are preserved.

- `name` — Name used to identify the leaderboard, up to 50 characters.

### `/leaderboard edit <leaderboard> [image]`

Updates the description, `auto` or hex accent, and optional replacement banner.
Omitting `image` preserves the existing banner.

### `/leaderboard delete <name>`

Owner-only. Confirms before deleting a leaderboard, its point records, and its
milestone rules, then attempts to remove roles managed by those rules.

- `name` — Existing leaderboard name. Discord provides autocomplete.

### `/leaderboard add <leaderboard> <user> <points>`

Adds points to a user's existing total on a leaderboard, or creates the user's
point record when none exists.

- `leaderboard` — Existing leaderboard name, with autocomplete.
- `user` — Non-bot Discord user receiving points.
- `points` — Numeric point amount supplied as text. Negative values are treated
  as positive and values are rounded to two decimal places.

### `/leaderboard remove <leaderboard> <user> <points>`

Subtracts points from a user without allowing the total to fall below zero.

- `leaderboard` — Existing leaderboard name, with autocomplete.
- `user` — Non-bot Discord user losing points.
- `points` — Numeric point amount supplied as text. Negative values are treated
  as positive and values are rounded to two decimal places.

### `/leaderboard reset <leaderboard>`

Confirms before clearing every score while preserving the leaderboard itself,
then reconciles milestone roles.

### `/leaderboard role add|remove|list|sync`

Owner-only controls for roles awarded at point thresholds. Bro Eden needs
**Manage Roles**, its role must be above each reward role, and a managed role
can belong to only one leaderboard milestone rule.

### `/points [user]` and `!points`

Shows up to 20 leaderboard totals and ranks for the selected member or caller,
plus current/longest streak information when available.

### `/leaderboards <name>`

Displays a leaderboard as a portrait paginated graphic, ten members per page. The
graphic uses the same dark visual system as stats leaderboards, including the
uploaded banner, automatic or configured accent, avatars, medal ranks, progress
rails, point pills, and a live timestamp.
Empty leaderboards display a friendly empty state. If an avatar cannot be
downloaded, the leaderboard still renders with a placeholder.

- `name` — Existing leaderboard name, with autocomplete.

## Poll command

### `/poll <question> <options> <time>`

Creates an interactive poll in the current channel.

- `question` — Poll title/question.
- `options` — Two to 25 unique comma-separated choices, each up to 80
  characters. Example: `Friday, Saturday, Sunday`.
- `time` — Poll duration. Supported units include seconds, minutes, hours,
  days, weeks, months, and years, up to one year. Examples: `30m`, `1h`, or
  `2d`.

Members vote using compact lettered buttons and may change their vote before
the poll closes. Running the command privately confirms creation; the poll
itself is public. When time expires, the original message becomes a visual
results board instead of being deleted and reposted. Poll posting and result
updates use no allowed mentions; if the bot cannot post, the private response
points operators back to channel permissions.

## Queue slash commands

Queue state is separate for each channel.

### `/queue dashboard`

Posts a public queue dashboard in the current voice or stage channel's text
chat. The dashboard includes buttons to join, leave, delay one position, and
pull the next member.

Joining requires the member to be connected to that same voice or stage
channel.

### `/queue lock`

Locks the current channel's queue so new members cannot join. Requires
**Manage Channels**.

### `/queue unlock`

Unlocks the current channel's queue. Requires **Manage Channels**.

### `/queue move <user> <position>`

Moves an existing queue member to a numbered position.

- `user` — User already in the current channel's queue.
- `position` — Desired one-based position. Values beyond the queue length are
  safely clamped to the final position.

### `/queue remove <user>`

Removes a user from the current channel's queue.

- `user` — User to remove.
- Requires **Manage Channels**.

## Legacy queue commands

These message commands use the bot prefix `!`.

| Command | Function |
| --- | --- |
| `!q` | Posts or refreshes the current channel's queue dashboard. |
| `!qj` | Joins the queue. The caller must be in the matching queue voice channel, and the queue must be unlocked. |
| `!ql` | Leaves the current channel's queue. |
| `!qd` | Moves the caller one place later in the queue. |
| `!qn` | Pulls the first member from the queue and announces who is next. Requires **Manage Channels**. |

## Environment variables

Create a `.env` file in the project root. Do not commit it.

Secrets and boot-only values remain environment-backed. Allowlisted safe runtime
settings are seeded from `.env` into `data.db` only when a database value does
not already exist. After seeding, the database value takes priority and can be
updated from the authenticated local dashboard without rewriting `.env`.

| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Discord bot token. Required. |
| `ENABLED_MODULES` | Optional comma/space-separated module gate. Keep existing values and add `bumps,events,reminders,streaks,stats,visual`; blank loads every cog. `events` requires `reminders`; `visual` enables Discord-backed Asset Library storage. |
| `BOT_OWNER_USER_IDS` | Comma-separated Discord user IDs allowed to use `/bot` commands. |
| `BOT_OWNER_ALLOW_ADMINS` | Allows server administrators to use `/bot` when `true`. Defaults to `false`. |
| `CHECKLIST_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to use `/checklist`; bot owners are also allowed. Blank makes checklist management owner-only. |
| `REMINDER_ALLOWED_ROLE_IDS` | Legacy fallback roles for staff reminder actions. Existing configured staff/admin roles are also recognized. |
| `REMINDER_PERSONAL_ALLOWED_ROLE_IDS` | Roles allowed to use `/remind personal`. Blank allows all server members. |
| `REMINDER_EVENT_ALLOWED_ROLE_IDS` | Roles allowed to use `/remind event`. Blank uses the legacy reminder/staff role rules. |
| `REMINDER_MANAGE_ALLOWED_ROLE_IDS` | Roles allowed to open `/remind manage` for their own reminders. Blank allows all server members. |
| `REMINDER_MANAGE_ALL_ROLE_IDS` | Roles allowed to manage every reminder in the guild. Blank uses the legacy reminder/staff role rules. |
| `REMINDER_SUBSCRIPTIONS_ALLOWED_ROLE_IDS` | Roles allowed to subscribe to events and use `/remind subscriptions`. Blank allows all server members. |
| `REMINDER_TIMEZONE` | Server fallback IANA timezone for reminder date/time input. Defaults to `America/Chicago`; members can override it with `/timezone`. |
| `ENABLE_LEGACY_REMINDER_COMMANDS` | Keeps `/reminder add`, `/reminder manage`, and `/remind subscribe` transition routes available. Defaults to `true`. |
| `REMINDER_DELIVERY_GRACE_MINUTES` | Maximum age of a missed delivery that may be caught up after downtime. Defaults to `120`; valid runtime range is 1–1440 minutes. |
| `REMINDER_EVENT_AUTO_SUBSCRIBE_CREATOR` | Automatically subscribes an event creator to the event defaults. Defaults to `true`. |
| `EVENTS_HEADER_ASSET_ID` | Saved Embed/Message Editor asset used as the `/events` header. Blank uses the built-in Upcoming Events card. Supports `{count}` and `{next_event}` placeholders. |
| `EVENTS_ARTWORK_STORAGE_CHANNEL_ID` | Private Discord forum, thread, or text channel used for artwork uploaded through the Events dashboard. Configure it from **Features → Events**. The bot retains the Discord attachment link and clears pending image bytes. |
| `DISBOARD_BOT_USER_ID` | Official DISBOARD bot user ID trusted for verified success responses. |
| `BUMP_REWARD_ROLE_ID` | Role granted after a verified bump for the external XP/reward handoff. |
| `BUMP_SUCCESS_ASSET_ID` | Optional Embed/Message Editor asset sent after a verified bump. Supports `{user.feature}`, `{role.feature}`, `{member}`, `{points}`, and `{reward_status}`. |
| `BUMP_PING_ROLE_ID` | Role mentioned through `{role.feature}` in automatic two-hour bump reminders. |
| `BUMP_REMINDER_ASSET_ID` | Optional complete Embed/Message Editor asset for the two-hour reminder. Its configured content, embed, and buttons are sent without an automatic subscription button. |
| `BUMP_LEADERBOARD_CHANNEL_ID` | Channel receiving the seven-day Bump Legends post. |
| `BUMP_POINTS_PER_SUCCESS` | Bump points awarded per verified success. Defaults to `1000`. |
| `STREAK_TIMEZONE` | IANA timezone used for daily streak boundaries. Defaults to `America/Chicago`. |
| `STREAK_MIN_WORDS` | Minimum word count for a qualifying streak message. Defaults to `4`. |
| `STREAK_DUPLICATE_LOOKBACK_DAYS` | Exact-message duplicate lookback window. Defaults to `30`. |
| `STREAK_EXCLUDED_CHANNEL_IDS` | Additional channels excluded from streak qualification. |
| `STREAK_EXCLUDED_CATEGORY_IDS` | Categories whose channels are excluded from streak qualification. |
| `STREAK_MILESTONE_CHANNEL_ID` | Optional channel for automatic milestone announcements. Leave blank for private-only milestone details. |
| `STREAK_MILESTONE_ASSET_ID` | Optional Embed/Message Editor asset for milestone announcements. Supports `{user.feature}`, `{member}`, and `{days}`. |
| `STREAK_LEADERBOARD_CHANNEL_ID` | Channel receiving the persistent weekly streak tracker. |
| `STREAK_RESTORE_ENABLED` | Enables automatic history recovery after a heartbeat gap. Defaults to `true`. |
| `STREAK_RESTORE_GAP_MINUTES` | Missing-heartbeat duration that queues automatic recovery. Defaults to `10`. |
| `STREAK_RESTORE_MAX_DAYS` | Maximum date span accepted by one automatic or dashboard restore. Defaults to `14`. |
| `STREAK_RESTORE_MAX_MESSAGES` | Maximum Discord messages scanned by one restore request. Defaults to `50000`. |
| `LEADERBOARD_RESET_ROLE_IDS` | Additional roles allowed to reset custom leaderboard points. |
| `AUDIT_LOG_THREAD_ID` | Optional existing thread for selected mention-safe leaderboard audit events. |
| `GEMINI_API_KEY` | Gemini API key used by `/ask`, `/staffai`, `/context`, and ModAI. |
| `AI_ENABLED` | Enables shared AI framework calls when `true`. Missing `GEMINI_API_KEY` still disables framework calls. Defaults to `true`. |
| `AI_MODEL_FAST` | Fast/cheap framework model tier. Defaults to `gemini-2.5-flash-lite`. |
| `AI_MODEL_DEFAULT` | Default framework model tier. Defaults to `gemini-2.5-flash`. |
| `AI_MODEL_ADVANCED` | Advanced framework model tier. Defaults to `gemini-3-flash-preview`. |
| `AI_ENABLE_ADVANCED_MODEL` | Allows the advanced tier when `true`; otherwise advanced requests fall back to default. Defaults to `false`. |
| `AI_DAILY_BUDGET_USD` | Daily estimated AI framework budget. Defaults to `0.35`. |
| `AI_MONTHLY_BUDGET_USD` | Monthly estimated AI framework budget. Defaults to `10.00`. |
| `AI_MAX_INPUT_TOKENS` | Maximum estimated framework input tokens. Defaults to `12000`. |
| `AI_MAX_OUTPUT_TOKENS` | Ceiling on framework output tokens per call (billed on actual usage, not the cap). Defaults to `6144`; must exceed `AI_STRUCTURED_THINKING_BUDGET` plus the answer size. |
| `AI_STRUCTURED_THINKING_BUDGET` | Thinking-token cap for reasoning-heavy calls like `/context` summaries. Billed as output tokens, so this is the main cost lever. `0` disables thinking (fields come back empty); higher values improve quality at higher cost. Defaults to `512`. |
| `AI_DEFAULT_TEMPERATURE` | Default framework generation temperature. Defaults to `0.4`. |
| `AI_MEMBER_COOLDOWN_SECONDS` | Reusable member-facing AI cooldown helper value. Defaults to `20`. |
| `AI_STAFF_COOLDOWN_SECONDS` | Reusable staff/admin AI cooldown helper value. Defaults to `5`. |
| `AI_LOG_PROMPTS` | Logs full framework prompts only when explicitly `true`. Defaults to `false`. |
| `AI_LOG_RESPONSES` | Logs full framework responses only when explicitly `true`. Defaults to `false`. |
| `AI_DASHBOARD_VISIBLE` | Shows the dashboard AI page/tab when `true`. Defaults to `true`. |
| `ASK_MODEL` | Optional primary Gemini model for `/ask`; falls back to `MODAI_MODEL`. |
| `ASK_FALLBACK_MODEL` | Optional fallback Gemini model for `/ask`; falls back to `MODAI_FALLBACK_MODEL`. |
| `ASK_ALLOWED_CHANNEL_IDS` | Optional comma- or space-separated channel IDs where `/ask` may be used. Blank allows all channels. |
| `ASK_COOLDOWN_SECONDS` | Optional per-user `/ask` cooldown in seconds. Defaults to `30`. |
| `MODAI_MODEL` | Primary Gemini model. |
| `MODAI_FALLBACK_MODEL` | Model tried after retryable primary-model failures. |
| `MODAI_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to use ModAI. |
| `STAFF_AI_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to use private `/staffai` commands. Bot owners are also allowed. |
| `STAFF_AI_MODEL` | Optional primary Gemini model for `/staffai`; falls back to `MODAI_MODEL`. |
| `STAFF_AI_FALLBACK_MODEL` | Optional fallback Gemini model for `/staffai`; falls back to `MODAI_FALLBACK_MODEL`. |
| `STAFF_CONTEXT_ENABLED` | Enables allowlisted live staff-message capture when `true`. Defaults to `false`. |
| `STAFF_CONTEXT_CHANNEL_IDS` | Comma- or space-separated staff channel IDs allowed for live capture. |
| `STAFF_CONTEXT_DB_PATH` | Optional staff-context database path. Defaults to `staff_context.db`. |
| `STAFF_CONTEXT_TRACK_DELETES` | Marks stored live messages deleted when Discord reports deletion. Defaults to `true`. |
| `MESSAGE_CONTEXT_ENABLED` | Enables full-server live message-content capture when `true`. Defaults to `false`. |
| `MESSAGE_CONTEXT_CHANNEL_IDS` | Optional included channel/parent IDs. Blank tracks all visible guild channels except exclusions. |
| `MESSAGE_CONTEXT_EXCLUDED_CHANNEL_IDS` | Channel/parent IDs that must never be captured. |
| `MESSAGE_CONTEXT_ALLOWED_ROLE_IDS` | Staff role IDs allowed to use `/context`. Bot owners are also allowed. |
| `MESSAGE_CONTEXT_DB_PATH` | Optional archive path. Defaults to `message_context.db`. |
| `MESSAGE_CONTEXT_TRACK_DELETES` | Marks captured messages deleted when Discord reports deletion. Defaults to `true`. |
| `MESSAGE_CONTEXT_TRACK_EDITS` | Updates captured rows to the latest message content. Defaults to `true`. |
| `MESSAGE_CONTEXT_IGNORE_BOTS` | Ignores bot-authored messages. Defaults to `true`. |
| `MESSAGE_CONTEXT_RETENTION_DAYS` | Optional positive retention period. Blank retains indefinitely. |
| `MESSAGE_CONTEXT_MODEL` | Optional primary Gemini model for `/context`; falls back to `MODAI_MODEL`. |
| `MESSAGE_CONTEXT_FALLBACK_MODEL` | Optional fallback Gemini model for `/context`; falls back to `MODAI_FALLBACK_MODEL`. |
| `STAFF_NOTES_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to use staff notes. |
| `VCSTATS_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to use VC stats and reward previews. |
| `ACTIVITY_EXCLUDED_ROLE_IDS` | Comma-separated role IDs excluded from message activity stats. Recommended bot/stat exclusion role: `1282775339566895239`. |
| `ACTIVITY_EXCLUDED_USER_IDS` | Comma-separated user IDs excluded from message activity stats as a fallback. |
| `VC_EXCLUDED_ROLE_IDS` | Comma-separated role IDs excluded from VC stats, VC leaderboards, and VC rewards. Recommended bot/stat exclusion role: `1282775339566895239`. |
| `VC_EXCLUDED_USER_IDS` | Comma-separated user IDs excluded from VC stats and rewards as a fallback. |
| `EXCLUDED_VOICE_CHANNEL_IDS` | Comma-separated voice channel IDs excluded from dashboard voice analytics and cleanup marking. |
| `VCREWARDS_ALLOWED_ROLE_IDS` | Optional role IDs allowed to use `/vcrewards audit`. Falls back to `VCSTATS_ALLOWED_ROLE_IDS`. |
| `VCXP_TRIGGER_ROLE_ID` | Discord role ID temporarily added for each VC XP pulse. |
| `VCXP_EXCLUDED_ROLE_IDS` | Comma-separated role IDs excluded from VC XP pulses only. Members with these roles remain visible in VC stats unless also excluded by `VC_EXCLUDED_ROLE_IDS`. |
| `VCXP_EXCLUDED_VOICE_CHANNEL_IDS` | Comma-separated voice channel IDs excluded from VC XP pulse eligibility only. Sessions in these channels remain visible in VC stats. |
| `VCXP_REWARD_START_AT` | ISO timestamp for the earliest completed VC session that can earn VC XP. Defaults to the first bot startup time after this setting exists, preventing historical back-pay pulses. |
| `VC_XP_PULSE_MINUTES` | Eligible VC minutes required per pulse across sessions. Defaults to `30`. |
| `VCXP_ENABLED` | Enables automatic and manual role pulses when `true`. Defaults to `false`. |
| `BANK_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to use bank commands. |
| `STATS_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to create and refresh stats pages. |
| `STATS_IMAGE_TARGET_BYTES` | Maximum accepted size for each generated stats PNG. Defaults to `8000000` (8 MB); oversized pages are optimized or re-rendered within the documented minimum dimensions, and are never silently uploaded above the target. |
| `VISUAL_ASSET_DIR` | Persistent normalized uploads for Visual Content Studio. Defaults to `data/visual-assets`; the bot and dashboard must use the same path. |
| `VISUAL_ASSET_STORAGE_THREAD_ID` | Existing private Discord forum-post/thread ID used as the durable source for Visual Content Studio Asset Library images. Configure under **Features → Visual Content Studio**. |
| `VISUAL_RENDER_CONCURRENCY` | Maximum concurrent centralized ranked/roster renders, clamped from `1` to `4`. Defaults to `2` for Raspberry Pi stability. |
| `DASHBOARD_ENABLED` | Enables the local dashboard when `true`. |
| `DASHBOARD_HOST` | Dashboard bind address. Use `0.0.0.0` for access from the local network. |
| `DASHBOARD_PORT` | Dashboard port. Defaults to `3000`. |
| `DASHBOARD_USERNAME` | Local dashboard login username. |
| `DASHBOARD_PASSWORD` | Local dashboard login password. Use a unique password. |
| `DASHBOARD_SECRET_KEY` | Long random key used to sign dashboard sessions. |
| `DASHBOARD_COOKIE_SECURE` | Set to `true` when the dashboard is served over HTTPS (e.g. behind a TLS reverse proxy) so the session cookie is only sent over secure connections. Leave `false` for plain-HTTP LAN access. |
| `DASHBOARD_AUTH_MODE` | Set to `discord` to enable Discord OAuth while retaining password fallback. |
| `DISCORD_OAUTH_CLIENT_ID` | Discord application OAuth2 client ID. |
| `DISCORD_OAUTH_CLIENT_SECRET` | Discord application OAuth2 client secret. Keep only in `.env`. |
| `DISCORD_OAUTH_REDIRECT_URI` | Exact OAuth callback URL, such as `https://dashboard.broeden.com/auth/discord/callback`. |
| `DASHBOARD_DISCORD_ALLOWED_USER_IDS` | Comma- or space-separated Discord user IDs approved for dashboard login. |
| `DASHBOARD_DISCORD_ALLOWED_ROLE_IDS` | Compatibility list of current Discord guild roles allowed to log in. Prefer database-backed mappings in Dashboard Access. |
| `DASHBOARD_DISCORD_DEFAULT_ROLE` | Role assigned to new approved Discord users: `admin` or `viewer`. |
| `DASHBOARD_DISCORD_REVERIFY_MINUTES` | Maximum age of verified Discord membership before fresh OAuth is required. Defaults to `60`; clamped to 5–1440 minutes. |
| `DATABASE_PATH` | Optional shared SQLite path for the dashboard. Defaults to the existing `data.db`, then common local database names. |
| `BANK_DATABASE_PATH` | Optional shared bank SQLite path for the bot and dashboard. Defaults to `brobank.db`. Use an absolute path on persistent-volume deployments. |

## Run locally

Python 3.11 or newer is recommended. Python 3.9 is end-of-life and current
Google libraries emit compatibility warnings on it.

```bash
cd ~/Documents/BroEdenBot
source .venv/bin/activate
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

The bot requests only guild, guild-expression, guild-message, voice-state,
member, and message-content gateway events. Enable **Server Members Intent**
and **Message Content Intent** in the Discord Developer Portal so VC/member tracking, legacy queue
commands, and the explicitly enabled private context archives work correctly.
Presence, typing, invite, integration, webhook, and other unrelated intents are
not requested.

For dashboard event publishing, grant the bot **Create Events** and **Manage
Events**. Stage and Voice events also require access to the selected channel;
keep normal **View Channel** and **Connect** permissions available there. The
private artwork destination needs **View Channel**, **Attach Files**, and
**Send Messages** or **Send Messages in Threads** as appropriate. The Events
readiness panel reports live event and storage permissions, eligible channels,
synchronization age, mappings, pending work, and recent failures.

Visual Content Studio storage uses one existing private Discord forum
post/thread. The bot needs **View Channel**, **Read Message History**, **Attach
Files**, and **Send Messages in Threads** there, plus **Manage Threads** if it
must reopen an archived post. The bot creates no forum structure itself.

To test `/ask` in Discord after startup:

```text
/ask question: how do I get NSFW access?
/ask question: how do I open a ticket?
/ask question: where do I ask for help?
/ask question: can staff ban someone for this?
/ask question: who reported me?
```

Successful answers, redirects, and errors should be visible only to the member
who invoked the command. Generated user, role, and everyone mentions must not
notify anyone.

## Local web dashboard

The local FastAPI dashboard provides shared-database status, bank overview,
historical-import status, database-backed editing for an explicit allowlist of
safe runtime settings, AI framework status/usage, a unified Knowledge manager,
a VC XP role-pulse readiness summary, stats graphics management, the reusable
Message Studio (Embed/Message Editor), and aggregate analytics. It does not
edit `.env`, modify bank records, expose Discord or Gemini secrets, or provide
public hosting.

The responsive dashboard navigation uses capability-filtered groups: **Monitor**
(Overview and Analytics), **Community** (Features, Streaks, and Events), **Operations**
(Bot Operations and Reminders), **Content** (Visual Content Studio, Message
Studio, Knowledge, and AI), **Finance** (Bank), and **System** (Settings,
Dashboard Access, and Audit Log).
It uses the Bro Eden pride icon in the desktop sidebar and mobile header. On
smaller screens the complete navigation moves into a labeled menu instead of
hiding destinations in a horizontal strip. `AI_DASHBOARD_VISIBLE=true` shows
the AI item; the item is hidden when that flag is false. The AI tab shows
framework health, usage,
recent `/ask` feedback, and a connected-sources page that explains which
knowledge chunks are available to AI retrieval. Knowledge changes happen in the
top-level Knowledge tab. Stats Graphics lives under Analytics. Feature-specific
settings live with their Features cards; users, roles, mappings, and overrides
live together in Dashboard Access. Older `/stats`, `/settings/knowledge`,
`/imports`, `/users`, `/settings/features`, and `/settings/permissions` URLs
redirect to their current locations so existing bookmarks remain usable.

### Message Studio (Embed/Message Editor)

The top-level **Message Studio** stores reusable Discord embed and message
assets in the shared `data.db`. **Create** offers an **Embed** or **Message**
type, and the table can search and sort by name, type, modification date, or
the bot features currently using each asset. Existing saved rows migrate to
`Embed`. Message assets provide trigger-ready content and optional buttons
without an embed card. Embed assets can contain up to 10 ordered embed cards in
one Discord message; each card provides author/header, title and URL,
description, color, thumbnail, large image, footer, and up to 25 fields. The
combined text across the cards follows Discord's 6,000-character message limit.
Existing single-card assets migrate automatically and keep their original
content. The
editor includes a visible searchable picker for Unicode emoji and custom emoji
from the latest live-server metadata snapshot. Server results retain the real
emoji name and insert Discord's exact `<:name:id>` static or `<a:name:id>`
animated markup into regular messages, author/title/description/footer text,
fields, and buttons. A bare numeric ID is accepted only when the current
snapshot can resolve its real name and animation state; complete copied custom
emoji markup remains accepted. The live Discord preview
renders headings, bold, italics, underline, strikethrough, spoilers, quotes,
lists, inline/fenced code, safe links, custom emoji, and raw Discord mentions
instead of showing the raw Markdown.

All text surfaces support reusable feature placeholders. `{user.feature}`
mentions the member who triggered the current feature, while `{role.feature}`
expands to every role that feature designates. Features can provide additional
values such as `{points}`, `{reward_status}`, and `{days}`. Unknown placeholders
remain intact so new feature integrations can add their own values later.

Each saved message can include up to five buttons. Role buttons may add or
remove one selected Discord role and use Discord's blue, gray, green, or red
button styles; URL buttons use Discord's fixed link style. At send time the bot
still checks **Manage Roles**, role hierarchy, and managed-role restrictions,
then confirms role changes privately. Bump reminders send the selected asset
as configured and no longer add an automatic subscription button. Successful
bump responses send the selected asset's content/embed and first four buttons,
then add the built-in **Bump Leaderboard** button. Assets used by a feature
cannot be deleted until a different asset (or the built-in fallback) is selected
on that feature's page under **Features**.

Set these values in the project-root `.env` and replace the placeholder
password and signing key before using the dashboard. When
`DASHBOARD_ENABLED=true`, startup fails clearly if either
`DASHBOARD_PASSWORD` or `DASHBOARD_SECRET_KEY` is blank or missing.

```dotenv
DASHBOARD_ENABLED=true
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=3000
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=change_this_password
DASHBOARD_SECRET_KEY=change_this_to_a_long_random_string
DASHBOARD_COOKIE_SECURE=true
DASHBOARD_AUTH_MODE=discord
GUILD_ID=
DISCORD_OAUTH_CLIENT_ID=
DISCORD_OAUTH_CLIENT_SECRET=
DISCORD_OAUTH_REDIRECT_URI=https://dashboard.broeden.com/auth/discord/callback
DASHBOARD_DISCORD_ALLOWED_USER_IDS=
DASHBOARD_DISCORD_ALLOWED_ROLE_IDS=
DASHBOARD_DISCORD_DEFAULT_ROLE=admin
DASHBOARD_DISCORD_REVERIFY_MINUTES=60
```

Generate a signing key with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
Run the dashboard separately from the Discord bot:

```bash
python3 -m dashboard.app
```

Or start it directly with Uvicorn:

```bash
uvicorn dashboard.app:app --host 0.0.0.0 --port 3000
```

Open `http://<pi-ip>:3000` or, when local hostname resolution is available,
`http://broedenbot.local:3000`. The deployed
`https://dashboard.broeden.com` endpoint may remain behind the existing
Cloudflare Tunnel/Access outer gate; the dashboard does not create or configure
that tunnel.

### Discord dashboard login

The login page supports Discord OAuth with the `identify guilds.members.read`
scopes while keeping the existing owner username/password as an emergency
fallback. Discord login is shown only when `DASHBOARD_AUTH_MODE=discord` and
`GUILD_ID`, the client ID, client secret, and redirect URI are configured. OAuth
state is stored in the signed session and consumed once during callback
validation. Access tokens exist only in memory long enough to fetch the Discord
identity and current configured-guild member; they are not logged, rendered, or
stored in SQLite.

Configure the Discord application:

1. Open the Discord Developer Portal and select the BroEdenBot application.
2. Open **OAuth2**.
3. Add the exact redirect URI
   `https://dashboard.broeden.com/auth/discord/callback`.
4. Copy the OAuth2 Client ID and Client Secret into `.env`.
5. Configure `GUILD_ID` and initially list the owner/test Discord user in
   `DASHBOARD_DISCORD_ALLOWED_USER_IDS`.
6. After owner login, map live Discord roles to dashboard roles in **Dashboard
   Access** and test with a low-risk Viewer mapping first.

The dashboard creates `dashboard_users` in the shared database. On a fresh
installation, the existing `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` are
seeded as a password-authenticated `owner`; only a salted PBKDF2 password hash
is stored. Discord users are admitted only after the OAuth member endpoint
verifies current membership and either a direct allowlist, compatibility
allowed role, existing legacy link, or database-backed Discord role mapping
grants access. Discord-derived assignments are replaced at login; losing the
qualifying role denies the next login. Verified Discord sessions expire after
`DASHBOARD_DISCORD_REVERIFY_MINUTES`, forcing a fresh membership/role check.
OAuth never auto-creates an Owner.

Capability-based permissions are reloaded from SQLite on every request and are
enforced by server middleware as well as permission-filtered navigation. The
seeded roles are Owner, Administrator, Moderator, Party Captain, and Analyst /
Viewer; owners can add custom roles, mappings, direct assignments, and per-user
allow/deny overrides. The final active Owner cannot be removed or disabled.
Significant actions are written to an append-only, secret-redacted audit log.

Keep Cloudflare Access enabled as the outer gate, keep password login until
Discord login is confirmed working, and never share
`DISCORD_OAUTH_CLIENT_SECRET`.

### Runtime settings and feature configuration

Global Settings contains General, Dashboard Access, Discord Connection, Data &
Storage, Advanced, and Audit Log. Feature-specific controls are discovered and
edited through focused **Features** pages rather than one continuous form.
Updates are validated as a group and stored as text in the shared `data.db`
`bot_settings` table, and recorded in `bot_settings_audit` when the value
changes. Existing database values are never overwritten during environment
seeding. The bot reads these safe values from SQLite first and falls back to
`.env` only when a database row is missing.
After a successful database read, the bot keeps an in-process copy of each
setting so a temporary SQLite read error does not drop runtime behavior back to
older `.env` values.

Editable settings include `/ask` channels and cooldown, staff/owner permission
IDs, voice/channel exclusions, bank access, VC XP role-pulse controls, and the
transferred bump, reminder, streak/recovery, stats, and leaderboard settings.
Each control shows a plain-language label, current source (Database,
Environment, Default, or Not configured), impact-oriented help, and a collapsed
Technical details section containing the raw key. Feature forms use one dirty-
state Save/Discard bar. Role and channel settings use compact searchable live
Discord selectors; user ID allowlists remain plain snowflake fields. The
dashboard Overview page also shows a
read-only VC XP readiness card using the shared database and latest Discord
metadata snapshot when available. Discord ID lists accept blank values or
comma-separated 17-20 digit IDs and remove spaces when saved. Integer and
boolean values are range-checked. Changes take effect through runtime setting
lookups; the dashboard does not automatically restart the bot.

Dashboard-managed JSON settings for Discord object selectors are also stored
in `bot_settings`:

- `analytics_excluded_channel_ids`
- `analytics_excluded_category_ids`
- `knowledge_allowed_channel_ids`
- `knowledge_allowed_category_ids`

Older dashboard-only role, ask, and bank JSON settings remain accepted by the
settings validator for compatibility, but the normal dashboard UI does not show
them. `BANK_LOG_CHANNEL_ID` is also hidden because the current bank cog does not
read it. Advanced settings are limited to miscellaneous local
operator defaults such as:

- `import_archive_path`
- `import_context_only_default`

Role, channel, category, and custom-emoji pickers use local metadata endpoints only:
`/api/discord/roles`, `/api/discord/channels`,
`/api/discord/categories`, `/api/discord/emojis`, and
`/api/discord/guild-structure`. These endpoints
require dashboard login and return the latest live-guild snapshot written by
the running Discord bot: role color, position, managed/mentionable/hoist flags,
member count when available, channel type, parent category, NSFW/thread flags,
custom emoji name/ID/animated/availability flags, and Discord sort order.
Historical import channels are not used as selector
options. Missing or deleted saved objects are displayed separately as missing
saved items so operators can remove stale values deliberately; the dashboard
does not silently delete saved IDs.

Settings → Discord Connection shows compact roles/categories/channels/emojis
counts, a friendly last-refresh time, and the latest refresh error if one exists.
Feature-owned selectors stay on their feature pages.
The Refresh Discord Metadata button queues the fixed
`refresh_discord_metadata` dashboard action. The live bot process handles that
action from its existing dashboard action worker, explicitly fetches the current
custom emojis from Discord (with the enabled guild-expression gateway cache as a
fallback), and snapshots guild roles/channels/categories/emojis into SQLite.
The FastAPI dashboard still does not start a second Discord client and does not
expose arbitrary API calls.
Knowledge reindex actions now also queue this fixed Discord metadata refresh,
so the custom-emoji browser is repopulated after a reindex.

Channel settings and category settings are stored separately. A channel is
treated as selected when either its own channel ID is selected or its parent
category ID is selected, which lets future channels added under a selected
category inherit that setting automatically. Parallel category settings include
`analytics_excluded_category_ids`, `knowledge_allowed_category_ids`, and
`ask_command_allowed_category_ids`.

`GUILD_ID`, `MODAI_MODEL`, `MODAI_FALLBACK_MODEL`, `ASK_MODEL`, and
`ASK_FALLBACK_MODEL` remain read-only displays. `DISCORD_TOKEN`,
`GEMINI_API_KEY`, `DASHBOARD_PASSWORD`, `DASHBOARD_SECRET_KEY`, and every key
containing `TOKEN`, `API_KEY`, `PASSWORD`, or `SECRET` are forbidden from the
settings database and edit route.

### Phase 2 bot operations

The authenticated Operations page adds local visibility and three fixed
actions:

- Status and the latest 100 journal lines for `broedenbot` and
  `broeden-dashboard`.
- Hostname, uptime, disk and memory usage, Python version, and current Git
  commit/branch when available.
- Shared and bank SQLite paths, sizes, and rough counts for known settings,
  audit, import, bank, and donation tables.
- Fixed restart buttons for the two services.
- A consistent SQLite backup of the active shared database into `backups/`
  using a timestamped `broeden-backup-YYYYMMDD-HHMMSS.sqlite` filename.

The page has no terminal, arbitrary unit input, deploy/pull action, environment
editor, or backup download route. Logs are redacted for obvious token,
password, API-key, secret, authorization, bearer-token, and Discord-token
patterns. Read-only `systemctl status` and `journalctl` commands run without
`sudo` where the host permits it. Missing systemd, journal, or Git metadata is
shown as unavailable instead of crashing the page.

The restart actions execute only:

```text
sudo systemctl restart broedenbot
sudo systemctl restart broeden-dashboard
```

To permit those commands without giving the dashboard general root access,
first find the executable path on the Pi:

```bash
command -v systemctl
command -v journalctl
```

Then create a dedicated sudoers file:

```bash
sudo visudo -f /etc/sudoers.d/broeden-dashboard
```

If `command -v systemctl` returns `/bin/systemctl`, add:

```sudoers
sadcatdad ALL=NOPASSWD: /bin/systemctl restart broedenbot, /bin/systemctl restart broeden-dashboard
```

Use `/usr/bin/systemctl` instead when that is the path reported by the Pi.
Status and log reads normally need no sudoers entries. If the local system
policy requires privileged reads, allow only these exact additional commands,
using the paths returned by `command -v`:

```sudoers
sadcatdad ALL=NOPASSWD: /bin/systemctl status broedenbot --no-pager, /bin/systemctl status broeden-dashboard --no-pager, /bin/journalctl -u broedenbot -n 100 --no-pager, /bin/journalctl -u broeden-dashboard -n 100 --no-pager
```

The current implementation intentionally does not invoke `sudo` for status or
log reads; these optional entries are only a reference if local policy is
tightened and the fixed read commands are updated accordingly.

### Stats Graphics Manager

The authenticated Analytics → Stats Graphics page manages the bot's existing
tracked role rosters, role-comparison and missing-role graphics, and tracked
activity reports. It reuses `role_stat_embeds`, `tracked_stats_reports`, and
`tracked_activity_reports`; it does not create a parallel stats system.

Tracked items use dashboard IDs such as `roster-12`, `report-4`, and
`activity-8`. The list and detail pages show their safe configuration, Discord
channel/message IDs, update time, archive/error state, and locally stored
member-snapshot count. Role rosters and role reports allow edits to title and
description. Banner images remain managed through the existing Discord
attachment workflow because the roster renderer uses persisted image bytes,
not an editable URL. Guild, role, channel, and message IDs are read-only.

The dashboard and Discord bot run as separate processes. Clicking Refresh
therefore inserts a fixed `refresh_stat` entry into `dashboard_actions`; it
does not start another Discord client. The live Stats cog checks this queue,
uses its existing refresh helpers, records success or failure, and captures a
local member snapshot for supported role rosters and role reports. No shell
command or user-supplied SQL is involved.

Archive is intentionally non-destructive. It marks the tracked record
`archived`, excludes it from automatic and queued refreshes, keeps it visible
in the dashboard, and leaves the existing Discord message alone. Existing
Discord `/stats delete` and `/stats reset` behavior remains separate.

CSV export uses the latest locally stored `dashboard_stat_members` snapshot and
includes Discord user ID, username/display name, role ID, join time when
available, snapshot category, capture time, and export time. If no snapshot is
available, the dashboard shows a friendly message and suggests queueing a
refresh. Member previews are limited to the first 100 rows on the detail page.

The Stats Graphics Manager requires dashboard login.
Every edit, refresh, and archive POST requires the existing session CSRF token.
It adds no bank manager, checklist manager, terminal, or arbitrary command/SQL
surface.

### Visual Content Studio

The authenticated **Content → Visual Content Studio** workspace manages the
appearance of every runtime-generated Bro Eden PNG without changing Discord
commands, ranking calculations, query logic, or existing permission checks. Its
registry currently covers activity, voice, custom, bump, and streak
leaderboards; role rosters; role-comparison, missing-role, and stats-error
cards; and the queue up-next banner.

The workspace includes registered templates, a reusable Asset Library, themes,
global defaults, draft/publish previews, the last 20 published versions,
restore-to-draft, optional variants, seasonal schedules, audit history, and
versioned JSON import/export. Authenticated viewers can inspect the workspace;
only dashboard owners and admins can upload, edit, publish, archive, restore,
delete, import, or schedule changes. Every mutation uses the existing CSRF
protection.

Each upload screen shows the exact recommended, minimum, maximum, aspect-ratio,
transparency, file-size, fit, focal-point, and safe-area guidance for its
destination before the file is saved. Still PNG, JPG, and WEBP uploads are
content-validated, normalized once, stripped of metadata, and stored under
generated keys in `VISUAL_ASSET_DIR`. The live bot then posts that normalized
copy to the configured private Discord forum post, records the attachment URL,
and the dashboard switches to that link. The local normalized copy remains a
fast renderer cache and can be rebuilt from the allowlisted Discord CDN source;
SQLite contains metadata, message references, storage jobs, and dependency
records rather than image BLOBs. Wrong-ratio crops and undersized uploads
require explicit acknowledgement. Referenced assets cannot be archived or
deleted. Permanently deleting an archived, unused asset queues deletion of its
matching Discord message.

Configure **Asset Library Storage Forum Post** under **Features → Visual
Content Studio** and add `visual` to `ENABLED_MODULES`. On startup, the live bot
queues every active legacy asset without a Discord source. Changing the
destination queues those assets into the new post and removes each prior
storage message only after its replacement is recorded.

Live resolution follows built-in defaults → published global defaults → theme
→ published template overrides → variant → active schedule. Broken or missing
custom data falls back to the legacy renderer and bundled/BLOB assets instead
of breaking the Discord command. Existing per-image Discord byte-limit
enforcement remains authoritative.

Member-list graphics use Discord account usernames rather than server
nicknames or global display names. This keeps decorated nickname text, symbols,
and emoji from turning into missing-glyph boxes in Pillow-rendered images.
Unsupported glyphs in historical labels are normalized or removed before
drawing as an additional fallback.

See [the complete admin, developer, migration, backup, rollback, and
troubleshooting guide](docs/visual-content-studio.md), the
[registry-generated upload-size table](docs/visual-content-studio-size-reference.md),
and the [source audit and PNG inventory](docs/visual-content-studio-implementation-map.md).

### Unified Knowledge Manager

The authenticated Knowledge tab is the single dashboard surface for public and
staff knowledge. It shows internal file-backed dashboard docs,
dashboard-created text sources in `ai_kb_sources` / `ai_kb_chunks`, and live
Discord sources in `knowledge_sources` / `knowledge_entries`.

File-backed documents are limited to internal dashboard docs and still use a
fixed source-code allowlist. The dashboard does not accept arbitrary file paths,
browse directories, follow symlinks outside the project, or expose a
file-download route. The old public Rules, public Survival Guide, and private
Ranger's Handbook Markdown files are no longer dashboard knowledge sources;
those topics are expected to come from configured Discord channel/forum/thread
sources instead. The remaining file allowlist covers internal guides under
`docs/`: message context, staff context, checklists, historical imports, VC log
imports, and the codebase map.

Each file entry shows its category, public/staff/internal visibility, relative
path, editability, modified time, size, approximate word count, and
found/missing/empty state. Search and filters run in the browser against
already allowlisted metadata. Document previews are escaped plain text, so raw
HTML and scripts are not executed. Obvious token, password, API-key, secret,
authorization, and provider-token patterns are redacted from displayed content
and rejected on save.

The message-context and staff-context guides are editable. Import guides,
checklist docs, and the codebase map are intentionally read-only in this
dashboard phase. Edits are limited to UTF-8 Markdown/text, reject binary or
content over 1 MB, and use a temporary file plus atomic replacement. Before
replacing an existing file, the dashboard creates a timestamped copy under
`backups/knowledge/`; those runtime backups are ignored by Git.
`knowledge_audit` records metadata for edits and reindex requests without
storing old or new document contents.

Dashboard text sources support public/staff visibility, source type, content
editing, deletion, and a `Use this source for AI retrieval` checkbox. Disabled
AI sources remain stored but are ignored by `search_kb()` and therefore by
member-facing `/ask` retrieval. Discord `/knowledge sources list` and sync
results show whether each live source is connected to AI retrieval, so an
indexed source that is intentionally AI-disabled is visible to admins.

Live Discord sources can be configured from the same Knowledge tab using either
the channel picker or a pasted channel/forum-thread ID. This supports whole text
channels, whole forum channels, and individual forum posts/threads. The
dashboard can save source type, public vs staff-only visibility, live/manual
sync mode, enabled state, and whether the source is mirrored into AI retrieval.
Sync buttons enqueue a fixed `sync_knowledge_source` action in
`dashboard_actions`; the live Discord bot performs the actual history fetch and
marks the action completed or failed. The dashboard never starts a second
Discord client.

Knowledge loaders are cached inside the Discord bot process. File reindex
buttons enqueue the fixed `reindex_knowledge` action plus the fixed
`refresh_discord_metadata` action so custom server emoji metadata is refreshed
at the same time. The live bot action worker validates fixed payloads,
clears/reloads the relevant caches or syncs the requested Discord source, then
records each action result.

The Knowledge manager requires the existing signed login session. Every edit,
toggle, remove, sync, and reindex POST requires CSRF protection. It is not a
bank manager, checklist manager, terminal, arbitrary SQL surface, or public
knowledge portal.

### Server Analytics Dashboard

The authenticated Analytics section is a read-only view of existing aggregate
server activity. It uses `stats_message_activity` for text analytics and the
existing `vc_sessions` and `vc_imported_sessions` tables for voice analytics
when those tables contain data. It does not read or display message bodies from
`message_context.db` or `staff_context.db`, and it does not expose deleted or
NSFW message snippets.

The Overview page shows all-time messages, distinct stored user and channel
IDs, covered dates, 7-day and 30-day totals, selected-range volume, top
channels and stored member identities, trend comparison, peak heatmap cell,
voice summary, and data freshness. Separate pages provide:

- Daily, weekly, and monthly message totals.
- Channel counts, unique posters, first/last activity, configured category,
  and percent of tracked activity.
- Stored member message totals, active days, first/last activity, and top
  channel. Bots are excluded by the existing live tracker and historical
  importer. The dashboard cannot verify current guild membership because it
  intentionally has no Discord client, so departed users may remain in stored
  aggregate history.
- Live and imported completed VC-session totals, top users/channels, daily and
  weekly summaries, and recent session metadata.
- A server-rendered UTC day-of-week/hour heatmap with no JavaScript chart
  dependency.

The Analytics sidebar contains Overview, Activity Analytics, Stats Graphics,
VC Analytics, and Exports. Channel, member, and heatmap drilldowns remain
available from Activity Analytics.

Allowed text ranges are 7 days, 30 days, 90 days, 1 year, and all time. The
heatmap supports 30 days, 90 days, 1 year, and all time. Leaderboards accept
only 10, 25, 50, or 100 rows. These values are fixed allowlists; routes do not
accept SQL fragments, table names, or arbitrary date clauses.

CSV exports are available for overview, activity, channels, members, voice,
and heatmap views. Exports contain aggregated counts and stored IDs/names only,
never message content or secrets. Missing tables and empty datasets render
friendly empty states instead of creating replacement schemas.

Analytics remain behind the signed dashboard login and existing Cloudflare
outer gate. Phase 3C adds no POST actions, imports, uploads, cache rebuilds, AI
testing, or shell commands. Historical imports continue to use the existing
terminal-only workflows.

An optional separate systemd unit template is provided at
`broeden-dashboard.service.example`. It uses the current
`sadcatdad` account and `/home/sadcatdad/BroEdenBot` project path. Copy it to
`/etc/systemd/system/broeden-dashboard.service`, then enable it independently
from the existing bot service.

To create a shareable source archive from the directory containing the project,
use this command. It excludes local credentials, virtual environments, Git
metadata, runtime databases and logs, existing archives, and bundled font
files:

```bash
zip -r BroEdenBot-safe.zip BroEdenBot \
  -x '*/.env' '*/.env.*' \
     '*/.venv/*' '*/.git/*' \
     '*.db' '*.db-*' \
     '*.sqlite' '*.sqlite-*' \
     '*.sqlite3' '*.sqlite3-*' \
     '*.log' '*.zip' \
     '*/assets/*.ttf'
```

## Deploy on the Raspberry Pi

Commit and push the changes, then:

```bash
cd ~/BroEdenBot
./deploy.sh
```

The deployment script should restart the bot. If it does not, restart the bot
service or process manually so updated cogs and slash-command definitions are
loaded and synchronized.

The example bot and dashboard systemd units run a read-only SQLite quick check
before every start. A missing virtual environment, missing runtime database, or
failed integrity check prevents the service from starting, and systemd limits
repeated restart attempts. Keep runtime databases on a filesystem that has been
verified healthy; do not point either service at a mount reporting USB resets,
I/O errors, an aborted journal, or a read-only remount.

## Deploy on Railway

Railway runs the Discord bot and FastAPI dashboard together as one service so
they can safely share a single persistent SQLite volume. The repository
includes `Dockerfile`, `railway.toml`, and `scripts/railway_start.sh` for this
layout. Mount a Railway volume at `/data`, upload the four existing databases,
and configure these absolute paths:

```env
DATABASE_PATH=/data/data.db
MESSAGE_CONTEXT_DB_PATH=/data/message_context.db
STAFF_CONTEXT_DB_PATH=/data/staff_context.db
BANK_DATABASE_PATH=/data/brobank.db
VISUAL_ASSET_DIR=/data/visual-assets
VISUAL_ASSET_STORAGE_THREAD_ID=your-private-forum-post-thread-id
DASHBOARD_HOST=0.0.0.0
DASHBOARD_COOKIE_SECURE=true
```

Railway supplies `PORT`; the startup script binds the dashboard to that port,
runs read-only SQLite quick checks, applies and validates the additive reminder
and Visual Content Studio migrations, then supervises both long-running
processes. A missing or malformed database prevents the deployment from
starting. Configure `/health` as the service health check and keep the replica
count at one because SQLite volumes cannot be shared safely across replicas.
For a brand-new empty volume, temporarily set `BROEDEN_SEED_MODE=true`; this
serves only `/health` so Railway can mount the volume for direct file upload.
Set it back to `false` before the production deployment.

Store `.env` values in Railway service variables, never in Git or the Docker
image. The live bot only requires the four databases and the Visual Content
Studio asset directory. Raw import exports are not runtime dependencies after
their rows and import ledgers have been verified in SQLite; archive only the
source files needed for a future re-import and remove redundant copies instead
of placing them on the Railway volume. Enable scheduled Railway volume backups
after the first verified deployment.

After the owner IDs and Pi permissions are configured, `/bot deploy` provides
the confirmation-gated Discord shortcut. Historical imports are never launched
from Discord; continue using `bedimportdry` and `bedimport` in the terminal.
