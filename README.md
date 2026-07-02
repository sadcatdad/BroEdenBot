# BroEdenBot

BroEdenBot is a Discord community bot for Bro Eden. It provides member-facing
server guidance, moderation guidance, staff notes, voice-channel activity
tracking, live statistics, queues, polls, leaderboards, and bank tracking.

The bot loads every Python cog in `cogs/` and synchronizes its application
commands when it starts.

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
| `/guide search` | All server members |
| `/bot help`, `/bot status`, `/bot logs`, `/bot restart`, `/bot deploy` | Users listed in `BOT_OWNER_USER_IDS`; administrators only when `BOT_OWNER_ALLOW_ADMINS=true` |
| `/ai test`, `/ai status`, `/ai kb` commands | Users listed in `BOT_OWNER_USER_IDS`; administrators only when `BOT_OWNER_ALLOW_ADMINS=true` |
| `/staffai help`, `/staffai status`, `/staffai ask`, `/staffai search`, `/staffai summarize` | Roles listed in `STAFF_AI_ALLOWED_ROLE_IDS` or users listed in `BOT_OWNER_USER_IDS` |
| `/context help`, `/context status`, `/context search`, `/context summarize`, `/context timeline`, `/context user`, `/context channel` | Roles listed in `MESSAGE_CONTEXT_ALLOWED_ROLE_IDS` or users listed in `BOT_OWNER_USER_IDS` |
| `/rulecard draft` and **Draft Rule Reminder** message action | Administrators or roles listed in `MODAI_ALLOWED_ROLE_IDS` |
| `/checklist` commands and management controls | Roles listed in `CHECKLIST_ALLOWED_ROLE_IDS` or users listed in `BOT_OWNER_USER_IDS` |
| `/reminder add`, `/reminder manage` | Internal staff only: administrators, `BOT_OWNER_USER_IDS`, `REMINDER_ALLOWED_ROLE_IDS`, or configured staff/admin roles |
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
| Stats creation and refresh commands | Administrators or roles listed in `STATS_ALLOWED_ROLE_IDS` |
| `/stats delete` and `/stats reset` | Administrators only |
| `/poll`, `/queue dashboard`, and member queue actions | All server members |
| Queue lock/unlock/move/remove/pull controls | Administrators or members with **Manage Channels** |
| `/leaderboards` | All server members |
| Leaderboard create/delete/add/remove commands | Administrators or members with **Manage Server** |

For staff-restricted features, an empty role-ID environment variable makes the
feature effectively administrator-only. This does not apply to `/ask` or the
`/bot` or `/checklist` groups. Bot management and checklist management are
owner-only by default when their broader access settings are blank.

## Internal reminder commands

Reminders live in `data.db` and are checked by a background task every 45
seconds. Pending reminders survive bot restarts because only the database row is
the source of truth. The command group is for internal staff use only. All setup
and management responses are private; the fired reminder posts in the selected
channel, mentions the target member outside the embed so Discord pings them, and
marks the row as `sent`. If the target channel is missing or the bot cannot send
there, the reminder is marked `failed` with a short reason instead of retrying
forever.

- `/reminder add [who] <message> <date_time> <channel>` — Schedule a reminder.
  `who` defaults to the caller. Use local community time; supported formats
  include `2026-07-01`, `2026-07-01 7:30 PM`, and
  `07/01/2026 7:30 PM`. Date-only reminders default to 9:00 AM local time.
- `/reminder manage` — Privately list pending reminders created by you or aimed
  at you. The panel supports selecting a reminder, editing message/time,
  editing the channel, editing the target when permitted, and deleting the
  reminder before it fires.

Using reminders requires administrator permission, a user ID in
`BOT_OWNER_USER_IDS`, a role in `REMINDER_ALLOWED_ROLE_IDS`, or one of the
dashboard-managed staff/admin roles. Regular members cannot create or manage
reminders, including self-reminders. The reminder timezone defaults to
`America/Chicago`; set `REMINDER_TIMEZONE` to another IANA timezone if the
community standard changes. Reminder sends require the bot to have **View
Channel**, **Send Messages** or **Send Messages in Threads**, and **Embed
Links** in the selected channel.

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
- `/guide search <query>` — Searches only the public Survival Guide and Rules
  without Gemini.
