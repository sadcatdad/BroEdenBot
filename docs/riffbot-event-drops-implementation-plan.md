# RiffBot Implementation Plan: Event Drops, Rare Variants, Manual Drops, and Role Pings

## Objective

Implement a reusable **Event Drops** campaign system in RiffBot and its admin dashboard. Periodically post a themed collectible in one selected Discord channel. Every eligible member may claim that drop once before it expires. Claims award campaign points, and the message is deleted after expiration while its history remains available.

Include all four capabilities in this plan:

1. Configurable Event Drops campaigns and leaderboards.
2. Weighted Drop Variants with different rewards, appearances, and images.
3. Separate **Send Drop Now** and **Rare Drop Now** dashboard controls.
4. An optional campaign role ping on every drop.

This is a portable implementation brief based on the completed BroEdenBot feature. RiffBot's repository and deployment have not been inspected; adapt names and implementation details to its existing architecture. Do not hardcode a particular event, currency, theme, or rarity system.

## 1. Inspect RiffBot before changing code

Identify and reuse the existing:

- Discord feature/module structure, command registration, and persistent interactions.
- Scheduler, background workers, startup recovery, and feature switches.
- Database engine, connection management, transactions, and migrations.
- Dashboard routing, templates/components, navigation, and form conventions.
- Authentication, administrative permissions, CSRF protection, and guild isolation.
- Channel/role metadata synchronization and selectors.
- Embed builders, image validation/storage, and asset delivery.
- Timezone handling, logs, audit records, tests, and deployment process.

Check command names before adding `/event` or `/eventdrop`; preserve existing commands. Reuse existing infrastructure rather than introducing another scheduler, role system, or Discord client in the dashboard.

First report a short repository-specific implementation plan. Then implement the complete feature, migrations, tests, and documentation.

## 2. Non-negotiable compatibility requirements

- Preserve existing campaigns, schedules, channel selections, eligibility rules, variants, images, active drops, claim history, and scores.
- Use additive, versioned, repeatable migrations. Never reset or recreate populated campaign tables to add this feature.
- Existing campaigns remain in standard mode until variants are explicitly enabled.
- Existing campaigns and previously created drops default to **No role ping**.
- Configuration changes do not rewrite existing drop rewards, images, appearance, or notification settings.
- Retain historical references when an asset is removed or a previously used variant is deleted.
- Keep existing participant CSV columns compatible; add separate detailed exports.
- Never test with production Discord channels or mutate production campaign data during development.

## 3. Campaign configuration and lifecycle

Place the feature under **Events → Event Drops**, adapting to RiffBot's current navigation.

Support multiple current/historical campaign records with guild-scoped access. Do not hardcode a single campaign into the service.

### Configuration

| Area | Settings |
| --- | --- |
| Identity | Campaign name; collectible singular/plural names; collectible emoji |
| Standard reward | Whole-number points per claim, from 1 to 1,000,000 |
| Appearance | Embed title, description, color, optional HTTPS thumbnail |
| Claim button | Label, Unicode/custom Discord emoji, and button style |
| Artwork | Optional campaign image pool; choose one image for each drop |
| Scheduling | Fixed interval or random interval range; optional start/end dates |
| Claim window | Configurable duration, default 10 minutes |
| Destinations | Explicit allowlist of individual Discord text channels |
| Channel variety | Avoid the last X channels, default 3; relax oldest exclusions when necessary |
| Eligibility | Optional eligible roles and excluded roles |
| Limits | Optional campaign maximum drops and maximum campaign points per member |
| Variants | Standard mode or enabled Drop Variants |
| Notifications | Optional single role to ping with each drop |

Default scheduling: fixed every 60 minutes, or a random interval between 45 and 90 minutes. Store timestamps in UTC and display them in the configured server timezone.

### Lifecycle

- **Draft:** editable, not sending automatically.
- **Scheduled:** waiting for a future start; first automatic drop occurs at that start time.
- **Active:** automatic scheduling enabled. A manual start waits one interval before its first automatic drop.
- **Paused:** no future automatic drops; cancel unsent queued work, but allow already-posted drops to finish. Manual sends remain available within campaign dates and limits.
- **Completed:** permanently ended; close live claims and clean up messages while preserving history and scores.

