# Dashboard RBAC migration and deployment

The migration is additive. It adds RBAC/audit tables and guild-verification columns, seeds system definitions, and records dashboard schema version 1. It does not drop or rewrite existing dashboard users, settings, bot data, or compatibility keys.

## Pre-deployment

1. Confirm the current branch/commit and a clean or intentionally reviewed worktree.
2. Stop writes or use `deploy.sh`, which stops bot/dashboard services before migrations.
3. Resolve the effective shared database path with `.venv/bin/python -c 'from utils.settings import settings_database_path; print(settings_database_path())'`.
4. Back up the database with SQLite backup and run `PRAGMA quick_check` on the copy.
5. Preserve the source diff and persistent Visual Content Studio asset directory.

## Migration rehearsal

```bash
.venv/bin/python scripts/migrate_dashboard_rbac.py \
  --database /absolute/path/to/data.db \
  --backup-dir /absolute/path/to/backups

.venv/bin/python scripts/migrate_dashboard_rbac.py \
  --database /absolute/path/to/data.db \
  --validate-only
```

The validator checks SQLite integrity, all RBAC/audit tables, verification columns, append-only triggers, schema version, permissions, and five system roles. It is idempotent.

## Configuration before restart

Required for Discord login:

```dotenv
DASHBOARD_AUTH_MODE=discord
DASHBOARD_PUBLIC_URL=https://garden.broeden.com
DASHBOARD_LEGACY_HOSTS=dashboard.broeden.com
GUILD_ID=123456789012345678
DISCORD_OAUTH_CLIENT_ID=123456789012345678
DISCORD_OAUTH_CLIENT_SECRET=keep_this_only_in_env
DISCORD_OAUTH_REDIRECT_URI=https://garden.broeden.com/auth/discord/callback
DASHBOARD_DISCORD_REVERIFY_MINUTES=60
DASHBOARD_COOKIE_SECURE=true
```

`DASHBOARD_PUBLIC_URL` must be an origin only: scheme plus hostname, without a
path, query string, credentials, or fragment. Requests whose hostname matches
`DASHBOARD_LEGACY_HOSTS` receive a 308 redirect to the same path and query on
the public URL. Keep the preferred Cloudflare edge redirect as the first line
of defense and retain the app redirect as a safe fallback.

`DASHBOARD_DISCORD_ALLOWED_USER_IDS` remains a direct compatibility admission list. `DASHBOARD_DISCORD_ALLOWED_ROLE_IDS` remains a compatibility role list; new role-to-dashboard-role policy should be managed in **Admin Dashboard → Access** after the first owner login.

## Deployment

`deploy.sh` now performs, in order: source/database/assets backup, fast-forward pull, dependency/compile validation, service stop, reminder migration, dashboard RBAC migration plus validation, Visual Studio migration plus validation, service restart, and service status checks.

After restart:

1. Check both systemd services are active and inspect logs.
2. Verify `https://garden.broeden.com/health` before enabling the legacy-host redirect.
3. Log in with the emergency owner account.
4. Open **Admin Dashboard → Access**; confirm Owner, Administrator, Moderator, Party Captain, and Analyst / Viewer roles.
5. Map a low-risk Discord test role to Analyst / Viewer and verify OAuth login/navigation/403 behavior.
6. Test role removal and confirm the next login is denied.
7. Verify Discord Connection status/refresh and Audit Log events.
8. Verify Settings, Features, Operations, Message Studio, Visual Studio, Knowledge, Analytics, Streaks, and Bank for the owner.
9. Confirm `https://dashboard.broeden.com/events?test=redirect` redirects to the same path and query on `garden.broeden.com`.
10. Run `PRAGMA quick_check` again and retain the pre-deploy backup.

Local visual evidence for the revised information architecture is recorded under `docs/screenshots/`; the capture used disposable databases and is not a substitute for the production-role smoke test above.

## Rollback

1. Stop `broedenbot.service` and `broeden-dashboard.service`.
2. Restore the exact `pre-deploy-<timestamp>.sqlite` created before migration using SQLite backup/copy while services are stopped.
3. Restore the matching pre-deploy source commit/branch and Visual Studio asset archive if that deployment changed assets.
4. Run `PRAGMA quick_check` on the restored database.
5. Start both services and verify logs, dashboard login, bot connectivity, and known commands.

Because the migration is additive, rolling application code back without restoring the database is also generally safe: older code ignores the new tables/columns. Restore the database when an exact state rollback or audit-table removal is required. Never drop the new tables manually on the production database.
