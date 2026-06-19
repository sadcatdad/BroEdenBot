# BroEdenBot

BroEdenBot is a Discord community bot for Bro Eden. It provides moderation
guidance, staff notes, voice-channel activity tracking, live statistics,
queues, polls, leaderboards, and bank tracking.

The bot loads every Python cog in `cogs/` and synchronizes its application
commands when it starts.

## Command notation

- `<input>` means the input is required.
- `[input]` means the input is optional.
- **Ephemeral** responses are visible only to the person who ran the command.
- Legacy queue commands use the `!` prefix.

## Permissions

| Feature | Who can use it |
| --- | --- |
| ModAI commands and context menus | Administrators or roles listed in `MODAI_ALLOWED_ROLE_IDS` |
| Staff-note commands | Administrators or roles listed in `STAFF_NOTES_ALLOWED_ROLE_IDS` |
| `/staffnote delete` | Administrators only |
| `/staffnote edit` | Administrators or the original note author |
| VC stats and reward-preview commands | Administrators or roles listed in `VCSTATS_ALLOWED_ROLE_IDS` |
| `/vcstats reset` | Administrators only |
| Bank commands | Administrators or roles listed in `BANK_ALLOWED_ROLE_IDS` |
| Stats creation and refresh commands | Administrators or roles listed in `STATS_ALLOWED_ROLE_IDS` |
| `/stats delete` and `/stats reset` | Administrators only |
| Poll, queue, and leaderboard commands | No additional role check is currently implemented in their cogs |

If a role-ID environment variable is empty, that feature is effectively
administrator-only.

## ModAI commands

All ModAI responses are private unless an authorized staff member deliberately
uses the **Send Rule Reminder** button. ModAI provides guidance only. It does
not warn, timeout, kick, ban, delete messages, or otherwise moderate members
automatically.

### `/modai check <text>`

Privately reviews pasted text for possible moderation concerns using Gemini and
the local Bro Eden rules and survival guide.

- `text` — The text staff want reviewed. This is treated as untrusted content
  and is not stored by this command.

The response can include possible concern categories, relevant rules, context
considerations, suggested staff action, a draft response, handling route, and
whether more context is needed.

### `/modai rulesearch <query>`

Performs a local keyword search of the Bro Eden rules and survival guide. This
does not call Gemini.

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

The VC stats module tracks non-bot members while BroEdenBot is online. Tracking
begins when the module is deployed; Discord does not provide historical VC
time from before that point. Joining, leaving, and switching voice channels
creates session records in `data.db`.

All `/vcstats` and `/vcrewards` responses are ephemeral. The module records all
completed sessions but separately calculates reward-eligible time for future
use. It does not currently grant XP, currency, roles, or other rewards.

A session is reward-eligible when it lasts at least five minutes and the member
was not in the server's configured AFK channel, alone for the entire session,
or self-deafened for the entire session. A best-effort heartbeat updates active
sessions once per minute. Time while the bot is offline is not counted.

### `/vcstats user <user> [days]`

Shows a member's total tracked time, reward-eligible time, session count, top
voice channel, and average session length.

- `user` — Member whose VC activity should be displayed.
- `days` — Optional lookback period from 1 to 3,650 days. Defaults to 30.

### `/vcstats leaderboard [days] [limit] [eligible_only]`

Ranks members by tracked or reward-eligible VC time.

- `days` — Optional lookback period. Defaults to 30.
- `limit` — Optional number of members from 1 to 25. Defaults to 10.
- `eligible_only` — When true, ranks by reward-eligible time instead of all
  tracked time. Defaults to false.

### `/vcstats current`

Shows members currently tracked in voice channels, including their channel,
current session duration, and available mute/deafen status.

### `/vcstats channel [channel] [days]`

Shows activity for one voice channel or the ten most-used voice channels.

- `channel` — Optional voice channel. Leave blank to show the top channels.
- `days` — Optional lookback period. Defaults to 30.

### `/vcstats export [days] [user] [channel]`

Exports completed VC sessions to an ephemeral CSV attachment.

- `days` — Optional lookback period. Defaults to 30.
- `user` — Optional member filter.
- `channel` — Optional voice-channel filter.

The CSV includes member and channel identifiers, timestamps, tracked and
counted durations, eligibility, and best-effort mute/deafen/alone flags.

### `/vcstats reset <confirm>`

Clears completed and active VC sessions for the current server when `confirm`
is true. This is administrator-only and does not clear future reward snapshot
tables.

### `/vcstats settings`

Shows the current reward-preparation rules, including minimum session length
and the AFK, alone, and self-deafened exclusions.

## Voice reward-preview commands

Reward previews use reward-eligible VC time but do not save or grant rewards.
Daily caps are calculated by UTC day.

