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

## Member-facing tools

- `cogs/ask.py` — Private Gemini answers grounded only in public guidance.
- `cogs/guide.py` — Deterministic public-guide search without AI.
- `cogs/poll.py` — Persistent button polls and visual result boards.
- `cogs/queue.py` — Voice-channel queues and legacy prefix commands.
- `cogs/leaderboards.py` — Staff-managed scores with public PNG leaderboards.
- `cogs/bank.py` — Contribution ledger and public bank summary.

## Staff and analytics tools

- `cogs/admin.py` — Private health and deployment diagnostics.
- `cogs/mod_ai.py` — Private Gemini-assisted moderation guidance.
- `cogs/staff_notes.py` — Manual private staff records.
- `cogs/stats.py` — Live/imported activity reports and roster graphics.
- `cogs/vc_stats.py` — Voice-session tracking and VC XP accounting.

## Shared helpers

- `utils/ui.py` — Brand colors, status embeds, progress bars, and truncation.
- `utils/knowledge.py` — Cached public rules and guide search.
- `utils/member_filter.py` — Current-member filtering safeguards.
- `utils/compact_roster.py` — Multi-page roster image rendering.
- `utils/stats_reports.py` — Analytics PNG cards.
- `scripts/import_discord_history.py` — Streaming metadata-only history import,
  dedupe, batching, and optional archiving.

## Interaction design

- Public dashboards use dark PNG cards, the Bro Eden pink accent, compact
  metrics, and clear empty states.
- Private actions return branded success, warning, or error embeds.
- Generated content cannot ping everyone or roles by default.
- Destructive and staff-control actions require explicit permissions.
- Persistent component IDs remain below Discord's 100-character limit and do
  not embed arbitrary member-supplied text.

## Reliability boundaries

- Message content is not stored by `/ask`, ModAI review metadata, or historical
  activity imports.
- Poll options are JSON-encoded. Legacy Python-list rows are read with
  `ast.literal_eval`, never `eval`.
- Network image downloads use timeouts and visual fallbacks.
- Queue writes are serialized per channel and duplicate membership is blocked
  with a database index. The active dashboard message ID is persisted so a
  restart does not create duplicate dashboards.
- Failed cogs are logged at startup; unhandled application-command errors get a
  private user-safe response.

## Validation

```bash
PYTHONPYCACHEPREFIX=/tmp/broeden-pycache \
  .venv/bin/python -m compileall -q main.py config.py cogs utils scripts

.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```
