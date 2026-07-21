# Dashboard configuration inventory

Inventory date: 2026-07-20. Values are intentionally omitted. The source column shown in the UI is computed live so operators can see whether each effective value comes from SQLite, the environment, a built-in default, or nowhere.

## Precedence and validation

For allowlisted runtime settings, precedence is **database → environment → declared default**. Environment seeding never overwrites an existing database row. Successful reads are cached only as resilience against a temporary SQLite read failure. A dashboard save validates the whole feature form before one transaction writes `bot_settings` and `bot_settings_audit`.

`csv_ids` and `json_ids` accept only 17–20 digit Discord snowflakes and retain them as strings in forms/JSON. Integers enforce declared minima; booleans accept only true/false; asset/embed IDs must reference numeric saved IDs; datetimes normalize to UTC ISO 8601; text enforces length limits. Keys containing token, secret, password, API key, or private key markers are forbidden from dashboard editing.

The primary display name is the registry title where defined; otherwise it is a humanized label with AI/API/CSV/DB/ID/JSON/URL/VC/XP acronyms normalized. Raw keys appear only in collapsed Technical details.

## Current non-secret source snapshot

- **Database:** `ASK_ALLOWED_CHANNEL_IDS`, `ASK_COOLDOWN_SECONDS`, `MODAI_ALLOWED_ROLE_IDS`, `STAFF_NOTES_ALLOWED_ROLE_IDS`, `STATS_ALLOWED_ROLE_IDS`, `VCSTATS_ALLOWED_ROLE_IDS`, `BANK_ALLOWED_ROLE_IDS`, `VCXP_ENABLED`, `VCXP_TRIGGER_ROLE_ID`, `VCXP_REWARD_START_AT`, `VC_XP_PULSE_MINUTES`, the four hidden VC XP compatibility keys, and `message_context_excluded_channel_ids`.
- **Environment:** `GUILD_ID`, `MODAI_MODEL`, `MODAI_FALLBACK_MODEL`, `ASK_MODEL`, `ASK_FALLBACK_MODEL`.
- **Default:** reminder timezone/legacy/grace/creator defaults; streak timezone/qualification/message/recovery defaults; built-in bump messages/points; `import_context_only_default`.
- **Not configured:** every remaining blank-default key in the tables below.

This is a checkout snapshot, not a production claim. The dashboard is the authoritative live source indicator after deployment.

## Registry by owning feature

Dashboard values are `edit`, `read-only`, or `hidden`. `blank` means no built-in default.

### Ask

| Key | Type | Default | Dashboard |
|---|---|---|---|
| `ASK_ALLOWED_CHANNEL_IDS` | `csv_ids` | blank | edit |
| `ASK_COOLDOWN_SECONDS` | `int` | `30` | edit |
| `ASK_MODEL` | `string` | blank | read-only |
| `ASK_FALLBACK_MODEL` | `string` | blank | read-only |
| `ask_command_allowed_channel_ids` | `json_ids` | blank | hidden compatibility |
| `ask_command_allowed_category_ids` | `json_ids` | blank | hidden compatibility |

The `/ask` runtime reads the uppercase channel/cooldown values and model environment values. Lowercase selector values remain compatibility data and are not duplicated in the normal UI.

### Staff and moderation tools

| Key | Type | Default | Dashboard |
|---|---|---|---|
| `MODAI_ALLOWED_ROLE_IDS` | `csv_ids` | blank | edit |
| `STAFF_AI_ALLOWED_ROLE_IDS` | `csv_ids` | blank | edit |
| `STAFF_NOTES_ALLOWED_ROLE_IDS` | `csv_ids` | blank | edit |
| `MESSAGE_CONTEXT_ALLOWED_ROLE_IDS` | `csv_ids` | blank | edit |
| `BOT_OWNER_USER_IDS` | `csv_ids` | blank | edit |
| `AUDIT_LOG_THREAD_ID` | `csv_ids` | blank | edit |
| `MODAI_MODEL` | `string` | blank | read-only |
| `MODAI_FALLBACK_MODEL` | `string` | blank | read-only |
| `staff_role_ids` | `json_ids` | blank | hidden compatibility |
| `message_context_excluded_channel_ids` | `json_ids` | blank | edit |

All visible values have consumers in staff/moderation/context code. Model values remain environment-controlled. Private staff context is not shared with public Ask.

### Reminders and events