- `/ask <question>` — Answers from public guidance with private follow-up
  buttons.
- `/staffai help`, `/staffai status`, `/staffai ask`, `/staffai search`,
  `/staffai summarize` — Private imported and live staff-channel context tools.
- `/context help`, `/context status`, `/context search`, `/context summarize`,
  `/context timeline`, `/context user`, `/context channel` — Private
  full-server message archive, search, and timeline tools.
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
- Successful answers include `Open Ticket`, `Search Guide`, `This Helped`, and
  `Still Confused` buttons. Only the member who ran `/ask` can use them.
  `This Helped` and `Still Confused` acknowledge the member privately and then
  disable the feedback buttons on that response. The selection is stored
  locally for dashboard review; it does not publish feedback, alert staff, or
  retrain Gemini.

`/ask` uses Gemini with only `public` sources in the AI Knowledge Base
(`ai_kb_sources` and `ai_kb_chunks`). Import public rules, guides, FAQs, role
guides, or channel guides through `/ai kb import` or the dashboard AI Knowledge
Base editor.

It does not read `staff_notes.py`, staff-note records, ModAI prompts, moderation
logs, private incident records, imported Discord history, message history,
member activity stats, or other private data. Successful member-facing answers
create a local `ask_feedback` row with the member ID, question, answer, matched
public KB chunks, model metadata, and later `helped`/`confused` button choice.
This review data is used to improve the public Knowledge Base manually.

The prompt requires Gemini to stay grounded in the retrieved public KB chunks,
avoid inventing policies or permissions, and keep replies concise. When no
public KB source matches, `/ask` says it could not find an answer and directs
the member to submit a ticket in <#1300632962127368283>.

Questions involving personal disputes, accusations, harassment, reports,
appeals, staff complaints, moderation actions, rule-enforcement decisions,
private information, attempts to bypass rules, or crisis/legal/medical matters
are not sent to Gemini. The member is privately directed to the support-ticket
channel instead. Clearly unrelated questions are also redirected rather than
answered.

The command uses `AI_MEMBER_COOLDOWN_SECONDS`. If Gemini returns an empty or
blocked response, the private embed says it cannot answer safely. If Gemini
fails, the private embed directs the member to the support-ticket
channel. Missing `GEMINI_API_KEY` configuration is reported ephemerally.

### `/guide search <query>`

Performs a deterministic, case-insensitive search of the public Survival Guide
and Rules, returning up to three short matching snippets.

- `query` — Required keywords or topic, from 2 to 200 characters.
- Responses are ephemeral and available to all server members.
- A 15-second per-user cooldown limits repeated searches.
- It does not use Gemini or read staff/private data, imported chat history, or
  activity statistics.

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
match exists. Staff-only KB chunks are available only to staff tools such as
context summaries and rulecard drafting.

The framework accepts task types such as `ask_server_guide`,
`staff_context_user`, `staff_context_channel`, `staff_context_topic`,
`rulecard_draft`, `ticket_summary`, `moderation_classification`,
`weekly_recap`, and `onboarding_helper`.

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
  — Neutral member participation review. Timeframes include `24h`, `3d`,
  `7d`, `14d`, `30d`, `60d`, and `90d`.
- `/context channel <channel> <timeframe> [topic] [include_bots] [max_messages]`
  — Channel recap. Timeframes include `1h`, `6h`, `12h`, `24h`, `3d`, `7d`,
  `14d`, and `30d`.

Access requires `MESSAGE_CONTEXT_ALLOWED_ROLE_IDS` or `BOT_OWNER_USER_IDS`.
All replies are ephemeral. Full privacy, Message Content Intent, retention,
configuration, import, and test instructions are in
[docs/message-context.md](docs/message-context.md).

## ModAI commands

