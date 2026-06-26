# Internal staff checklists

BroEdenBot checklists are persistent staff task lists. The SQLite backend in
`data.db` is the source of truth; Discord messages are synchronized copies.
Deleting a posted message does not delete its checklist.

## Permission setup

Set the role IDs that may manage checklists:

```dotenv
CHECKLIST_ALLOWED_ROLE_IDS=ROLE_ID_ONE,ROLE_ID_TWO
```

Users with any configured role can use `/checklist`. Users listed in
`BOT_OWNER_USER_IDS` are also allowed. If `CHECKLIST_ALLOWED_ROLE_IDS` is blank,
only configured bot owners can use the feature. Every command, button, modal,
and selector checks permission. Management responses are ephemeral.

The bot also needs channel permissions before it can post or refresh checklist
copies. New posts require **View Channel**, **Send Messages** (or **Send
Messages in Threads** for thread posts), and **Embed Links**. Updating an
existing post requires **View Channel**, **Read Message History**, and **Embed
Links**. If one is missing, `/checklist post` reports the missing permission in
the private response.

## Commands

- `/checklist create <name> [description] [post_channel]` creates a backend
  checklist, opens its private management panel, and optionally posts it.
- `/checklist view [checklist]` opens the management panel. Omit the input to
  choose an active checklist from a selector.
- `/checklist list [status]` lists active, archived, or deleted backend records
  with progress, creator, update time, and active-post count.
- `/checklist post <checklist> <channel> [update_existing]` posts a synchronized
  copy. With `update_existing:true`, an existing active post for that checklist
  in the channel is edited when one exists.
- `/checklist delete [checklist]` asks for confirmation, soft-deletes the
  checklist and its items, then tries to delete every active posted copy.
- `/checklist rename <checklist> <new_name> [description]` updates the name and,
  when supplied, the description.
- `/checklist archive <checklist> [delete_posts]` removes a checklist from the
  active list. Posts remain and show archived status unless deletion is chosen.
- `/checklist restore <checklist>` returns an archived checklist to active.
- `/checklist refresh <checklist>` refreshes every active posted copy and
  reattaches its persistent controls. Use this as the recovery path if Discord
  ever displays stale or nonresponsive buttons after a restart or deployment.
- `/checklist export <checklist>` privately exports all item rows, including
  deleted items, as CSV.

Checklist inputs support autocomplete by ID or name. IDs can also be entered
directly.

Commands that read or synchronize checklist state acknowledge the interaction
before doing database or Discord message work. If Discord still shows a stale
button after a deployment or permission change, use `/checklist refresh` to
reattach controls to active posts.

## Management panel

The private panel provides buttons to add an item, toggle completion, soft-delete
an item, rename the checklist, post it to a channel, archive or restore it, and
refresh the panel. Any authorized staff member can use a panel they can access;
controls are not tied only to the person who opened it.

Posted checklist messages display the same controls. The buttons are visible to
people who can see the channel, but every click checks
`CHECKLIST_ALLOWED_ROLE_IDS` and `BOT_OWNER_USER_IDS` again. Unauthorized users
receive a private denial, while authorized selectors, modals, and confirmations
are ephemeral. Posted controls are persistent across bot restarts. If Discord
retains an older component state, `/checklist refresh` re-edits all active posts
with newly attached controls without changing checklist data.

Completing an item records who completed it and when. Reopening it clears those
completion fields. Item deletion is soft deletion and active positions are
compacted afterward.

## Posting and synchronization

A checklist can be posted in multiple channels. After an item is added,
toggled, or deleted, or the checklist is renamed, archived, or restored, the
bot edits every active posted copy and keeps its controls attached. Checklist
text uses no allowed mentions, so item text cannot ping users, roles, or
everyone.

If a post is manually deleted, a raw-message listener marks its database post
record as `missing`. Sync also detects missing or forbidden messages, marks
them missing, and continues updating other copies. Use `/checklist post` to
create a fresh copy. The backend checklist remains manageable throughout.

Deleting a checklist soft-deletes the checklist and active items. The bot then
attempts to delete all active Discord copies and marks each post `deleted` or
`missing` based on the result. `/checklist list status:deleted` still shows the
backend record.

## Storage

The feature creates three tables in the shared `data.db`:

- `checklists` stores checklist identity, description, state, creator, and
  deletion metadata.
- `checklist_items` stores ordered item state plus creation, completion, and
  deletion metadata.
- `checklist_posts` stores each Discord channel/message copy and sync state.

Rows are retained when checklists or items are deleted. There is no purge
command.

## Limits

- Discord selectors allow at most 25 options. Selectors show the first 25 and
  autocomplete commands can find other checklists.
- Posted embeds show a safe subset of very long checklists and report how many
  items are hidden. `/checklist view` remains the management path.
- Management panels time out, but the checklist does not; run `/checklist view`
  to open a new panel.
- Discord permissions still control whether the bot can send, edit, fetch, or
  delete messages in a channel.
