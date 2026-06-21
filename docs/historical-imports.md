# Historical Discord Imports

DiscordChatExporter exports are local-only data files. Place new JSON or CSV
exports in:

```text
imports/discord_history/
```

Raw exports can be very large and may contain private server history. Never
commit them to GitHub.

## What the importer stores

The importer stores activity metadata only:

- Message ID for deduplication
- Timestamp
- Channel ID and name
- User ID and name
- Hourly message counts

It does not store message content, attachments, embeds, stickers, or reactions.

## Import workflow

1. Export channels with DiscordChatExporter CLI as JSON.
2. Transfer new exports from the Mac to the Raspberry Pi:

   ```bash
   rsync -avh --progress ~/Documents/BroEdenBot/imports/discord_history/ sadcatdad@raspberrypi.local:/home/sadcatdad/BroEdenBot/imports/discord_history/
   ```

3. Connect to the Pi and activate the project environment:

   ```bash
   ssh sadcatdad@raspberrypi.local
   cd ~/BroEdenBot
   source .venv/bin/activate
   ```

4. Preview the import:

   ```bash
   python scripts/import_discord_history.py --folder imports/discord_history --guild-id 1278253523619807233 --dry-run
   ```

5. Run the real import:

   ```bash
   python scripts/import_discord_history.py --folder imports/discord_history --guild-id 1278253523619807233
   ```

6. Verify the imported activity in Discord:

   ```text
   /stats activity importinfo
   /stats activity overview period:all time source:imported
   /stats activity channels period:all time source:imported
   ```

Re-running imports is safe because message IDs are deduplicated.

## Archive completed exports

Archiving is opt-in. `--archive-completed` moves clean files that imported at
least one new message after processing finishes:

```bash
python scripts/import_discord_history.py --folder imports/discord_history --guild-id 1278253523619807233 --archive-completed
```

Add `--archive-duplicates` to also archive clean files whose valid messages
were all already imported:

```bash
python scripts/import_discord_history.py --folder imports/discord_history --guild-id 1278253523619807233 --archive-completed --archive-duplicates
```

The default destination is `imports/discord_history/archive/`. Use
`--archive-folder PATH` to choose another destination. Existing filenames are
preserved; if the same name already exists, the importer adds a timestamp.

Failed or incomplete files are never archived automatically. They remain in
the active folder for repair or re-export. Dry runs never move files and print
`Would archive completed file` when a file would qualify.

Recursive folder imports skip directories named:

- `archive`
- `archived`
- `broken_exports`
- `repaired_from_pi`

An explicit `--file` path can still process a file inside one of those folders.

## Troubleshooting

- If a giant JSON export is incomplete, repair it or re-export the channel in
  smaller date chunks.
- A malformed file can have committed 5,000-message batches before its final
  parse failure. Re-running remains safe because those message IDs are deduped.
- If the Pi is killed while reading a huge JSON file, install current
  requirements and confirm the importer is using streaming `ijson` parsing.
- Keep failed files visible in the active folder until they are repaired or
  replaced.

## Alias shortcuts

These examples use the current project paths and guild ID.

On the Mac:

```bash
alias bedsync='rsync -avh --progress ~/Documents/BroEdenBot/imports/discord_history/ sadcatdad@raspberrypi.local:/home/sadcatdad/BroEdenBot/imports/discord_history/'
alias bedssh='ssh sadcatdad@raspberrypi.local'
```

On the Pi:

```bash
alias bedimportdry='cd ~/BroEdenBot && source .venv/bin/activate && python scripts/import_discord_history.py --folder imports/discord_history --guild-id 1278253523619807233 --dry-run --archive-completed --archive-duplicates'
alias bedimport='cd ~/BroEdenBot && source .venv/bin/activate && python scripts/import_discord_history.py --folder imports/discord_history --guild-id 1278253523619807233 --archive-completed --archive-duplicates'
alias beddeploy='cd ~/BroEdenBot && ./deploy.sh'
alias bedrestart='sudo systemctl restart broedenbot'
alias bedlogs='journalctl -u broedenbot -f'
```

Confirm the systemd service name before using `bedrestart` or `bedlogs`; replace
`broedenbot` if the Pi uses a different unit name. Reload aliases with:

```bash
source ~/.bashrc
```

The normal shortcut workflow is:

```bash
bedimportdry
bedimport
```
