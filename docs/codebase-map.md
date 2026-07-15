# BroEdenBot Codebase Map

This map describes the live architecture and the boundaries used during
maintenance.

## Runtime

- `main.py` — Startup, shared SQLite connection, optional `ENABLED_MODULES`
  cog gating, command sync, global interaction errors, mention policy, and
  graceful shutdown.
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
  SQLite backups, a VC XP role-pulse readiness summary, stats management, an
  AI framework status/usage page, a unified Knowledge manager for file-backed,
  manual AI, and live Discord sources, and aggregate server analytics. Discord
  role/channel pickers read a live-guild metadata snapshot written by the bot,
  not historical import tables.
- `dashboard/templates/embeds.html`, `dashboard/templates/embed_edit.html`, and
  `dashboard/static/embed_editor.js` — Searchable saved-embed inventory,
  Discord-style live editor/preview, Unicode emoji chooser, dynamic fields,
  and role/URL button configuration.
- `dashboard/streaks_manager.py` — Streak summaries, durable history-restore
  requests, audited source-day adjustments, and current/longest recalculation
  for the top-level Streaks dashboard page.
- `dashboard/users.py` — Dashboard user schema, PBKDF2 password bootstrap,
  Discord identity linking, active/disabled status, and owner/admin/viewer
  roles.
- `dashboard/oauth.py` — Fixed Discord OAuth2 `identify` authorization, token
  exchange, and identity fetch; access tokens are never persisted.
- `broeden-dashboard.service.example` — Optional standalone systemd unit
  template for the dashboard; it does not replace the bot service.

## Member-facing tools

- `cogs/ask.py` — Private Gemini answers grounded only in public knowledge sources.
- `cogs/poll.py` — Persistent button polls and visual result boards.
- `cogs/queue.py` — Voice-channel queues and legacy prefix commands.
- `cogs/leaderboards.py` — Custom banner/accent leaderboards, score controls,
  confirmations, point summaries, and live milestone roles.
- `cogs/disboard_bumps.py` — Verified DISBOARD `/bump` points, reward-role
  handoff, automatic two-hour reminder delivery, saved reminder embeds,
  self-service embed role buttons, and Bump Legends publishing.
- `cogs/streaks.py` — Daily public-message streak qualification, deletion
  reconciliation, milestones, current/longest graphical leaderboards,
  heartbeat gap detection, and restart-safe Discord-history recovery.
- `cogs/bank.py` — Contribution ledger and public bank summary.

## Staff and analytics tools

- `cogs/bot_admin.py` — Owner-only private status, logs, restart, and deploy
  controls. Historical imports remain terminal-only.
- `cogs/ai.py` — Owner/admin-only AI framework test, status, and AI KB
  management commands.
- `cogs/rulecard.py` — Staff-only AI-assisted rule reminder draft previews and
  the Draft Rule Reminder message context menu.
- `cogs/mod_ai.py` — Private Gemini-assisted moderation guidance.
- `cogs/staff_ai.py` — Role/owner-restricted historical/live staff-context
  capture, status, search, questions, and scoped summaries.
- `cogs/knowledge_sources.py` — Manage Discord text channels, whole forum
  channels, or specific forum posts/threads as public or staff-only live
  knowledge sources, backfill history, and keep indexed entries current from
  message create/edit/delete events.
- `cogs/message_context.py` — Disabled-by-default full-server content capture,
  staff-only search, summaries, user/channel reviews, and timelines.
- `cogs/staff_notes.py` — Manual private staff records.
- `cogs/stats.py` — Live/imported activity reports and roster graphics.
- `cogs/reminder.py` — Staff reminder modals, natural-language/personal
  timezone parsing, public subscribe cards, and persistent scheduled DMs.
- `cogs/vc_stats.py` — Voice-session tracking, muted/deafened interval
  exclusion for VC XP, active eligible-time pulse cooldowns, XP-only role
  exclusions, and trigger-role adds for external MEE6 automation.

## Shared helpers

- `utils/ui.py` — Brand colors, status embeds, progress bars, and truncation.
- `utils/access.py` — Shared configured owner/staff identity and role checks.
- `utils/audit_log.py` — Optional mention-safe publishing to the configured
  Discord audit thread.
- `utils/display_names.py` — Unicode normalization for readable, safe display
  names without changing stored Discord identity data.
- `utils/streaks.py` — Shared additive streak/recovery schema plus pure streak
  and milestone calculations used by both the bot and dashboard.
- `utils/knowledge.py` — Public and private-staff knowledge search over
  public/staff-filtered live Discord knowledge entries, with local file caches
  kept empty for legacy source compatibility.
- `utils/live_knowledge.py` — `knowledge_sources` / `knowledge_entries`
  schema, Discord message/embed/forum formatting, duplicate filtering, AI KB
  mirroring, and live-source search helpers.
- `utils/knowledge_manager.py` — Fixed internal document allowlist, safe UTF-8
  reads/atomic edits, knowledge backups and audit rows, and queued bot-side
  cache reloads for the local dashboard.
- `utils/analytics.py` — Fixed-range, parameterized read-only aggregation over
  existing text and VC activity tables, including dashboard summaries,
  leaderboards, heatmap data, and content-free CSV exports.
- `utils/member_filter.py` — Current-member filtering safeguards.
- `utils/stats_visuals/` — Central dashboard-derived brand tokens, profiles,
  reusable Pillow components, avatar cache/fallbacks, deterministic pagination,
  uploaded banner/background support, structured render results, diagnostics,
  and per-PNG byte-limit enforcement.
- `utils/compact_roster.py`, `utils/ranked_graphic.py`, and
  `utils/stats_reports.py` — Compatibility adapters into the centralized stats
  visual system.
- `scripts/generate_stats_visual_samples.py` — Deterministic ignored visual
  review set for overview, activity, role, leaderboard, empty, and stress
  states.
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
- `utils/ai_config.py` — Environment-backed shared AI configuration, model
  tier defaults, budget limits, token limits, cooldown values, logging flags,
  and dashboard visibility.
- `utils/ai_costs.py` — Static Gemini pricing map plus rough token and cost
  estimation helpers.
- `utils/ai_service.py` — Reusable Gemini model router, budget guardrails,
  `ai_usage_logs` schema/logging, cooldown helpers, and normalized result
  objects for future AI commands.
- `utils/ai_kb.py` — Shared `ai_kb_sources` / `ai_kb_chunks` schema, chunking,
  keyword search, source upsert/delete, live-source type support, and
  dashboard/Discord KB helpers.
- `utils/settings.py` — Allowlisted, validated runtime settings stored in
  `data.db`, with environment fallback and a non-secret dashboard audit trail.
- `utils/embed_templates.py` — Saved message/embed schema, Discord-limit and URL
  validation, feature-use protection, and runtime Discord embed/button builders.
- `utils/discord_metadata.py` — Shared SQLite snapshot and fixed dashboard
  action helpers for live Discord roles, categories, and channels used by the
  dashboard settings pickers.
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
- Streak recovery requests persist in SQLite. Automatic requests are queued
  after heartbeat gaps, dashboard requests can be queued while the bot is
  offline, and interrupted processing returns to pending on the next load.

## Validation

```bash
PYTHONPYCACHEPREFIX=/tmp/broeden-pycache \
  .venv/bin/python -m compileall -q main.py config.py cogs utils scripts

.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
.venv/bin/python -m riffbot.setup safety-check
```
