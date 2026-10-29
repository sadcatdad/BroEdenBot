# Self-hosted migration and recovery

The bot and dashboard share SQLite state. Run one production bot instance and
keep the bot, dashboard, databases, and uploaded assets together on the same
host. The Garden remains at `https://garden.broeden.com`; retain
`dashboard.broeden.com` as its compatibility redirect.

## Preserve a complete recovery set

Preserve the exact deployed source revision, installed package versions,
private service configuration, and these persistent files together:

| Container path | Contents |
| --- | --- |
| `/data/data.db` | Application state, campaigns, claims, schedules, and dashboard authorization |
| `/data/message_context.db` | Message context |
| `/data/staff_context.db` | Staff context |
| `/data/brobank.db` | Bank data |
| `/data/visual-assets/` | Uploaded visual assets |

Keep the existing Discord bot identity, OAuth client credentials and callback,
dashboard session secret, public URL, and secure-cookie settings. The callback
remains `https://garden.broeden.com/auth/discord/callback`.

Recovery archives contain member data and credentials. Keep their directories
private, restrict file permissions, and exclude them from Git and image builds.
Keep an independent copy off the server. Use SQLite's backup API for a running
database; copying only its main file can omit committed WAL data.

## Prepare and validate before interruption

Build the recorded source revision with matching dependency versions. Bind the
dashboard and tunnel metrics ports to localhost. A Cloudflare connector can
share the application's Docker network and route to `http://app:3000` without
opening a public application port on the host.

Set `BROEDEN_SEED_MODE=true` during preparation. Its
`{"status":"seed-mode"}` health response confirms connectivity; it does not
mean the production bot or dashboard is running. Test a separate temporary
hostname before moving the Garden's production records.

Validate an extracted recovery copy offline: all file checksums, all four
SQLite integrity and foreign-key checks, dependency consistency, application
schemas, campaign state, and existing authorization. Validate the dashboard's
runtime migration records and authorization schema; the standalone RBAC CLI's
separate migration ledger may be absent on an otherwise valid runtime database.
Do not create a placeholder ledger or reset grants merely to satisfy that check.

## Capture the final state and hand over

Before pausing production, check open claim windows, in-flight deliveries, and
the next scheduled drop. Choose a gap long enough for export, verification,
startup, and public routing. Claim expiry times remain real timestamps during
an interruption.

Confirm the source platform permits maintenance and recovery operations under
its current plan. An existing running deployment does not prove that a new
deployment will be allowed. If redeployment is blocked, explicitly choose
whether to restore that capability or proceed without an immediate source-host
fallback before retiring the old deployment.

Stop source writers before taking the final coordinated snapshot. Health-only
maintenance is preferred when available. A directly supervised service needs a
separately rehearsed pause procedure: killing one child can terminate the
supervisor and lose access to its volume. The coordinated pause procedure used
for this migration acquires write barriers on all four databases before
stopping every thread in both writers, verifies process identities, releases
the barriers, and includes a bounded recovery watchdog. It verifies the pause
again after snapshot creation and immediately before source retirement.

Transfer and verify the final archive on both the recovery computer and the
destination. Check every restored file against its source hash and run full
integrity and foreign-key checks on all four databases. Preserve the rehearsal
directory separately; do not merge a new snapshot into old rehearsal files.

Confirm the source deployment is stopped before enabling production on the
destination. The activation helper requires a matching verified snapshot and,
for the retirement workflow, recent evidence that Railway removed the old
deployment and has no other active deployment. Future Railway deployments
remain configured for health-only mode.

Start the destination using the production configuration, then verify Discord
readiness, database health, and preserved claim counts before changing public
routing. Configure both Garden hostnames on the new tunnel with service URL
`http://app:3000` and no path restriction. Preserve unrelated DNS records and
Railway verification TXT records. Save the old CNAME targets before replacing
them; an existing CNAME can prevent Cloudflare from adding a tunnel hostname.

Verify public HTTPS health, Garden login and OAuth callback, authorized pages,
the legacy-host redirect, live bot logs, and campaign operation after routing.

## Operate the deployed Compose project

The deployed directory contains `app/`, `state/`, `service.env`, and preparation,
tunnel, and production Compose files. The production override disables seed
mode and enables restart after host reboots. Keep the tunnel credential in a
private file mounted read-only into the connector.

Use all three files for production management:

```bash
cd ~/broedenbot
sudo docker compose -f compose.yaml -f compose.tunnel.yaml -f compose.production.yaml ps
sudo docker compose -f compose.yaml -f compose.tunnel.yaml -f compose.production.yaml logs --since 10m --tail 100 app
```

A project `.env` containing
`COMPOSE_FILE=compose.yaml:compose.tunnel.yaml:compose.production.yaml` selects
the same files for ordinary `docker compose` commands. Without that setting,
the base preparation file alone serves only health checks. Keep preparation
helpers disabled after activation so they cannot accidentally replace the
production configuration with seed mode.

## Recover without losing newer activity

### Updating code from GitHub on the Docker server

The installed `~/broedenbot/deploy-from-git.sh` pulls `main` from the clean
`source/` checkout and runs `scripts/self_hosted_deploy.py`. The Mac shortcut
`bebotdeploy` invokes it over SSH with an interactive password prompt. It uses
the existing runtime configuration; no bot token or member data belongs in Git.

Each candidate is built and dependency/syntax checked without a Discord login
before production stops. All four SQLite databases are copied through SQLite's
backup API after the app is stopped, so committed WAL contents are preserved.
Uploaded assets and private runtime configuration are included in the private
snapshot. Only `app` is recreated; the tunnel, Docker network, and public website
remain running. The generated `compose.git.yaml` is added to the runtime
`COMPOSE_FILE` selection so later normal Compose commands keep the deployed
image and source path.

Success requires the requested image ID, a new Discord-ready log entry, and a
healthy readable dashboard database. The command records the deployed revision
in `SOURCE-COMMIT` and `deployment-result.json`. If startup fails after the new
code begins running, it first saves the newer state before attempting the old
code. It does not copy the pre-deploy snapshot over current databases. A schema
change incompatible with old code may still require manual recovery using the
preserved snapshots and private startup/rollback logs.

`--plan` does not use Docker or stop production. `--check` builds and checks the
candidate without stopping production. The normal deploy refuses open Event
Drop windows and drops due within five minutes. Backups are retained rather
than automatically deleted; the command refuses if recovery space is low.

Before the new host has started, the original paused writers can be resumed
only while their original deployment still exists. After Railway retirement,
recover the new host from the verified archive or restore Railway deployment
entitlement before rebuilding there. Retain source volumes and recovery
archives until operation and off-host backups have been verified.

Once the new host has started, it may contain newer writes even if startup
verification later fails. Stop it and capture its current four databases and
assets together before any reverse transfer. Restore and validate that current
state on the chosen host, then start exactly one bot. Resuming the old frozen
Railway files alone would discard newer activity.