Allow configuration edits only in draft or paused status. Pause an active/scheduled campaign before editing. Resuming starts a fresh interval. End-of-campaign time closes claims just like manually ending a campaign.

Require an explicit confirmation such as typing `END` before permanently ending a campaign. Only unused drafts can be deleted. Duplicating a campaign creates a new draft with its configuration, roles, channels, artwork, variants, and notification setting, but no drops, claims, scores, or scheduled dates.

## 4. Storage and shared service

Use a shared domain service for campaign management, reservations, claims, totals, variants, and migrations. The dashboard queues work; the existing bot process delivers Discord messages.

Adapt these conceptual entities to RiffBot's database conventions:

| Entity | Main responsibility |
| --- | --- |
| Campaign | Guild, lifecycle, configuration, scheduling state, limits, variant mode, ping role |
| Campaign channels | Explicit destination allowlist |
| Campaign role rules | Eligible/excluded roles |
| Assets | Validated image data or durable storage references; ownership and campaign-pool membership |
| Drop variants | Campaign-owned weighted reward and appearance configurations |
| Variant assets | Associations between variants and reusable assets |
| Drops | Delivery state, channel/message IDs, occurrence/manual request identity, immutable reward/variant/image/ping data, expiration |
| Drop snapshots | Resolved embed/button appearance, collectible names, visibility flags, and claim-window duration |
| Claims | Campaign/drop/member IDs, actual awarded points, and claim timestamp |
| Member display cache | Display names for reporting, including members who have left |
| Worker state | Heartbeat and recovery information |
| Schema versions | Completed migration versions |

Required database guarantees:

- Unique claim per `(drop_id, user_id)`.
- Unique automatic occurrence per `(campaign_id, scheduled_at)`.
- Unique manual request key to make repeated submissions idempotent.
- At most one non-deleted default variant per campaign, plus service validation that it is enabled when variants are on.
- Foreign keys and indexes for campaign history, scheduled work, expiration, variant ownership, and score aggregation.

Claim rows are the source of truth for scores. Store the actual awarded amount on each claim and the selected reward on each drop; never calculate historical rewards from today's variant configuration.

Use transactions appropriate to RiffBot's engine. For SQLite, use dedicated write connections, a busy timeout, and `BEGIN IMMEDIATE` rather than sharing an open transaction with unrelated bot features.

## 5. Scheduling and delivery reliability

Implement an approximately 15-second worker, or integrate equivalent behavior into the existing job system.

1. Atomically reserve each due occurrence and advance its scheduling state.
2. Select the variant once, select the image once, and save all resolved drop data before delivery.
3. Claim pending work atomically so only one worker sends it.
4. Select one eligible allowlisted channel, honoring recent-channel avoidance.
5. Set expiration on the first send attempt; retain it across channel fallback.
6. Send the embed, persistent claim button, optional image, and optional role mention.
7. Store the confirmed Discord message/channel and posting time.

Recheck View Channel, Send Messages, Embed Links, and Read Message History permissions. Require Attach Files for image drops. Categories are organizational labels only: newly created channels must not join a campaign automatically.

Use a stable message nonce with deduplication enforcement where the Discord library supports it, plus a unique drop marker for reconciliation. Definitive permission/not-found rejections may fall back to another allowed channel. Ambiguous timeouts or network errors must not trigger blind resends: retain uncertain delivery state and reconcile against message history. This intentionally prioritizes avoiding duplicate posts and pings over automatically retrying every uncertain send.

On restart, register persistent interactions before starting the worker. Skip overdue automatic occurrences rather than replaying a backlog. Mark old unsent automatic reservations as missed and schedule a future occurrence. The reference implementation skips occurrences more than 90 seconds late and bounds reconciliation scans to 500 messages per pass, retaining a cursor for later continuation.

Count pending and uncertain reservations against campaign drop limits. Failed or missed attempts do not consume the limit. An individual failure must not terminate the worker.

## 6. Claims, expiration, and Discord commands

Each eligible human may claim each live drop once. There is no global winner limit or cap on distinct claimers.

Inside one transaction:

