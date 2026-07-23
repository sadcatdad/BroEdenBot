#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${BROEDEN_DATA_DIR:-/data}"
PORT="${PORT:-3000}"

export DATABASE_PATH="${DATABASE_PATH:-$DATA_DIR/data.db}"
export MESSAGE_CONTEXT_DB_PATH="${MESSAGE_CONTEXT_DB_PATH:-$DATA_DIR/message_context.db}"
export STAFF_CONTEXT_DB_PATH="${STAFF_CONTEXT_DB_PATH:-$DATA_DIR/staff_context.db}"
export BANK_DATABASE_PATH="${BANK_DATABASE_PATH:-$DATA_DIR/brobank.db}"
export VISUAL_ASSET_DIR="${VISUAL_ASSET_DIR:-$DATA_DIR/visual-assets}"
export DASHBOARD_HOST="0.0.0.0"
export DASHBOARD_PORT="$PORT"

if [[ "${BROEDEN_SEED_MODE:-false}" == "true" ]]; then
  echo "Railway seed mode is active; serving health checks only."
  exec python scripts/railway_seed_health.py
fi

mkdir -p "$VISUAL_ASSET_DIR" "$DATA_DIR/backups/migrations"
cd "$APP_DIR"

database_paths=(
  "$DATABASE_PATH"
  "$MESSAGE_CONTEXT_DB_PATH"
  "$STAFF_CONTEXT_DB_PATH"
  "$BANK_DATABASE_PATH"
)

for database in "${database_paths[@]}"; do
  if [[ ! -f "$database" ]]; then
    echo "Required Railway database is missing: $database" >&2
    exit 2
  fi
  result="$(sqlite3 -readonly "$database" 'PRAGMA quick_check(1);')"
  if [[ "$result" != "ok" ]]; then
    echo "SQLite quick check failed for $database: $result" >&2
    exit 3
  fi
done

python scripts/migrate_reminders.py --database "$DATABASE_PATH"
python scripts/migrate_reminders.py --database "$DATABASE_PATH" --validate-only
python scripts/migrate_events.py --database "$DATABASE_PATH"
python scripts/migrate_events.py --database "$DATABASE_PATH" --validate-only

if ! python scripts/migrate_visual_content_studio.py \
  --database "$DATABASE_PATH" \
  --asset-dir "$VISUAL_ASSET_DIR" \
  --validate-only; then
  python scripts/migrate_visual_content_studio.py \
    --database "$DATABASE_PATH" \
    --asset-dir "$VISUAL_ASSET_DIR" \
    --backup-dir "$DATA_DIR/backups/migrations"
fi

python scripts/migrate_visual_content_studio.py \
  --database "$DATABASE_PATH" \
  --asset-dir "$VISUAL_ASSET_DIR" \
  --validate-only

python main.py &
bot_pid=$!
python -m uvicorn dashboard.app:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --proxy-headers \
  --forwarded-allow-ips="*" &
dashboard_pid=$!

shutdown() {
  kill -TERM "$bot_pid" "$dashboard_pid" 2>/dev/null || true
  wait "$bot_pid" "$dashboard_pid" 2>/dev/null || true
}
trap shutdown TERM INT EXIT

set +e
wait -n "$bot_pid" "$dashboard_pid"
status=$?
set -e

echo "A Railway service process exited with status $status; stopping its companion." >&2
if [[ "$status" -eq 0 ]]; then
  exit 1
fi
exit "$status"
