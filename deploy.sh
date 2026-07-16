#!/bin/bash
set -Eeuo pipefail

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="$BOT_DIR/backups/deploy-snapshots"
SERVICES=(broedenbot.service broeden-dashboard.service)
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SERVICES_STOPPED=false

show_failure() {
  local exit_code=$?
  trap - ERR
  echo
  echo "ERROR: Deployment stopped safely (exit $exit_code)."
  echo "Recovery files: $BACKUP_DIR/pre-deploy-$TIMESTAMP.*"
  if [[ "$SERVICES_STOPPED" == true ]]; then
    sudo systemctl start "${SERVICES[@]}" || true
  fi
  git status --short || true
  sudo systemctl --no-pager --full status "${SERVICES[@]}" || true
  exit "$exit_code"
}
trap show_failure ERR

cd "$BOT_DIR"
mkdir -p "$BACKUP_DIR"

DATABASE_PATH="$($BOT_DIR/.venv/bin/python -c 'from utils.settings import settings_database_path; print(settings_database_path())')"
VISUAL_ASSET_DIR="$($BOT_DIR/.venv/bin/python -c 'from utils.visual_studio.storage import visual_asset_directory; print(visual_asset_directory())')"
if [[ ! -f "$DATABASE_PATH" ]]; then
  echo "Database not found: $DATABASE_PATH" >&2
  exit 2
fi

echo "Saving source and SQLite recovery snapshots..."
git status --short > "$BACKUP_DIR/pre-deploy-$TIMESTAMP.status"
git diff --binary > "$BACKUP_DIR/pre-deploy-$TIMESTAMP.patch"
git diff --cached --binary >> "$BACKUP_DIR/pre-deploy-$TIMESTAMP.patch"
sqlite3 "$DATABASE_PATH" ".backup '$BACKUP_DIR/pre-deploy-$TIMESTAMP.sqlite'"
sqlite3 "$BACKUP_DIR/pre-deploy-$TIMESTAMP.sqlite" "PRAGMA quick_check;"
if [[ -d "$VISUAL_ASSET_DIR" ]]; then
  tar -czf "$BACKUP_DIR/pre-deploy-$TIMESTAMP.visual-assets.tar.gz" \
    -C "$(dirname "$VISUAL_ASSET_DIR")" "$(basename "$VISUAL_ASSET_DIR")"
fi

echo "Fetching and fast-forwarding the current branch..."
git fetch --prune origin
git pull --ff-only

echo "Installing and validating dependencies..."
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
PYTHONPYCACHEPREFIX=/tmp/broeden-deploy-pycache \
  .venv/bin/python -m compileall -q main.py cogs utils dashboard scripts

echo "Stopping services for database migrations..."
sudo systemctl stop "${SERVICES[@]}"
SERVICES_STOPPED=true
.venv/bin/python scripts/migrate_reminders.py --database "$DATABASE_PATH"
.venv/bin/python scripts/migrate_reminders.py --database "$DATABASE_PATH" --validate-only
.venv/bin/python scripts/migrate_visual_content_studio.py \
  --database "$DATABASE_PATH" \
  --asset-dir "$VISUAL_ASSET_DIR" \
  --backup-dir "$BACKUP_DIR"
.venv/bin/python scripts/migrate_visual_content_studio.py \
  --database "$DATABASE_PATH" \
  --asset-dir "$VISUAL_ASSET_DIR" \
  --validate-only

echo "Restarting BroEdenBot and its dashboard..."
sudo systemctl reset-failed "${SERVICES[@]}"
sudo systemctl start "${SERVICES[@]}"
SERVICES_STOPPED=false
sudo systemctl is-active --quiet "${SERVICES[@]}"

echo
echo "Deployment completed successfully."
git --no-pager log -1 --oneline
sudo systemctl --no-pager --full status "${SERVICES[@]}"
