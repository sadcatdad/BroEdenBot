# Dashboard audit

Audit date: 2026-07-20. Source of truth: the FastAPI routes, Jinja templates, runtime setting registry, Discord cogs, SQLite schemas, tests, and `deploy.sh` in this checkout. The local databases passed `PRAGMA quick_check`; the pre-change suite passed 420 tests. No production logs were present in the checkout, so runtime-use claims below are based on registration, static consumers, local schemas, and tests unless explicitly noted.

## Executive findings

- The dashboard is a server-rendered FastAPI/Jinja application with a coherent dark Bro Eden visual base, but the former Settings page mixed 60+ unrelated fields, exposed raw keys first, and used one Save button per row.
- Password authentication was hashed and CSRF/OAuth state protections existed. Discord OAuth previously fetched only `/users/@me`; guild membership and current roles were not verified.
- Authorization previously collapsed to owner/admin/viewer and a broad write check. Page navigation was not capability-aware and signed-cookie role claims were not reloaded from SQLite per request.
- Live Discord role/channel/category/emoji snapshots and compact searchable selectors already existed and were worth preserving.
- Settings already used database-first, environment-second, default-third precedence. Most allowlisted fields are consumed at runtime. Five VC XP compatibility keys and two lowercase bank compatibility keys are intentionally hidden. `BANK_LOG_CHANNEL_ID` had no runtime consumer and is now hidden instead of being presented as functional.
- The checkout contains no reliable production-usage telemetry. Valid registered features without local usage evidence remain `UNKNOWN`, not deleted.

## Page and route disposition

| Page or route | Purpose and audience | Findings | Disposition |
|---|---|---|---|
| `/login`, `/auth/discord/*` | Owner recovery and Discord staff login | OAuth state was one-time, but guild/role verification was absent | Improve: guild-member OAuth scope, current-role verification, audit events |
| `/` | Owner/staff health overview | Useful summary; VC XP and service cards are operationally relevant | Keep |
| `/analytics/*`, `/stats/*` | Aggregate analytics, managed stats, exports | Strong domain separation; legacy `/stats` URLs remain bookmarks | Keep; preserve redirects |
| `/streaks` | Streak health and audited recovery | Focused and actively tested | Keep |
| `/features` | Feature discovery and configuration health | Missing before this pass | Add feature hub |
| `/features/{feature}` | Focused feature settings | Replaces the giant miscellaneous form | Add; permission-aware and bulk-saved |
| `/operations` | Fixed service status, logs, backup, restart | Useful, but actions need granular authorization/audit | Improve |
| `/operations/reminders/*` | Reminder delivery management | Correctly separate operational workflow | Keep |
| `/embeds/*` | Message Studio assets | Mature editor; settings links used old terminology | Keep; rename links to Features |
| `/visual/*`, `/api/visual/*` | Visual Content Studio | Mature draft/publish workflow with its own audit data | Keep; add dashboard capability guard and publication audit |
| `/knowledge/*` | Public/staff knowledge management | Correct top-level domain; old Settings URLs duplicate it | Keep; legacy URLs redirect |
| `/ai/*` | AI health and AI KB | Appropriate top-level content area | Keep; capability guard |
| `/bank` | Read-only finance summary | Dashboard intentionally does not mutate ledger | Keep |
| `/settings` | Cross-cutting system configuration | Formerly an unrelated field dump | Replace with system hub |
| `/settings/access` | Roles, mappings, assignments, overrides | Missing before this pass | Add; owner-only `access.manage` |
| `/settings/discord` | Live guild-catalog status | Former page mixed metadata and feature selectors | Improve: compact status-only page |
| `/settings/imports` | Read-only import history | Correct under Data & Storage | Keep |
| `/settings/advanced` | Technical/compatibility controls | Needed, but routine fields did not belong here | Improve: allowlisted system/import fields only |
| `/settings/audit` | Security and administrator action history | Missing before this pass | Add; filterable append-only log |
| `/settings/users`, `/users` | Former minimal user table | Duplicated access management | Merge into Dashboard Access; retain redirects |
| `/settings/features`, `/settings/permissions` | Former giant/permission settings URLs | Duplicated new feature/access organization | Redirect; do not render duplicate forms |
| `/api/discord/{roles,channels,categories,emojis,guild-structure}` | Local snapshot for selectors | Good local-first API; previously broad auth only | Keep; require `discord_metadata.view` |
| `/health` | Unauthenticated process health | No sensitive payload | Keep public |

All mutating route families (`/settings`, `/features`, `/operations`, `/analytics`, `/streaks`, `/embeds`, `/visual`, `/knowledge`, `/ai`, and reminder actions) now pass through a server-side capability policy before their existing CSRF and domain validation. Dynamic feature pages additionally check the feature’s own view capability. Unauthorized API requests return JSON 403; unauthorized pages render a styled 403 without loading page data.

## UX and design-system findings

Implemented improvements:

- task-oriented navigation groups: Monitor, Community, Operations, Content, Finance, System;
- permission-filtered links and controls;
- plain-language setting labels with raw keys moved into collapsed Technical details;
- source badges for Database, Environment, Default, and Not configured;
- contextual feature cards with enabled/disabled, healthy/incomplete, support status, and missing requirements;
- one dirty-state save bar per feature page, Save/Discard, disabled pristine Save, submit progress, and navigation warning;
- compact Discord connection summary with friendly local timestamps;
- a new stylesheet cache token so browsers do not combine revised templates with stale pre-audit CSS;
- reusable responsive grids and access/audit components; below 980px side navigation becomes a drawer and settings navigation becomes horizontally scrollable;
- visible focus outlines, semantic labels/fieldsets, skip link, accessible 403, alerts, empty states, and responsive tables.

Remaining design-system debt is tracked in owner confirmation rather than hidden: older domain pages still use some one-off layout classes, destructive actions do not all have a browser confirmation dialog, and there is no global toast/skeleton component. These are not justification for changing proven operational semantics in this pass.

## Legacy and duplication audit

| Item | Evidence | Decision |
|---|---|---|
| `/settings/features` and the continuous Feature Settings form | Duplicate of feature-owned configuration | Redirect to `/features`; remove the now-unreferenced giant-form template |
| `/settings/users` and `/users` | User list duplicated access management | Redirect into `/settings/access` |
| `/settings/knowledge*`, `/stats*`, `/imports` | Existing documented bookmarks | Keep as server redirects; no navigation clutter |
| `VCXP_MINUTES_PER_PULSE` | Superseded by `VC_XP_PULSE_MINUTES`; compatibility read/migration value | Hidden, preserve temporarily |
| `VCXP_ROLE_REMOVE_DELAY_SECONDS`, `VCXP_DAILY_PULSE_CAP`, `VCXP_WEEKLY_PULSE_CAP` | Runtime behavior no longer uses removal/caps | Hidden compatibility values; no deletion |
| lowercase `bank_allowed_role_ids`, `bank_log_channel_id` | Dashboard JSON compatibility definitions; bank cog uses uppercase `BANK_ALLOWED_ROLE_IDS` only | Hidden; preserve stored data |
| `BANK_LOG_CHANNEL_ID` | Registry/UI only; no bank runtime consumer | Hide from dashboard and flag orphaned; preserve key/data pending owner decision |
| legacy reminder command routes | Explicit `ENABLE_LEGACY_REMINDER_COMMANDS` gate and active fallback roles | Keep, visible only as compatibility control |

## Security review

- Session fixation: `login_user()` clears the session before establishing identity.
- Sessions: signed, HTTP-only via Starlette, 12-hour maximum, SameSite Lax; `DASHBOARD_COOKIE_SECURE=true` is required behind HTTPS.
- Passwords: PBKDF2-SHA256 with per-user salt and 600,000 iterations; plaintext is never stored.
- OAuth: state is random, signed-session-bound, single-use, and constant-time compared. Scopes are `identify guilds.members.read`; the current configured guild member is fetched before admission.
- Authorization: current active user and effective permissions are reloaded from SQLite for every request. Discord sessions fail closed when verification is missing or older than `DASHBOARD_DISCORD_REVERIFY_MINUTES` (default 60), forcing a fresh OAuth login.
- Discord snowflakes remain strings in SQLite, JSON, forms, selectors, and mappings.
- CSRF remains mandatory on every supported mutating form.
- Secrets are not dashboard settings. Audit payload keys containing token/secret/password/API/private-key markers are redacted.
- The audit table has SQLite triggers rejecting update and delete.

## Screenshot evidence

Authenticated screenshots were captured at 1280×720 from isolated baseline and revised dashboard instances on the owner-approved local origin. Each instance used a disposable SQLite database; neither capture read from or wrote to the production database.

- [Overview before](screenshots/dashboard-overview-before.png) and [overview after](screenshots/dashboard-overview-after.png)
- [Settings before](screenshots/dashboard-settings-before.png) and [settings after](screenshots/dashboard-settings-after.png)
- [Feature hub after](screenshots/dashboard-features-after.png)
- [Dashboard Access after](screenshots/dashboard-access-after.png)
- [Discord Connection after](screenshots/dashboard-discord-after.png)

The first comparison pass exposed stale stylesheet caching when the baseline and revised dashboards shared an origin. The stylesheet version token was bumped and the revised screenshots were recaptured, providing a deployment-realistic check that existing browser caches receive the new layout.

## Validation and runtime evidence limits

The repository did not include production service logs or a representative populated production database. Local data was insufficient to label every registered feature as actively used. The feature inventory therefore distinguishes supported registration from usage evidence.

See [feature-inventory.md](feature-inventory.md), [configuration-inventory.md](configuration-inventory.md), [dashboard-information-architecture.md](dashboard-information-architecture.md), [dashboard-rbac.md](dashboard-rbac.md), [dashboard-migration-deployment.md](dashboard-migration-deployment.md), and [dashboard-owner-confirmation.md](dashboard-owner-confirmation.md).