1. Verify the guild and reject bot accounts.
2. Check live drop/campaign state and expiration after obtaining the transaction lock.
3. Apply eligible-role rules; any eligible role suffices. Excluded roles take precedence.
4. Reject duplicate claims.
5. Enforce the member's campaign point cap, including concurrent claims across different drops.
6. Insert the claim with the drop's frozen reward and return the updated total.

Require room for the full reward: if only 3 points remain under a cap, a 5-point drop cannot be claimed. Do not partially award it. Eligibility and campaign limits use current settings; rewards and presentation use the saved drop snapshot.

Use ephemeral confirmations and errors. A success response identifies the collectible/variant, points earned, and updated campaign total. Duplicate clicks must never award twice.

At expiration, reject claims immediately even if message deletion is delayed. Normally delete the message on the next worker pass. Treat already-deleted messages as successfully cleaned up; retry other deletion failures without removing historical data. The bot does not need Manage Messages to delete its own posts.

Suggested commands, subject to existing RiffBot naming:

- `/event score [campaign_id]`: ephemeral personal total and rank.
- `/event leaderboard [campaign_id]`: public top 10, with mentions suppressed.
- `/eventdrop status|drop|pause|resume [campaign_id]`: authorized administrator controls.

Commands without a campaign ID resolve an appropriate campaign in the current guild. Public leaderboards combine all variant rewards into one campaign total.

## 7. Drop Variants and weighted selection

Enabling variants on a campaign without configured variants creates an enabled Default Variant using the current standard reward and inherited appearance. Keep exactly one enabled default while variant mode is on; do not allow disabling/deleting the last enabled variant or current default without a valid replacement or switching back to standard mode.

Each variant contains:

- Name and optional freeform rarity label.
- Relative selection weight and whole-number reward.
- Enabled/default flags and display order.
- Optional overrides for emoji, embed title/body/color/thumbnail, and button label/emoji/style.
- Image mode and asset associations.
- Reward and rarity visibility controls.
- Creation/update timestamps and deletion state.

Create a reusable `select_drop_variant(...)` service independent of scheduling and channel selection. Consider only enabled, non-deleted variants.

```text
probability(variant) = variant.weight / sum(enabled variant weights)
```

| Example variant | Weight | Reward | Probability |
| --- | ---: | ---: | ---: |
| Normal Fish | 90 | 1 | 90% |
| Golden Fish | 8 | 5 | 8% |
| Legendary Salmon | 2 | 10 | 2% |

Weights need not total 100: weights 50/5/1 produce approximately 89.3%/8.9%/1.8%. Draw independently per drop; do not promise a fixed sequence. Rarity labels and colors have no selection semantics.

Reject non-finite weights, negative weights, and zero weights on enabled variants. Reference limits are 30 non-deleted variants per campaign and finite weights up to 1,000,000,000.

### Appearance and image inheritance

- Unset overrides inherit campaign values.
- Explicit empty values may clear optional fields such as description, thumbnail, or emoji.
- New variants show reward value by default; rarity display defaults off.
- Standard campaigns retain their original appearance unless explicitly configured otherwise.
- Support image modes: inherit campaign pool, one specific image, variant image pool, and no image.
- Variant-only uploads must not enter the campaign image pool accidentally.

Resolve inheritance at reservation time. Freeze variant identity/name/rarity, reward, image, appearance, collectible names, claim duration, and visibility flags. Retrying delivery or restarting must not reroll or reinterpret a drop.

Duplicating a variant copies settings and asset associations under a new ID without becoming the default. Disabling preserves it for reuse. Delete unused variants normally; soft-delete previously used variants to preserve history. Switching to standard mode retains variant configuration.

## 8. Send Drop Now and Rare Drop Now

Provide two clearly separate controls on active/paused campaign detail pages.

### Send Drop Now

- Standard mode sends a standard campaign drop.
- Variant mode selects using enabled variant weights.
- Allow a random eligible channel or a specific allowlisted channel.
- Keep the automatic schedule unchanged.

### Rare Drop Now

