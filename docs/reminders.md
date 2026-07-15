# Reminder system

BroEdenBot uses one canonical reminder system for personal reminders and public
subscribable events. The Discord cog, delivery worker, migration command, and
dashboard all use `utils/reminder_service.py`; scheduled work is never held only
in memory.

## Commands and permissions

- `/remind personal [destination] [who]` creates a one-time or recurring
  personal reminder. `REMINDER_PERSONAL_ALLOWED_ROLE_IDS` controls access;
  leaving it blank allows every member to create a DM reminder for themselves.
  `destination` changes delivery to a server channel and `who` changes the
  target; both retain the configured staff check.
- `/remind event` uses `REMINDER_EVENT_ALLOWED_ROLE_IDS`. When it is blank, the
  command falls back to `REMINDER_ALLOWED_ROLE_IDS` and the existing configured
  staff/admin roles.
  The setup defaults to the current channel, can select a text, voice, or Stage
  destination, and always shows a parsed confirmation before publishing.
- `/remind manage [status] [reminder_type] [recurrence]` uses
  `REMINDER_MANAGE_ALLOWED_ROLE_IDS`; blank allows every member to manage their
  own reminders. `REMINDER_MANAGE_ALL_ROLE_IDS` controls guild-wide management
  and falls back to the legacy staff rules when blank.
  Controls edit content/time/default
  timings, change destination, inspect subscribers and occurrences, reschedule
  one/this-and-future/all occurrences, cancel one occurrence or the series,
  duplicate, and archive completed/cancelled records.
- `/remind subscriptions` and new **Remind Me** subscriptions use
  `REMINDER_SUBSCRIPTIONS_ALLOWED_ROLE_IDS`; blank allows every member. The
  command shows only the caller's subscriptions and can change timing, restore
  defaults, open the event channel, or unsubscribe.
- `/remind help` explains the member-facing workflow.
- `/timezone` is available to all members because it controls how their reminder
  input is interpreted. Timestamps are stored as UTC and displayed with Discord
  timestamps.

`/reminder add`, `/reminder manage`, and `/remind subscribe` are legacy
transition routes. They call the canonical flow and show a move notice. Set
`ENABLE_LEGACY_REMINDER_COMMANDS=false` after the compatibility period.

All five role settings are available under **Settings → Features → Reminders**
with the dashboard's Discord multi-role picker. Bot owners and Discord
administrators always retain access. Existing subscriptions remain cancellable
from their private controls even if the member later loses a subscription role.

## Event and subscription behavior

The public card uses the event name as its title and shows when, where, host,
default timings, recurrence, and an eventually consistent subscriber count.
**Remind Me** is labeled; the destination button is a direct Discord channel
link. Subscriber-count edits are coalesced for three seconds to reduce message
edit rate.

Default event timing syntax is a comma-separated subset of `start`, `15m`,
`1h`, `1d`, or another whole-minute/hour/day offset. Duplicate values are
removed. Limits are five timings and 30 days before the event. A member cannot
schedule an advance timing that already passed before they subscribed.

The confirmation DM is a single **Reminder Set** card with event, time,
destination, host, and timings. If the DM fails, the subscription becomes
`delivery_unavailable`, future delivery rows are cancelled, and the ephemeral
response tells the member to enable server DMs and select **Remind Me** again.
Permanent privacy failures are not repeatedly retried. No alternate public
subscriber-ping mode is enabled because it would create avoidable spam and
mention-abuse risk.

## Recurrence

Personal reminders and events support daily, weekly, monthly, and `every N
days`. Add `for 10` or `until 2035-12-01` to set an end condition. A series
generates stable occurrence records. Explicit occurrence counts are capped at
60. A blank end condition is treated as the 60-occurrence system limit, not an
unbounded row generator. Weekly/monthly generation uses the input
timezone so the local wall-clock time survives daylight-saving changes; UTC is
stored afterward. Monthly dates clamp to the last valid day of shorter months.

Event subscriptions apply to the series. Timing changes rebuild only unsent
delivery rows and preserve sent/claimed identities. Creators can reschedule one
occurrence, the selected and future occurrences, or all upcoming occurrences;
they can cancel one occurrence or the complete series. When an occurrence
finishes, the series card advances to the next occurrence.

## Persistent schema and migration

Canonical tables:

- `reminder_items` — personal/event configuration, recurrence, public message,
  creator/target, interpretation timezone, and lifecycle status.
- `reminder_occurrences` — stable indexed occurrences for a one-time reminder or
  series.
- `reminder_subscriptions` — one unique subscription per event/user, DM delivery
  mode, defaults/custom timing, and delivery availability.