| Key | Type | Default | Dashboard |
|---|---|---|---|
| `REMINDER_ALLOWED_ROLE_IDS` | `csv_ids` | blank | edit (legacy fallback) |
| `REMINDER_PERSONAL_ALLOWED_ROLE_IDS` | `csv_ids` | blank | edit |
| `REMINDER_EVENT_ALLOWED_ROLE_IDS` | `csv_ids` | blank | edit |
| `REMINDER_MANAGE_ALLOWED_ROLE_IDS` | `csv_ids` | blank | edit |
| `REMINDER_MANAGE_ALL_ROLE_IDS` | `csv_ids` | blank | edit |
| `REMINDER_SUBSCRIPTIONS_ALLOWED_ROLE_IDS` | `csv_ids` | blank | edit |
| `REMINDER_TIMEZONE` | `string` | `America/Chicago` | edit |
| `REMINDER_DELIVERY_GRACE_MINUTES` | `int` | `120` | edit |
| `REMINDER_EVENT_AUTO_SUBSCRIBE_CREATOR` | `bool` | `true` | edit |
| `ENABLE_LEGACY_REMINDER_COMMANDS` | `bool` | `true` | Advanced |
| `EVENTS_HEADER_ASSET_ID` | `asset_id` | blank | edit on Events |

Every key is read by the reminder/event service or its compatibility command gate. The fallback and command-specific roles intentionally overlap with documented precedence; they are not duplicate writes.

### Activity streaks

| Key | Type | Default | Dashboard |
|---|---|---|---|
| `STREAK_TIMEZONE` | `string` | `America/Chicago` | edit |
| `STREAK_MIN_WORDS` | `int` | `4` | edit |
| `STREAK_DUPLICATE_LOOKBACK_DAYS` | `int` | `30` | edit |
| `STREAK_EXCLUDED_CHANNEL_IDS` | `csv_ids` | blank | edit |
| `STREAK_EXCLUDED_CATEGORY_IDS` | `csv_ids` | blank | edit |
| `STREAK_MILESTONE_CHANNEL_ID` | `csv_ids` | blank | edit |
| `STREAK_MILESTONE_MESSAGE` | `string` | built-in congratulations message | hidden presentation compatibility |
| `STREAK_MILESTONE_ASSET_ID` | `asset_id` | blank | edit |
| `STREAK_LEADERBOARD_CHANNEL_ID` | `csv_ids` | blank | edit |
| `STREAK_RESTORE_ENABLED` | `bool` | `true` | edit |
| `STREAK_RESTORE_GAP_MINUTES` | `int` | `10` | edit |
| `STREAK_RESTORE_MAX_DAYS` | `int` | `14` | edit |
| `STREAK_RESTORE_MAX_MESSAGES` | `int` | `50000` | edit |

All keys have live streak/recovery/render consumers. Fixed milestone thresholds are code behavior, not a decorative setting.

### DISBOARD bumps

| Key | Type | Default | Dashboard |
|---|---|---|---|
| `DISBOARD_BOT_USER_ID` | `csv_ids` | blank | edit |
| `BUMP_REWARD_ROLE_ID` | `csv_ids` | blank | edit |
| `BUMP_SUCCESS_MESSAGE` | `string` | built-in response | hidden presentation compatibility |
| `BUMP_SUCCESS_EMBED_ID` | `embed_id` | blank | hidden predecessor |
| `BUMP_SUCCESS_ASSET_ID` | `asset_id` | blank | edit |
| `BUMP_PING_ROLE_ID` | `csv_ids` | blank | edit |
| `BUMP_REMINDER_MESSAGE` | `string` | `{role}` | hidden presentation compatibility |
| `BUMP_REMINDER_EMBED_ID` | `embed_id` | blank | hidden predecessor |
| `BUMP_REMINDER_ASSET_ID` | `asset_id` | blank | edit |
| `BUMP_LEADERBOARD_CHANNEL_ID` | `csv_ids` | blank | edit |
| `BUMP_POINTS_PER_SUCCESS` | `int` | `1000` | edit |

The runtime consumes all keys. Asset IDs are canonical; predecessor embed IDs remain a fallback for existing data and are not shown.

### Analytics and leaderboards