- Require an explicit selection of an enabled campaign variant.
- Bypass the weighted draw and send exactly that variant.
- Include the variant name, optional rarity, and reward in the selector.
- Do not infer rarity from a name, color, reward, or weight threshold; the administrator chooses which special variant to force.
- Reject missing, disabled, deleted, or foreign-campaign variant IDs on the server.
- Allow random or explicitly selected eligible destinations.
- Keep normal claim rules, limits, expiration, notification settings, and scheduling behavior.
- Record that the selection was forced in drop history.

Give the forms distinct idempotency keys so one action cannot accidentally reuse the other's request. Repeated submission of the same request creates only one drop. If variants are off, show a link to configure them instead of allowing a misleading random fallback.

## 9. Optional role pings

Add **Edit campaign → Drop notifications → Role to ping with each drop** with a default **No role ping** option.

- Choose one role from the campaign's guild using existing synchronized metadata.
- Apply it to automatic, manual, and forced rare drops.
- Keep notification roles independent of claim eligibility.
- Validate role IDs and reject another guild's roles and the guild's `@everyone` role.
- Snapshot the selected role ID when reserving the drop.
- Place the role mention in message content above the embed.
- Configure allowed mentions to permit only the selected role, with users, everyone/here, other roles, and reply mentions disabled.
- The role must be mentionable, or the bot needs Mention Everyone permission in that destination channel. Explain this beside the selector.
- If the role was deleted, send the drop without the ping and log the missing role.
- Preserve an unavailable saved role in the editor until the administrator clears or replaces it.
- If an older submitted form omits the new field, preserve the campaign's saved setting; an explicitly blank field clears it.

Do not add pings retroactively to existing Discord messages or migrated queued drops.

## 10. Dashboard, assets, reporting, and authorization

Build campaign lists, creation/editing, lifecycle controls, and detail pages using RiffBot's established components.

Include:

- A searchable channel picker grouped by category, with explicit selections.
- Role eligibility and notification selectors.
- Campaign and variant previews, including unsaved appearance/image changes.
- Variant cards showing enabled/default status, weight, normalized probability, and reward.
- Add/edit/duplicate/enable/disable/set-default/delete variant actions.
- Clear configuration-lock explanations.
- Worker heartbeat/readiness, last/next drop, live messages, pending/uncertain delivery, and errors.
- Paginated drop history with variant, rarity, reward, selection method, claim count, and status.
- Campaign leaderboard, per-drop claimers, and participant breakdowns by variant.
- Variant statistics: delivered drops, claims, awarded points, and average claims including zero-claim deliveries.

Group historical statistics by recorded variant identity/name/rarity, so later renaming cannot rewrite past presentation. Retain former members' IDs and scores even when their names are unavailable.

Preserve the existing participant CSV: member ID, display name, points, claim count, and rank. Add separate detailed claims and drops exports with campaign/drop/variant IDs, stored names/rarity, channel, reward, UTC timestamps, selection method, and relevant status/member fields. Escape spreadsheet-formula-leading values safely.

For image parity, allow up to 10 JPEG/PNG/WebP images per pool, at most 8 MiB each. Validate actual image content, strip metadata, and normalize to the existing safe image format. The reference uses 1600×900 WebP. Reuse RiffBot's image infrastructure and durable storage; dashboard and bot must both be able to read the chosen asset. Preserve assets referenced by historical drops.

Protect all management, results, asset, and export routes with the existing administrative permission system, using an `event_drops.manage`-equivalent permission. Require CSRF protection for mutations, enforce guild/campaign ownership on every object lookup, and audit administrative actions. Do not send one audit-channel message per member claim.

Use external scripts compatible with the dashboard's content security policy. Make forms usable on mobile and contain wide tables without causing page overflow.

## 11. Migration and implementation order

Implement in reviewable stages, adapting version numbers to RiffBot's migration history:

1. **Core storage and service:** campaigns, channels, roles, assets, drops, claims, totals, lifecycle, transactional reservations.
2. **Discord delivery:** scheduling, persistent claim interactions, expiration, cleanup, recovery, commands.
3. **Dashboard:** campaign editor, previews, manual sends, history, results, and protected exports.
4. **Variants:** variant/asset-association/snapshot storage, weighted selection, editor, reports, and forced manual sends.
5. **Role pings:** optional campaign role and immutable per-drop role ID, defaulting to blank.
6. **Verification and documentation:** complete the acceptance tests, migration checks, live smoke test, and operator guide.

