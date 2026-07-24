# Dashboard RBAC and authentication

## Identity and admission

The password-authenticated bootstrap account remains the emergency owner path. Its session is cleared and regenerated at login; only a salted PBKDF2 hash is stored.

Discord OAuth is the primary non-owner identity path:

1. Request `identify guilds.members.read` with a random, signed-session, single-use state value.
2. Exchange the code without storing the access token.
3. Fetch `/users/@me` and `/users/@me/guilds/{GUILD_ID}/member`.
4. Reject missing, pending, or mismatched membership.
5. Preserve snowflakes as strings and store only the user ID, guild ID, role-ID snapshot, display data, source, and verification timestamp.
6. Admit a user through an explicit user allowlist, compatibility allowed-role list, or database-backed Discord role mapping.
7. Replace Discord-derived role assignments on login. A user admitted through role mapping is rejected when their qualifying role disappears.

Requests reload the active user and permissions from SQLite. A disabled/deleted user loses access immediately. Discord verification expires after `DASHBOARD_DISCORD_REVERIFY_MINUTES` (60 by default; clamped to 5–1440), at which point the session is cleared and fresh OAuth is required.

## Capability model

Permissions are stable strings grouped into Monitor, Community, Operations, Finance, Content, and System. View and manage capabilities are separate. High-risk examples include `bot.restart`, `access.manage`, `settings.manage`, `discord_metadata.refresh`, and `audit_log.view`. Feature pages also use feature-domain view keys such as `bumps.view`, `voice.view`, `staff_tools.view`, and existing domain capabilities such as `analytics.view`.

Multiple roles combine additively. A per-user explicit deny removes an inherited capability; an explicit allow adds it. Owner denies are rejected.

For Discord users admitted through a database-backed role mapping, mapped
assignments are authoritative. The compatibility `viewer` value stored on older
user rows is not unioned into effective permissions, so a Verified Member does
not gain Overview or Analytics accidentally.

## System roles

| Role | Default intent |
|---|---|
| Owner | Every current capability; final active owner cannot be removed, disabled, or denied |
| Administrator | Every current capability by default, including access management for roles below Administrator |
| Moderator | Dashboard/analytics plus selected staff, knowledge, voice, reminder, and member-event views; no infrastructure writes |
| Party Captain | Overview, feature hub, event view/subscribe/create/edit-own only |
| Verified Member | Member-facing Garden access: My BROfile, BRO Directory, private Events schedule, and personal DM subscription controls |
| Analyst / Viewer | Overview, bot-status summary, read-only analytics, and the member BROfile/Events surfaces |

Custom roles store a name, description, and selected capabilities. Owners can
configure Administrator and all lower system/custom roles. Administrators can
configure or create roles below Administrator, but cannot edit Owner,
Administrator, or delegate `access.manage`. Owner remains an immutable recovery
authority. System role names/descriptions stay canonical, while configured
capability sets persist across restarts; defaults and required compatibility
changes use one-time migration records instead of reseeding permissions.

## Discord role mapping

`dashboard_discord_role_mappings` maps one or more live Discord role IDs to a dashboard role. Saving a mapping set replaces the mappings for that dashboard role. Multiple matching mappings yield additive dashboard permissions. `dashboard_user_role_assignments.source` identifies `legacy`, `direct`, or `discord`; Discord-derived assignments retain the source role ID.

An owner can also add direct dashboard roles and per-user allow/deny/inherit overrides. The UI shows each effective permission and access source. Mapping changes do not impersonate Discord: login remains the authoritative membership refresh.

Users with member permissions and `dashboard.view` can switch between a fixed
**Member View** and **Dashboard** control in the sidebar. Member View shows only
the working My BROfile, BRO Directory, and Events destinations available to
that role. `brofiles.view` permits published-profile browsing,
`brofiles.edit` permits self-owned profile changes, and `brofiles.manage`
protects the separate Dashboard **BROfile Management** space: searchable
15-member pages, staff visibility holds, typed-confirmation deletion, private
upload-thread configuration, and role-badge administration. Staff visibility
holds are separate from a member's own directory preference. The switch changes
navigation presentation only; server-side capabilities remain unchanged.

## Enforcement

- `DashboardPermissionMiddleware` resolves a capability before route handlers run.
- Dynamic feature routes check the registry capability again.
- Existing CSRF and domain validators still run for authorized mutations.
- Template context contains only server-computed capabilities; navigation/actions use `can()`.
- Unauthorized page routes render 403 without page data. Unauthorized APIs return JSON 403.
- No role, permission, or user claim supplied by a form or browser is trusted as the caller’s authority.
- Event edits and cancellation re-check stored Discord/dashboard ownership in the handler; `events.edit_own` alone never authorizes another Captain's event.

## Storage

The additive schema contains:

- `dashboard_permissions`
- `dashboard_roles`
- `dashboard_role_permissions`
- `dashboard_user_role_assignments`
- `dashboard_discord_role_mappings`
- `dashboard_user_permission_overrides`
- `dashboard_audit_log`
- `dashboard_rbac_migrations`
- dashboard membership-verification columns on `dashboard_users`

The append-only audit log records actor, action, target, timestamp, redacted before/after JSON, success/error, and correlation ID. SQLite triggers reject update and delete. Semantic events exist for login, denied access, configuration/access changes, Discord refresh, service restart, database backup, and visual publication; the middleware also records mutating route outcomes.

## Operational cautions

- Configure `DASHBOARD_COOKIE_SECURE=true` for HTTPS.
- Keep the emergency owner password and outer access gate until Discord login/mappings are proven in production.
- Do not map a Discord role to Owner. Assign owner access directly and retain at least one tested recovery account.
- Run the versioned migration and validation before service restart. See [dashboard-migration-deployment.md](dashboard-migration-deployment.md).