- `reminder_deliveries` — one unique occurrence/recipient/trigger record with
  due time, claim lease, attempts, error category, and terminal timestamps.
- `reminder_audit` — content-free action metadata for create, edit, subscribe,
  unsubscribe, delivery, cancellation, occurrence, dashboard, and archive work.
- `reminder_dashboard_actions` — admin requests queued by the dashboard and
  claimed by the bot service.
- `reminder_migrations` — idempotent migration run report.

The legacy `reminders`, `reminder_subscription_posts`, and
`reminder_subscribers` tables are retained unchanged. Startup and
`scripts/migrate_reminders.py` map them with unique legacy source/ID keys.
Pending rows become upcoming, sent/completed rows remain completed, failed rows
remain failed, and active subscribers are deduplicated. Malformed dates are
logged with the legacy record ID and excluded from scheduling; they are counted
in the migration report. Completed records never receive new delivery rows.

Run a consistent SQLite backup before migration. Do not copy only `data.db`
while WAL writes are active; use SQLite `.backup`, the dashboard's **Back up
database** action, or stop both services first.

## Scheduler and failure handling

The bot polls every 30 seconds. Each delivery has a stable unique key. Claiming
uses a conditional database update from `pending`/`retry` to `claimed`, so a
second loop cannot claim the same row. Claims have a ten-minute lease; startup
reconciliation returns expired claims to `retry`. The delivery state is checked
again immediately before Discord I/O so cancellation can win a race with a
claim.

Temporary Discord HTTP errors retry with exponential delays (30, 60, 120, then
240 seconds, capped at 15 minutes) and stop after four attempts. Forbidden DMs,
missing users, and deleted channels are permanent failures. The default missed
delivery grace is 120 minutes; older unsent work becomes `stale`. Configure it
with `REMINDER_DELIVERY_GRACE_MINUTES` from 1 through 1440.

Public event views are restored with the stored Discord message ID during cog
load. Component IDs use the `broeden:remind:` namespace and resolve the database
record before acting. Deleted, expired, cancelled, wrong-guild, or wrong-user
controls return private messages rather than internal details.

## Dashboard

Open **Operations → Manage reminders**. Authenticated viewers can inspect the
overview, filters, reminder configuration, occurrences, delivery health, and
audit history. Subscriber identities and delivery error details are masked from
viewer accounts. Owner/admin accounts can queue edits, duplicates,
cancellations, eligible temporary-failure retries, and archival. Actions carry
the selected guild ID and are rejected if the reminder belongs to another
guild. The bot processes them through the canonical service within one worker
interval; dashboard route handlers do not update reminder rows directly.

## Manual Discord checklist

Use a test guild and two accounts. Keep production DMs and channels out of this
test.

1. Run `/timezone` and save the test creator's real IANA timezone.
2. Run `/remind personal`; create a reminder five minutes ahead and confirm the
   parsed preview.
3. Run `/remind event`; use the current channel, set `15m, start`, and publish.
4. From account two, select **Remind Me** twice. Confirm one subscription exists,
   one confirmation DM was sent, and the public count becomes one.
5. Use `/remind subscriptions` to select `1h, 15m, start`, then restore defaults.
6. Restart `broedenbot`; confirm the public buttons still work.
7. Create another five-minute reminder, restart before it is due, and confirm it
   still sends exactly once.
8. Disable server DMs for account two. Subscribe to a different event and verify
   the bot reports failure instead of success.
9. Re-enable DMs and select **Remind Me** again; verify delivery becomes active.
10. Edit the event time. Confirm its public card, delivery due times, and account
    two's meaningful-change DM update.
11. Change the destination and repeat the update checks.
12. Cancel the event. Confirm the card is disabled, subscribers receive a
    cancellation DM, and every unsent delivery becomes cancelled.
13. Create daily, weekly, monthly, and custom-interval series. Verify occurrence
    dates in `/remind manage` and reschedule one/this-and-future/all.
14. Cancel one occurrence and confirm the rest of its series remains active.
15. Open **Operations → Manage reminders**. Test filters, detail, duplicate,
    edit, cancel, retry on an eligible temporary failure, and archive.
16. Log in as a viewer and confirm write actions and subscriber identities are
    unavailable.
17. Attempt to manage another member's reminder as a normal Discord member and
    confirm it is denied; repeat as configured staff and confirm guild-only
    management.
18. Inspect `journalctl -u broedenbot` for creation, claim, sent, retry,
    permanent-failure, cancellation, public-update, and migration entries.

## Production deployment and rollback