For an existing Event Drops installation, mirror the reference's additive upgrades:

- Variant upgrade adds variant tables, asset associations, snapshot records, and variant/asset-pool columns. Preserve old rewards, assets, and claims. Backfill legacy drops as Standard Drop.
- If the old system never recorded appearance, backfill the best-known campaign appearance and document the limitation; do not pretend to recover unknowable historical values or edit old Discord messages.
- Role-ping upgrade adds blank fields to campaigns and drops without updating other values.

Make migration application atomic and repeatable. Provide backup and validate-only operations covering integrity, expected columns/version, foreign keys, and required snapshots.

## 12. Required tests and acceptance criteria

### Core behavior

- Multiple members can claim the same live drop; each gets the full reward once.
- Concurrent duplicate claims produce one award.
- Concurrent claims across drops cannot exceed a member's cap.
- Bots, wrong guilds, excluded roles, expired drops, and completed campaigns cannot claim.
- Cleanup delays cannot extend eligibility; cleanup tolerates manually deleted messages.
- Fixed/random schedules, future starts/ends, pause/resume, limits, channel avoidance, and manual idempotency behave correctly.
- Competing workers cannot double-reserve/send; restart recovery does not replay missed drops.
- Definitive send fallback works; uncertain sends reconcile without blind duplicate delivery.
- Persistent claim buttons still work after restarting RiffBot.

### Variants and manual controls

- Deterministic boundary tests verify weighted selection with non-100 totals, disabled variants, and one enabled variant.
- Invalid weights/default states are rejected.
- One claim each on rewards 1, 5, and 10 yields a 16-point campaign total.
- Variant selection/image choice occurs once per drop.
- Existing drops retain their saved data after edits, disabling, deletion, or restart.
- Image inheritance, single image, variant pools, and no-image behavior remain isolated and ownership-checked.
- Rare Drop Now requires a valid explicit choice, bypasses randomness, records forced selection, and leaves the automatic schedule unchanged.
- Variant statistics, participant details, and CSVs agree with stored claims, including zero-claim deliveries.

### Role pings and preservation

- Standard and variant campaigns default to no ping after upgrading.
- Configured pings apply to automatic, manual, and forced rare drops.
- Only the chosen role is allowed to mention; everyone/here and user mentions remain suppressed.
- Editing the role does not alter queued drops; deleted roles do not block delivery.
- Old forms and missing metadata do not erase saved notification settings.
- Upgrade a populated pre-feature database containing campaigns, variants, images, live/queued drops, claims, and unrelated data. Compare all existing fields before/after and prove only the intended new fields/version entries differ.
- Run migration twice to prove repeatability. Verify old live claims and pending delivery still function.

### UI and regression checks

- Verify permissions, CSRF, guild isolation, and malicious/foreign object IDs.
- Verify desktop/mobile forms, live previews, upload previews, CSP, and table scrolling.
- Run RiffBot's relevant and full regression suites plus its existing lint/type/build checks.
- Use temporary databases and mocked Discord delivery in automated tests. Do not claim that mocked tests prove real guild permissions.

## 13. Deployment and handoff

1. Update RiffBot's README and operator documentation in the same implementation pass.
2. Use its existing deployment process; do not introduce new services or dependencies unless its architecture requires them.
3. Stop affected bot/dashboard processes during the upgrade, create a database backup, apply migrations, and run validation.
4. Restart both on matching code and confirm worker readiness and metadata synchronization.
5. In a designated test channel, verify standard/rare drops, two different claimers, duplicate rejection, 1+5+10 scoring, a restart during a live drop, expiration, and deletion.
6. Select a test notification role and verify its mention permissions and actual ping behavior. Turn notifications off and verify subsequent drops are silent.
7. Compare existing campaign configuration and scores before opening public use. Retain the backup and document rollback using matching application/database versions.
8. Remove temporary preview/debug files and report exactly what was deployed versus only tested locally.

The final implementation report should cover: **Implemented, Architecture, Database, Selection Logic, Discord, Dashboard, Backward Compatibility, Testing, and Deployment Notes**. Identify any remaining live checks honestly. Deliver working functionality rather than stopping at this plan.