All ModAI responses are private unless an authorized staff member deliberately
uses the **Send Rule Reminder** button. ModAI provides guidance only. It does
not warn, timeout, kick, ban, delete messages, or otherwise moderate members
automatically.

Private ModAI knowledge includes the public Rules and Survival Guide plus the
staff-only `data/staff_knowledge/rangers_handbook.md`. The Ranger's Handbook is
not loaded by public `/ask`.

### `/modai check <text>`

Privately reviews pasted text for possible moderation concerns using Gemini and
the local Bro Eden rules and survival guide.

- `text` — The text staff want reviewed. This is treated as untrusted content
  and is not stored by this command.

The response can include possible concern categories, relevant rules, context
considerations, suggested staff action, a draft response, handling route, and
whether more context is needed.

### `/modai rulesearch <query>`

Performs a local keyword search of the Bro Eden rules, survival guide, and
staff-only Ranger's Handbook. This does not call Gemini.

- `query` — A word or short topic to find, such as `unsolicited DMs`,
  `self-promo`, or `politics`.

### `/modai rulehelp <situation>`

Privately evaluates a described situation against the local rules and survival
guide.

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
statuses/errors. It never grants or removes roles, calls MEE6, starts a payout,
or changes VCXP accounting.

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
snapshot, and recent role-add activity. The Discord audit remains the source of
truth for live role hierarchy and Manage Roles checks.

## Stats commands

