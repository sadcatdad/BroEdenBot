"""Secret-safe preflight checks adapted for the Bro Eden repository."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOKEN_RE = re.compile(
    r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b"
)
SENSITIVE_NAMES = {".env", ".env.local"}
SENSITIVE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".csv", ".zip"}
FEATURE_FILES = (
    "cogs/disboard_bumps.py",
    "cogs/reminder.py",
    "cogs/streaks.py",
    "cogs/stats.py",
    "cogs/leaderboards.py",
)
FEATURE_SETTINGS = (
    "DISBOARD_BOT_USER_ID",
    "BUMP_REWARD_ROLE_ID",
    "BUMP_SUCCESS_EMBED_ID",
    "BUMP_PING_ROLE_ID",
    "BUMP_REMINDER_MESSAGE",
    "BUMP_REMINDER_EMBED_ID",
    "BUMP_LEADERBOARD_CHANNEL_ID",
    "BUMP_POINTS_PER_SUCCESS",
    "REMINDER_ALLOWED_ROLE_IDS",
    "REMINDER_TIMEZONE",
    "STREAK_TIMEZONE",
    "STREAK_MIN_WORDS",
    "STREAK_DUPLICATE_LOOKBACK_DAYS",
    "STREAK_EXCLUDED_CHANNEL_IDS",
    "STREAK_LEADERBOARD_CHANNEL_ID",
    "STREAK_RESTORE_ENABLED",
    "STREAK_RESTORE_GAP_MINUTES",
    "STREAK_RESTORE_MAX_DAYS",
    "STREAK_RESTORE_MAX_MESSAGES",
    "STATS_ALLOWED_ROLE_IDS",
    "LEADERBOARD_RESET_ROLE_IDS",
)


def _tracked_files() -> List[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=str(PROJECT_ROOT),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return [
        PROJECT_ROOT / item.decode("utf-8")
        for item in result.stdout.split(b"\0")
        if item
    ]


def safety_check_payload() -> Dict[str, Any]:
    tracked = _tracked_files()
    sensitive = []
    token_lines = []
    for path in tracked:
        relative = str(path.relative_to(PROJECT_ROOT))
        if path.name in SENSITIVE_NAMES or path.suffix.casefold() in SENSITIVE_SUFFIXES:
            sensitive.append(relative)
        if path.suffix.casefold() not in {
            ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".example"
        }:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(lines, start=1):
            if TOKEN_RE.search(line):
                token_lines.append({"file": relative, "line": line_number})

    settings_text = (PROJECT_ROOT / "utils" / "settings.py").read_text(
        encoding="utf-8"
    )
    missing_files = [
        relative for relative in FEATURE_FILES if not (PROJECT_ROOT / relative).is_file()
    ]
    missing_settings = [
        key for key in FEATURE_SETTINGS if f'"{key}"' not in settings_text
    ]
    safe = not sensitive and not token_lines and not missing_files and not missing_settings
    return {
        "safe_to_commit": safe,
        "tracked_sensitive_files": sensitive,
        "token_shaped_lines": token_lines,
        "missing_feature_files": missing_files,
        "missing_setting_definitions": missing_settings,
        "notes": [
            "Ignored local .env and SQLite files are expected and are not inspected.",
            "Never paste secret values into issue reports or chat messages.",
        ],
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m riffbot.setup")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("safety-check")
    args = parser.parse_args(argv)
    if args.command == "safety-check":
        payload = safety_check_payload()
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["safe_to_commit"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
