#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${BROEDEN_RUNTIME_ROOT:-$HOME/broedenbot}"
SOURCE="$ROOT/source"
exec 9>"$ROOT/.deploy.lock"
flock -n 9 || { echo 'A bot deployment is already running.' >&2; exit 1; }
[[ "$(git -C "$SOURCE" branch --show-current)" == main ]] || { echo 'The source checkout must be on main.' >&2; exit 1; }
[[ -z "$(git -C "$SOURCE" status --porcelain)" ]] || { echo 'The server source has local changes; resolve them before deployment.' >&2; exit 1; }
git -C "$SOURCE" pull --ff-only origin main
exec python3 "$SOURCE/scripts/self_hosted_deploy.py" --root "$ROOT" --source "$SOURCE" "$@"