Tracked stats pages update when relevant membership changes occur. Role
rosters and reports can contain an **Export Members to CSV** button; authorized
stats users receive that export privately.

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
EXCLUDED_VOICE_CHANNEL_IDS=
VC_EXCLUDED_USER_IDS=983091180885643326,716390085896962058
```

Live message activity ignores bot-authored messages, `ACTIVITY_EXCLUDED_USER_IDS`,
and members with any `ACTIVITY_EXCLUDED_ROLE_IDS`. Live VC tracking ignores bots,
`VC_EXCLUDED_USER_IDS`, and members with any `VC_EXCLUDED_ROLE_IDS`; excluded
members do not receive VC rewards or automatic VC XP pulses. Use
`VCXP_EXCLUDED_ROLE_IDS` when a role should be visible in VC stats but should
not earn VC XP pulses. The Voice dashboard also ignores `VC_EXCLUDED_USER_IDS`,
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

Viewing leaderboards is public. Creating, deleting, and changing scores
requires **Manage Server** or administrator permission.

### `/leaderboard create <name>`

Creates a named leaderboard. Existing leaderboards are preserved.

- `name` — Name used to identify the leaderboard, up to 50 characters.

### `/leaderboard delete <name>`

Deletes a leaderboard and all point records attached to it.

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

### `/leaderboards <name>`

Displays a leaderboard as a wide paginated graphic, ten members per page. The
graphic uses the same dark visual system as stats leaderboards, including
avatars, medal ranks, progress rails, point pills, and a live timestamp.
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

Secrets and boot-only values remain environment-backed. Phase 1.5 safe runtime
settings are seeded from `.env` into `data.db` only when a database value does
not already exist. After seeding, the database value takes priority and can be
updated from the authenticated local dashboard without rewriting `.env`.

| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Discord bot token. Required. |
| `BOT_OWNER_USER_IDS` | Comma-separated Discord user IDs allowed to use `/bot` commands. |
| `BOT_OWNER_ALLOW_ADMINS` | Allows server administrators to use `/bot` when `true`. Defaults to `false`. |
| `CHECKLIST_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to use `/checklist`; bot owners are also allowed. Blank makes checklist management owner-only. |
| `REMINDER_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to use internal staff reminders. Administrators, bot owners, and configured staff/admin roles are also allowed. |
| `REMINDER_TIMEZONE` | IANA timezone used for `/reminder add` and edit date/time input. Defaults to `America/Chicago`. |
| `GEMINI_API_KEY` | Gemini API key used by `/ask`, `/staffai`, `/context`, and ModAI. |
| `AI_ENABLED` | Enables shared AI framework calls when `true`. Missing `GEMINI_API_KEY` still disables framework calls. Defaults to `true`. |
| `AI_MODEL_FAST` | Fast/cheap framework model tier. Defaults to `gemini-2.5-flash-lite`. |
| `AI_MODEL_DEFAULT` | Default framework model tier. Defaults to `gemini-2.5-flash`. |
| `AI_MODEL_ADVANCED` | Advanced framework model tier. Defaults to `gemini-3-flash-preview`. |
| `AI_ENABLE_ADVANCED_MODEL` | Allows the advanced tier when `true`; otherwise advanced requests fall back to default. Defaults to `false`. |
| `AI_DAILY_BUDGET_USD` | Daily estimated AI framework budget. Defaults to `0.35`. |
| `AI_MONTHLY_BUDGET_USD` | Monthly estimated AI framework budget. Defaults to `10.00`. |
| `AI_MAX_INPUT_TOKENS` | Maximum estimated framework input tokens. Defaults to `12000`. |
| `AI_MAX_OUTPUT_TOKENS` | Maximum framework output tokens. Defaults to `1200`. |
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
| `VCXP_REWARD_START_AT` | ISO timestamp for the earliest completed VC session that can earn VC XP. Defaults to the first bot startup time after this setting exists, preventing historical back-pay pulses. |
| `VC_XP_PULSE_MINUTES` | Eligible VC minutes required per pulse across sessions. Defaults to `30`. |
| `VCXP_ENABLED` | Enables automatic and manual role pulses when `true`. Defaults to `false`. |
| `BANK_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to use bank commands. |
| `STATS_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to create and refresh stats pages. |
| `DASHBOARD_ENABLED` | Enables the local dashboard when `true`. |
| `DASHBOARD_HOST` | Dashboard bind address. Use `0.0.0.0` for access from the local network. |
| `DASHBOARD_PORT` | Dashboard port. Defaults to `3000`. |
| `DASHBOARD_USERNAME` | Local dashboard login username. |
| `DASHBOARD_PASSWORD` | Local dashboard login password. Use a unique password. |
| `DASHBOARD_SECRET_KEY` | Long random key used to sign dashboard sessions. |
| `DASHBOARD_AUTH_MODE` | Set to `discord` to enable Discord OAuth while retaining password fallback. |
| `DISCORD_OAUTH_CLIENT_ID` | Discord application OAuth2 client ID. |
| `DISCORD_OAUTH_CLIENT_SECRET` | Discord application OAuth2 client secret. Keep only in `.env`. |
| `DISCORD_OAUTH_REDIRECT_URI` | Exact OAuth callback URL, such as `https://dashboard.broeden.com/auth/discord/callback`. |
| `DASHBOARD_DISCORD_ALLOWED_USER_IDS` | Comma- or space-separated Discord user IDs approved for dashboard login. |
| `DASHBOARD_DISCORD_ALLOWED_ROLE_IDS` | Reserved for future guild-role approval; not used by this first implementation. |
| `DASHBOARD_DISCORD_DEFAULT_ROLE` | Role assigned to new approved Discord users: `admin` or `viewer`. |
| `DATABASE_PATH` | Optional shared SQLite path for the dashboard. Defaults to the existing `data.db`, then common local database names. |
| `BANK_DATABASE_PATH` | Optional bank SQLite path for the dashboard. Defaults to `brobank.db`. |

## Run locally

Python 3.11 or newer is recommended. Python 3.9 is end-of-life and current
Google libraries emit compatibility warnings on it.

