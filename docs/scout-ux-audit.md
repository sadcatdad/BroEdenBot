# Scout workflow and UX audit

This pass reviews command discovery, activity creation, follow-ups, escalation,
member reports, report outcomes, deleted-message evidence, check-ins, permissions,
notifications, and the Scout dashboard. All previews use an isolated local database;
no test notifications are sent to Discord.

| Friction found | Implemented behavior |
| --- | --- |
| An empty record argument gave Scouts nothing to choose | `/scout log follow_up_to`, `/scout escalate follow_up_to`, and `/scout edit record` autocomplete recent readable entries with a short number, type, and summary. Editing offers owned active manual entries only. |
| Escalation appeared to require an existing record | Omit `follow_up_to` to describe a new concern. Dashboard **Ask for help** does the same. Record pages preselect the entry when starting a follow-up. |
| `evidence_link` did not explain the input | Renamed to `message_link`, with **Copy Message Link** instructions. A message context action prefills member/channel/link. Forms accept supporting links/details as optional evidence. |
| Deleted text was captured but never attached | Use the rolling buffer or cached pre-deletion snapshot; reconcile a complete, uniquely matching deletion batch with the confirmed Scout audit record. Display the result explicitly as correlated context. |
| Notifications were oversized and technical | Compact purple bullet cards, readable member/channel mentions, short record references, and a hidden delivery receipt. Existing recent cards are edited in place on updated bot startup. |
| The dashboard mixed data browsing and many full forms | Dedicated activity/help pages; compact list rows, filters, short references, an overview, responsive record pages, and secondary details in disclosure panels. |
| Settings required awkward multiple-selection controls | Named searchable checkbox pickers for eligibility, capture, exclusions, and notification roles. Readiness includes message-buffer count and last observation. |
| Check-ins required copying user IDs | Dashboard uses named searchable Scout checkboxes followed by the existing preview/confirmation step. Membership is rechecked before sending. |
| Form wording and extra questions were hardcoded | **Scouts → Forms** controls activity, help, member report, outcome, and default check-in questions. Discord and dashboard use the same validated definitions. |
| Failed submissions could lose context | Private draft reopen controls and dashboard validation retain entered answers. A failed linked Discord draft offers a standalone-entry path. Original submission IDs prevent duplicate saves. |

## Configurable questions

Settings managers can edit question labels and hints, enable optional questions,
require answers, and add questions within five available slots per Discord form.
Activity has four default fields and one extra slot; help, report, and outcome
forms each have one core field and four extra slots. Core questions remain required.
Labels support 45 characters, hints 100, and answers 4,000 (evidence 2,000).
Check-ins support one to three questions, 200 characters each.

Saved custom answers retain their original labels. Activity labels are snapshotted
with the record; renamed questions do not rewrite previous records. Custom reporter
answers join the protected reporter explanation, never the shared activity payload.
No form definition can grant capabilities or change Discord moderation permissions.

## Deletion matching and limits

Discord confirms an audit actor, but its deletion audit entries do not provide
exact message IDs. The implementation treats message association as an inference.
Individual deletions require matching author and channel; bulk deletions require
the same channel. Both require a narrow time window, exact observed count delta,
and a single audit candidate across Scout and non-Scout actors. Partial or competing
batches stay unattached. A settled gateway event is consumed once. Incrementing
audit entries append context and refresh the existing card rather than posting a
second activity record.

Only confirmed human Scout actors produce automatic activity/cards. Self-deletions,
non-Scout moderators, bots, voice self-movement, automatic timeout expiry, and
unverifiable actors do not create Scout activity. Temporary raw observations expire
after five minutes; finalized captured evidence and revisions remain retained.
Capture requires enabled automatic logging, explicit channel/category scope, bot
visibility, and message-content access. Text never observed cannot be recovered;
attachment URLs may expire. Configuration changes do not backfill deleted content.

References: [Discord audit logs](https://docs.discord.com/developers/resources/audit-log)
and [discord.py raw deletion events](https://discordpy.readthedocs.io/en/stable/api.html#discord.RawMessageDeleteEvent).

## Access and delivery checks

Record choices are scoped to the current server and permitted record types, with
no private check-ins or protected reporter details. Live role removal revokes
access. CSRF checks, ownership, revision conflict checks, and no-store responses
remain enforced. Requesting check-ins does not grant access to others' answers.
Reports and check-in answers stay out of shared Discord cards, history, search,
and AI inputs unless an explicit private-answer capability permits an appropriate
source. Reporters' identities/explanations are always excluded from AI.

Activity saves and their outbox jobs are transactional. Channel destination is
explained before Discord submission; receipts distinguish saved, pending, sent,
failed, and uncertain delivery. The fast outbox worker runs separately from audit
polling; ambiguous sends are reconciled against durable receipts before retries.

## Validation and rollout

Storage/adapter/route tests cover custom required questions, label snapshots,
protected extra report answers, ownership and server-scoped selectors, configured
Discord modals, failed-draft recovery, cached deletion snapshots, exact count
matching, competing audits, and count increments after temporary observations are
pruned. Browser checks exercise Scout-only navigation, searchable follow-ups,
activity creation, form configuration, permission denial, and desktop/mobile layouts.

Deploy the bot and dashboard together using the existing **self-hosted** deployment
workflow. Restarting the updated bot synchronizes the revised slash-command options
and enables the form definitions in Discord. Settings flags outdated renderer/form
capabilities. Check **BOT DEPLOY VERIFIED**, destination readiness, buffer activity,
and a real Scout submission after rollout. Local tests do not establish production
health or Discord command propagation.
