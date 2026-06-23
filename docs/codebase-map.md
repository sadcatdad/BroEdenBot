# BroEdenBot Codebase Map

This map describes the live architecture and the boundaries used during
maintenance.

## Runtime

- `main.py` — Startup, shared SQLite connection, cog loading, command sync,
  global interaction errors, mention policy, and graceful shutdown.
- `config.py` — Environment loading and the shared brand color.
- `data.db` — Stats, queues, polls, staff notes, moderation metadata, and VC
  tracking.
- `brobank.db` — Separate bank ledger and public-summary settings.
- `staff_context.db` — Separate private staff-channel message context used only
  by `/staffai`.
- `message_context.db` — Separate full-server staff message archive used only
  by `/context`.
- `dashboard/` — FastAPI dashboard with signed sessions, password-owner
  bootstrap, allowlisted Discord OAuth identities, role-aware write
  protection, safe configuration, fixed service controls, redacted logs,
  SQLite backups, stats management, an allowlisted Knowledge Manager, and
  aggregate server analytics.
- `dashboard/users.py` — Dashboard user schema, PBKDF2 password bootstrap,
  Discord identity linking, active/disabled status, and owner/admin/viewer
  roles.
- `dashboard/oauth.py` — Fixed Discord OAuth2 `identify` authorization, token
  exchange, and identity fetch; access tokens are never persisted.
- `broeden-dashboard.service.example` — Optional standalone systemd unit
  template for the dashboard; it does not replace the bot service.

## Member-facing tools

- `cogs/ask.py` — Private Gemini answers grounded only in public guidance.
- `cogs/guide.py` — Deterministic public-guide search without AI.
- `cogs/poll.py` — Persistent button polls and visual result boards.
- `cogs/queue.py` — Voice-channel queues and legacy prefix commands.
- `cogs/leaderboards.py` — Staff-managed scores with public PNG leaderboards.
- `cogs/bank.py` — Contribution ledger and public bank summary.

## Staff and analytics tools

- `cogs/bot_admin.py` — Owner-only private status, logs, restart, and deploy
  controls. Historical imports remain terminal-only.
- `cogs/mod_ai.py` — Private Gemini-assisted moderation guidance.
- `cogs/staff_ai.py` — Role/owner-restricted historical/live staff-context
  capture, status, search, questions, and scoped summaries.
- `cogs/message_context.py` — Disabled-by-default full-server content capture,
  staff-only search, summaries, user/channel reviews, and timelines.
- `cogs/staff_notes.py` — Manual private staff records.
- `cogs/stats.py` — Live/imported activity reports and roster graphics.
- `cogs/vc_stats.py` — Voice-session tracking and VC XP accounting.

## Shared helpers

- `utils/ui.py` — Brand colors, status embeds, progress bars, and truncation.
- `utils/knowledge.py` — Separate cached public and private-staff knowledge
  loaders, cache reload, and search.
- `utils/knowledge_manager.py` — Fixed document allowlist, safe UTF-8
  reads/atomic edits, knowledge backups and audit rows, and queued bot-side
  cache reloads for the local dashboard.
- `utils/analytics.py` — Fixed-range, parameterized read-only aggregation over
  existing text and VC activity tables, including dashboard summaries,
  leaderboards, heatmap data, and content-free CSV exports.
- `data/staff_knowledge/rangers_handbook.md` — Private Ranger operations and
  moderation guidance used by staff-facing AI only.
- `utils/member_filter.py` — Current-member filtering safeguards.
- `utils/compact_roster.py` — Multi-page roster image rendering.
- `utils/stats_reports.py` — Analytics PNG cards.
- `scripts/import_discord_history.py` — Streaming metadata-only history import,
  dedupe, batching, and optional archiving.
- `scripts/import_staff_context.py` — Separate streaming CSV importer for
  private staff message context. It never writes activity-statistics tables.
- `utils/staff_context.py` — Staff-context schema, dedupe, date, redaction, and
  source-reference helpers.
- `utils/privacy.py` — Shared obvious-credential redaction for private context
  storage, display, and Gemini retrieval.
- `utils/sqlite.py` — Shared WAL, busy-timeout, synchronization, and optional
  foreign-key setup for async SQLite connections.
- `utils/settings.py` — Allowlisted, validated runtime settings stored in
  `data.db`, with environment fallback and a non-secret dashboard audit trail.
- `utils/stats_manager.py` — Non-destructive dashboard management for existing
  tracked stats graphics, queued bot-side refreshes, archives, and member
  snapshot CSV exports.
- `scripts/import_message_context.py` — Streaming DiscordChatExporter CSV
  importer for the separate full-server archive.
- `scripts/import_full_csv_exports.py` — Imports every full-server CSV into
  private context and backfills counts only for channels not covered by JSON.
- `utils/import_helpers.py` — Shared filename channel inference and completed
  JSON activity-channel detection.
- `utils/message_context.py` — Archive schema, FTS, access, date, retention,
  channel inference, and deterministic import-ID helpers.

## Interaction design

- Public dashboards use dark PNG cards, the Bro Eden pink accent, compact
  metrics, and clear empty states.
- Private actions return branded success, warning, or error embeds.
- Generated content cannot ping everyone or roles by default.
- Destructive and staff-control actions require explicit permissions.
- Gateway intents and the generated invite use the minimum event and Discord
  permissions required by the current feature set.
- Persistent component IDs remain below Discord's 100-character limit and do
  not embed arbitrary member-supplied text.

## Reliability boundaries

- Message content is not stored by `/ask`, ModAI review metadata, or historical
  activity imports. `staff_context.db` and `message_context.db` are separate
  private archives with separate restricted command groups. Public `/ask`
  opens neither database.
- Poll options are JSON-encoded. Legacy Python-list rows are read with
  `ast.literal_eval`, never `eval`.
- Network image downloads use timeouts and visual fallbacks.
- Queue writes are serialized per channel and duplicate membership is blocked
  with a database index. The active dashboard message ID is persisted so a
  restart does not create duplicate dashboards.
- Failed cogs are logged and retained for `/bot status`; unhandled
  application-command errors get a private user-safe response.
- Shutdown unloads cogs and cancels their background work before the shared
  SQLite connection is closed.

## Validation

```bash
PYTHONPYCACHEPREFIX=/tmp/broeden-pycache \
  .venv/bin/python -m compileall -q main.py config.py cogs utils scripts

.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```