```bash
cd ~/Documents/BroEdenBot
source .venv/bin/activate
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

The bot requests only guild, guild-message, voice-state, member, and message-
content gateway events. Enable **Server Members Intent** and **Message Content
Intent** in the Discord Developer Portal so VC/member tracking, legacy queue
commands, and the explicitly enabled private context archives work correctly.
Presence, typing, invite, integration, webhook, and other unrelated intents are
not requested.

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
safe runtime settings, AI framework status/usage, an AI Knowledge Base editor,
a VC XP role-pulse readiness summary, stats graphics management, and an
allowlisted local Knowledge Base manager. It does not edit `.env`, modify bank
records, expose Discord or Gemini secrets, or provide public hosting.

The top-level dashboard tabs are Overview, Operations, AI, Analytics, Bank, and
Settings when `AI_DASHBOARD_VISIBLE=true`; the AI tab is hidden when that flag
is false. The AI tab links to the AI Knowledge Base editor where dashboard
admins can create, edit, search, and delete `public` or `staff` KB sources in
`ai_kb_sources` and `ai_kb_chunks`. Paste/edit textareas support markdown or
plain text up to 2 MB per source; dashboard file upload is intentionally not
enabled in Phase 1. The AI tab also shows recent `/ask` feedback, prioritizing
`Still Confused` selections alongside the question, answer, and matched public
KB chunks. Stats Graphics lives under Analytics. Knowledge Base, Imports, and
Dashboard Users live under Settings. The older `/stats`, `/knowledge`,
`/imports`, and `/users` links redirect to their new locations so existing
bookmarks remain usable.

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
DASHBOARD_AUTH_MODE=discord
DISCORD_OAUTH_CLIENT_ID=
DISCORD_OAUTH_CLIENT_SECRET=
DISCORD_OAUTH_REDIRECT_URI=https://dashboard.broeden.com/auth/discord/callback
DASHBOARD_DISCORD_ALLOWED_USER_IDS=
DASHBOARD_DISCORD_ALLOWED_ROLE_IDS=
DASHBOARD_DISCORD_DEFAULT_ROLE=admin
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

The login page supports Discord OAuth with the `identify` scope while keeping
the existing owner username/password as a fallback. Discord login is shown only
when `DASHBOARD_AUTH_MODE=discord` and the client ID, client secret, and
redirect URI are configured. OAuth state is stored in the signed session and
consumed once during callback validation. Access tokens exist only in memory
long enough to fetch the Discord identity; they are not logged, rendered, or
stored in SQLite.

Configure the Discord application:

1. Open the Discord Developer Portal and select the BroEdenBot application.
2. Open **OAuth2**.
3. Add the exact redirect URI
   `https://dashboard.broeden.com/auth/discord/callback`.
4. Copy the OAuth2 Client ID and Client Secret into `.env`.
5. Enable Discord Developer Mode, right-click each approved user, choose
   **Copy User ID**, and add the IDs to
   `DASHBOARD_DISCORD_ALLOWED_USER_IDS`.

The dashboard creates `dashboard_users` in the shared database. On a fresh
installation, the existing `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` are
seeded as a password-authenticated `owner`; only a salted PBKDF2 password hash
is stored. Discord users are admitted only when already linked to an active row
or explicitly listed in `DASHBOARD_DISCORD_ALLOWED_USER_IDS`. Newly approved
Discord users receive `admin` or `viewer` according to
`DASHBOARD_DISCORD_DEFAULT_ROLE`; OAuth never auto-creates an `owner`. Disabled
linked users are denied.

Owners and admins retain existing dashboard write actions. Viewers can read
dashboard pages and exports, but the shared action guard rejects settings,
operations, stats, and knowledge POST actions. The minimal Users page lists
provider, Discord identity, role, status, and last login for owners/admins.
Role-based admission through `DASHBOARD_DISCORD_ALLOWED_ROLE_IDS` is deferred;
this version does not require bot-token guild-member lookups and never starts a
second Discord client.

Keep Cloudflare Access enabled as the outer gate, keep password login until
Discord login is confirmed working, and never share
`DISCORD_OAUTH_CLIENT_SECRET`.

### Phase 1.5 runtime settings

The Settings section has sidebar entries for Bot Configuration, Permissions &
Access, Discord Roles & Channels, Knowledge Base, Imports, Dashboard Users,
and Advanced. Updates are validated, stored as text in the shared `data.db`
`bot_settings` table, and recorded in `bot_settings_audit` when the value
changes. Existing database values are never overwritten during environment
seeding. The bot reads these safe values from SQLite first and falls back to
`.env` only when a database row is missing.
After a successful database read, the bot keeps an in-process copy of each
setting so a temporary SQLite read error does not drop runtime behavior back to
older `.env` values.