### `/vcrewards preview [days] [minutes_per_point] [daily_cap_minutes]`

Shows estimated future reward points for up to 25 members.

- `days` — Optional lookback period. Defaults to 7.
- `minutes_per_point` — Eligible minutes required for one point. Defaults to
  60.
- `daily_cap_minutes` — Maximum eligible minutes counted per member per UTC
  day. Defaults to 180.

### `/vcrewards export [days] [minutes_per_point] [daily_cap_minutes]`

Exports the full reward preview to an ephemeral CSV attachment using the same
calculation options as `/vcrewards preview`.

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

Immediately refreshes every tracked role roster and tracked stats report in
the server. It has no inputs.

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

## Bank commands

Bank commands use the separate `brobank.db` SQLite database.

### `/bank add <user> <amount> <note>`

Records a public contribution and refreshes the configured public bank summary.

- `user` — Member credited with the contribution.
- `amount` — Positive contribution amount.
- `note` — Short, public-safe description of the contribution.

### `/bank expense <amount> <note>`

Records an expense and refreshes the configured public bank summary.

- `amount` — Positive amount spent.
- `note` — Description of what the funds supported.

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

### `/bank clear`

Deletes all bank transaction test data and clears the configured summary
message ID. This is destructive and cannot be undone through the bot.

## Leaderboard commands

Leaderboard commands currently have no extra role check in their cog.

### `/leaderboard create <name>`

Creates a named leaderboard. If the name already exists, it is kept/replaced.

- `name` — Name used to identify the leaderboard.

### `/leaderboard delete <name>`

Deletes a leaderboard and all point records attached to it.

- `name` — Existing leaderboard name. Discord provides autocomplete.

### `/leaderboard add <leaderboard> <user> <points>`

Records points for a user on a leaderboard.

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

Displays a leaderboard as a paginated graphic, ten members per page.

- `name` — Existing leaderboard name, with autocomplete.

## Poll command

### `/poll <question> <options> <time>`

Creates an interactive poll in the current channel.

- `question` — Poll title/question.
- `options` — Comma-separated choices, with a maximum of 26. Example:
  `Friday, Saturday, Sunday`.
- `time` — Poll duration. Supported units include seconds, minutes, hours,
  days, weeks, months, and years. Examples: `30m`, `1h`, or `2d`.

Members vote using buttons. Running the command privately confirms creation;
the poll itself is public. When time expires, the active poll is replaced with
the final results.

## Queue slash commands

Queue state is separate for each channel.

### `/queue dashboard`

Posts a public queue dashboard in the current channel. The dashboard includes
buttons to join, leave, delay one position, and pull the next member.

Joining through the dashboard requires the member to be connected to the queue
voice channel represented by the current channel.

### `/queue lock`

Locks the current channel's queue so new members cannot join.

### `/queue unlock`

Unlocks the current channel's queue.

### `/queue move <user> <position>`

Moves an existing queue member to a numbered position.

- `user` — User already in the current channel's queue.
- `position` — Desired one-based position. Negative values are converted to
  positive values.

### `/queue remove <user>`

Removes a user from the current channel's queue.

- `user` — User to remove.

## Legacy queue commands

These message commands use the bot prefix `!`.

| Command | Function |
| --- | --- |
| `!q` | Posts or refreshes the current channel's queue dashboard. |
| `!qj` | Joins the queue. The caller must be in the matching queue voice channel, and the queue must be unlocked. |
| `!ql` | Leaves the current channel's queue. |
| `!qd` | Moves the caller one place later in the queue. |
| `!qn` | Pulls the first member from the queue and announces who is next. |

## Environment variables

Create a `.env` file in the project root. Do not commit it.

| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Discord bot token. Required. |
| `GEMINI_API_KEY` | Gemini API key used by ModAI. |
| `MODAI_MODEL` | Primary Gemini model. |
| `MODAI_FALLBACK_MODEL` | Model tried after retryable primary-model failures. |
| `MODAI_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to use ModAI. |
| `STAFF_NOTES_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to use staff notes. |
| `VCSTATS_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to use VC stats and reward previews. |
| `BANK_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to use bank commands. |
| `STATS_ALLOWED_ROLE_IDS` | Comma-separated Discord role IDs allowed to create and refresh stats pages. |

## Run locally

```bash
cd /path/to/BroEdenBot
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

The bot enables voice-state and member intents in code. Enable **Server Members
Intent** for the bot in the Discord Developer Portal so startup VC scans and
member information work correctly.

## Deploy on the Raspberry Pi

```bash
cd ~/BroEdenBot
./deploy.sh
```

The deployment script should restart the bot. If it does not, restart the bot
service or process manually so updated cogs and slash-command definitions are
loaded and synchronized.