| Key | Type | Default | Dashboard |
|---|---|---|---|
| `STATS_ALLOWED_ROLE_IDS` | `csv_ids` | blank | edit |
| `ACTIVITY_EXCLUDED_ROLE_IDS` | `csv_ids` | blank | edit |
| `ACTIVITY_EXCLUDED_USER_IDS` | `csv_ids` | blank | edit |
| `LEADERBOARD_RESET_ROLE_IDS` | `csv_ids` | blank | edit |
| `analytics_excluded_channel_ids` | `json_ids` | blank | edit |
| `analytics_excluded_category_ids` | `json_ids` | blank | edit |
| `bot_role_ids_excluded_from_stats` | `json_ids` | blank | hidden compatibility |

The analytics service reads these settings; channel/category selectors use the live metadata snapshot. User IDs remain plain snowflake input because the snapshot intentionally does not enumerate members.

### Voice statistics and VC XP

| Key | Type | Default | Dashboard |
|---|---|---|---|
| `VCSTATS_ALLOWED_ROLE_IDS` | `csv_ids` | blank | edit |
| `VC_EXCLUDED_ROLE_IDS` | `csv_ids` | blank | edit |
| `VC_EXCLUDED_USER_IDS` | `csv_ids` | blank | edit |
| `EXCLUDED_VOICE_CHANNEL_IDS` | `csv_ids` | blank | edit |
| `VCXP_ENABLED` | `bool` | `false` | edit |
| `VCXP_TRIGGER_ROLE_ID` | `csv_ids` | blank | edit |
| `VCXP_EXCLUDED_ROLE_IDS` | `csv_ids` | blank | edit |
| `VCXP_EXCLUDED_VOICE_CHANNEL_IDS` | `csv_ids` | blank | edit |
| `VCXP_REWARD_START_AT` | `datetime` | blank | edit |
| `VC_XP_PULSE_MINUTES` | `int` | `30` | edit |
| `VCXP_MINUTES_PER_PULSE` | `int` | `30` | hidden legacy |
| `VCXP_ROLE_REMOVE_DELAY_SECONDS` | `int` | `30` | hidden legacy |
| `VCXP_DAILY_PULSE_CAP` | `int` | `4` | hidden legacy |
| `VCXP_WEEKLY_PULSE_CAP` | `int` | `20` | hidden legacy |

The first ten values are current runtime configuration. The last four have no current behavioral consumer and remain only for compatibility/rollback; owner confirmation is required before deletion.

### Bank

| Key | Type | Default | Dashboard |
|---|---|---|---|
| `BANK_ALLOWED_ROLE_IDS` | `csv_ids` | blank | edit |
| `BANK_LOG_CHANNEL_ID` | `csv_ids` | blank | hidden orphan |
| `bank_allowed_role_ids` | `json_ids` | blank | hidden compatibility |
| `bank_log_channel_id` | `json_ids` | blank | hidden orphan/compatibility |

The bank cog reads only `BANK_ALLOWED_ROLE_IDS`; bank posting targets live in `bank_settings`. The three other keys are not presented as functional and are preserved pending production-data confirmation.

### Knowledge

| Key | Type | Default | Dashboard |
|---|---|---|---|
| `knowledge_allowed_channel_ids` | `json_ids` | blank | edit |
| `knowledge_allowed_category_ids` | `json_ids` | blank | edit |

Both values are consumed by knowledge access checks and use live Discord selectors.

### Imports and system

| Key | Type | Default | Dashboard |
|---|---|---|---|
| `import_archive_path` | `string` | blank | Advanced |
| `import_context_only_default` | `bool` | `false` | Advanced |
| `GUILD_ID` | `string` | blank | read-only/environment |
| `admin_role_ids` | `json_ids` | blank | hidden compatibility |

Import defaults are allowlisted technical controls. `GUILD_ID` is boot/OAuth configuration and cannot be changed from the dashboard. Legacy `admin_role_ids` is superseded for dashboard access by RBAC mappings but preserved for non-dashboard compatibility.

## Explicit flags

- **Saves but not read:** `BANK_LOG_CHANNEL_ID` (now hidden).
- **Legacy/no current behavior:** four hidden VC XP keys listed above.
- **Predecessor fallbacks:** `BUMP_SUCCESS_EMBED_ID`, `BUMP_REMINDER_EMBED_ID` and built-in message keys; hidden but still consumed.
- **Overlapping by design:** reminder legacy fallback roles versus command-specific roles; database values versus environment fallback.
- **Missing resource handling:** role/channel/category pickers use live snapshot names and retain missing IDs with a warning. No setting silently switches to imported activity metadata.
- **Safe removal:** none in this release. Hidden orphan/legacy keys require production database and rollback confirmation first.