Live read-only inspection on 2026-07-14 reported both units using
`/home/sadcatdad/BroEdenBot` and `data.db` there. It also reported that path on
`/dev/mmcblk0p2`, while the 2 TB `T-FORCE` SSD (`/dev/sdb2`, label `SSD`) was
present but not mounted. This conflicts with the expected SSD migration. Do not
invent a different path or edit systemd as part of the reminder deploy. Resolve
the mount/path discrepancy first if SSD hosting is required, then substitute
the path returned by the two `systemctl show` commands below.

Inspect current paths:

```bash
systemctl show broedenbot -p WorkingDirectory -p FragmentPath -p ExecStart
systemctl show broeden-dashboard -p WorkingDirectory -p FragmentPath -p ExecStart
findmnt -T /home/sadcatdad/BroEdenBot
```

At the currently configured checkout:

```bash
cd /home/sadcatdad/BroEdenBot
mkdir -p backups
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
sqlite3 data.db ".backup 'backups/data-before-reminders-${STAMP}.sqlite'"
sqlite3 "backups/data-before-reminders-${STAMP}.sqlite" "PRAGMA quick_check;"
git fetch origin
git switch feature/remind-system-overhaul
git pull --ff-only origin feature/remind-system-overhaul
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/migrate_reminders.py --database data.db
.venv/bin/python scripts/migrate_reminders.py --database data.db --validate-only
.venv/bin/python -m unittest tests.test_reminder tests.test_reminder_dashboard tests.test_reminder_dashboard_routes -v
sudo systemctl restart broedenbot
sudo systemctl restart broeden-dashboard
systemctl status broedenbot --no-pager
systemctl status broeden-dashboard --no-pager
journalctl -u broedenbot -n 150 --no-pager
journalctl -u broeden-dashboard -n 100 --no-pager
```

The bot's startup runs the same idempotent migration before syncing application
commands. In the logs, confirm the reminder migration report, persistent-view
restore count, successful cog load, and successful command sync. Smoke-test
`/remind help`, a five-minute personal reminder, one event subscription, and the
dashboard reminder list.

Rollback (replace the backup filename and previous commit explicitly):

```bash
cd /home/sadcatdad/BroEdenBot
sudo systemctl stop broeden-dashboard
sudo systemctl stop broedenbot
cp -a data.db "backups/data-failed-reminders-$(date -u +%Y%m%dT%H%M%SZ).sqlite"
cp -a backups/data-before-reminders-YYYYMMDDTHHMMSSZ.sqlite data.db
git switch --detach PREVIOUS_KNOWN_GOOD_COMMIT
.venv/bin/python -m pip install -r requirements.txt
sqlite3 -readonly data.db "PRAGMA quick_check;"
sudo systemctl start broedenbot
sudo systemctl start broeden-dashboard
systemctl status broedenbot --no-pager
systemctl status broeden-dashboard --no-pager
```

Restoring the pre-migration backup intentionally discards reminder activity
created after cutover. Keep the failed database copy for later reconciliation.
Do not merge or deploy this feature branch automatically.

## Troubleshooting

- **DM unavailable:** the subscription shows `delivery_unavailable`. Enable DMs
  from server members and select **Remind Me** again. Dashboard retry is only
  for exhausted temporary failures, not permanent privacy failures.
- **Missed reminder:** inspect `reminder_deliveries.error_category`. `stale`
  means downtime exceeded the grace window; `retry` has a future
  `next_attempt_at_utc`; `claimed` should have a future lease or be recovered at
  startup.
- **Button no longer works:** confirm the event is upcoming, its public message
  still exists, the reminder cog logged a restored view, and the custom ID begins
  `broeden:remind:`. Restart the bot after fixing a missing cog load.
- **Deleted channel/user:** these are permanent failures. Edit the reminder's
  destination or create a new subscription after the member is accessible.
- **Migration warning:** use the logged legacy table/record ID to inspect the
  retained source row. Correct it only after another backup, then rerun the
  migration command; unique legacy keys prevent duplicate canonical rows.

## Current limits

- Delivery mode is DM or a staff-authorized personal channel. Public event
  subscriber pings are deliberately disabled.
- A series is capped at 60 generated occurrences. Occurrence selectors show the
  next 25 Discord-select options at a time; broader series edits use
  `future`/`all` scope.
- Discord link buttons open channels directly, so link clicks are not available
  as interaction analytics. Subscription, delivery, cancellation, and failure
  health are tracked.
- Event images are not accepted in the Discord creation flow. The existing bot
  has no reminder-specific safe upload/retention path, so the compact branded
  embed is used without storing arbitrary remote media.
- The system uses one SQLite writer shared by the bot and queued dashboard
  actions. It is designed for the current single-bot Raspberry Pi deployment,
  not multiple simultaneously active bot processes.