Editable settings include `/ask` channels and cooldown, staff/owner permission
IDs, voice/channel exclusions, bank access, and VC XP role-pulse controls.
Role and channel permission settings use the same Discord metadata selectors
as the Discord Roles & Channels page; user ID allowlists remain plain ID
fields. The dashboard Overview page also shows a
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
them because the live runtime settings are edited in Bot Configuration or
Permissions & Access. Advanced settings are limited to miscellaneous local
operator defaults such as:

- `import_archive_path`
- `import_context_only_default`

Role, channel, and category pickers use local metadata endpoints only:
`/api/discord/roles`, `/api/discord/channels`,
`/api/discord/categories`, and `/api/discord/guild-structure`. These endpoints
require dashboard login and return the latest live-guild snapshot written by
the running Discord bot: role color, position, managed/mentionable/hoist flags,
member count when available, channel type, parent category, NSFW/thread flags,
and Discord sort order. Historical import channels are not used as selector
options. Missing or deleted saved objects are displayed separately as missing
saved items so operators can remove stale values deliberately; the dashboard
does not silently delete saved IDs.

Settings → Discord Roles & Channels includes a Discord Metadata Preview showing
roles, categories, channels, last refresh time, and the latest refresh error if
one exists, plus the remaining dashboard-managed Discord selector settings.
The Refresh Discord Metadata button queues the fixed
`refresh_discord_metadata` dashboard action. The live bot process handles that
action from its existing dashboard action worker and snapshots current guild
roles/channels/categories into SQLite. The FastAPI dashboard still does not
start a second Discord client and does not expose arbitrary API calls.

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

### Knowledge Base / Guide Manager

The authenticated Settings → Knowledge Base page lists only a fixed
source-code allowlist. It does not accept paths, browse directories, follow
symlinks outside the project, or expose a file-download route. The allowlist
includes:

- Public bot knowledge: `data/knowledge/rules.md` and
  `data/knowledge/survival_guide.md`.
- Private staff knowledge:
  `data/staff_knowledge/rangers_handbook.md`.
- Internal guides under `docs/`: message context, staff context, checklists,
  historical imports, VC log imports, and the codebase map.

Each entry shows its category, public/staff/internal visibility, relative path,
editability, modified time, size, approximate word count, and
found/missing/empty state. Search and filters run in the browser against this
already allowlisted metadata. Document previews are escaped plain text, so raw
HTML and scripts are not executed. Obvious token, password, API-key, secret,
authorization, and provider-token patterns are redacted from displayed content
and rejected on save.

The public Rules and Survival Guide, private Ranger's Handbook, message-context
guide, and staff-context guide are editable. Import guides, checklist docs, and
the codebase map are intentionally read-only in this dashboard phase. Edits are
limited to UTF-8 Markdown/text, reject binary or content over 1 MB, and use a
temporary file plus atomic replacement. Before replacing an existing file, the
dashboard creates a timestamped copy under `backups/knowledge/`; those runtime
backups are ignored by Git. `knowledge_audit` records metadata for edits and
reindex requests without storing old or new document contents.

Knowledge loaders are cached inside the Discord bot process. Reindex buttons
therefore enqueue only the fixed `reindex_knowledge` action in the existing
`dashboard_actions` table. The live Stats cog action worker validates that
fixed payload, clears and reloads the existing public/staff knowledge caches,
then marks the action completed or failed. The dashboard never creates a
second Discord client and does not build a duplicate knowledge index.

The Knowledge Manager requires the existing signed login session. Every edit
and reindex POST requires CSRF protection. This phase is a document manager,
not an AI prompt-testing console, import manager, bank manager, checklist
manager, terminal, or public knowledge portal.

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

After the owner IDs and Pi permissions are configured, `/bot deploy` provides
the confirmation-gated Discord shortcut. Historical imports are never launched
from Discord; continue using `bedimportdry` and `bedimport` in the terminal.
