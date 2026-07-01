"""Fixed, local-only operational helpers for the dashboard."""

from __future__ import annotations

import platform
import re
import shutil
import socket
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from dashboard.db import find_bank_database_path, find_database_path, table_names
from utils.sqlite import configure_sync_connection


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = PROJECT_ROOT / "backups"
SERVICE_NAMES = {
    "bot": "broedenbot",
    "dashboard": "broeden-dashboard",
}
RESTART_COMMANDS = {
    "bot": ("sudo", "systemctl", "restart", "broedenbot"),
    "dashboard": ("sudo", "systemctl", "restart", "broeden-dashboard"),
}
STATUS_COMMANDS = {
    "bot": ("systemctl", "status", "broedenbot", "--no-pager"),
    "dashboard": (
        "systemctl",
        "status",
        "broeden-dashboard",
        "--no-pager",
    ),
}
LOG_COMMANDS = {
    "bot": ("journalctl", "-u", "broedenbot", "-n", "100", "--no-pager"),
    "dashboard": (
        "journalctl",
        "-u",
        "broeden-dashboard",
        "-n",
        "100",
        "--no-pager",
    ),
}
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key|authorization)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
DISCORD_TOKEN_RE = re.compile(
    r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b"
)


def run_fixed_command(
    command: Sequence[str],
    *,
    timeout: float = 10,
    cwd: Path | None = None,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": f"{command[0]} is not installed or not available in PATH.",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": f"{command[0]} timed out after {timeout:g} seconds.",
        }
    except OSError as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": f"{command[0]} could not run: {exc}",
        }
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def sanitize_output(text: str) -> str:
    safe = str(text or "").replace("\x00", "")
    safe = BEARER_RE.sub("Bearer [REDACTED]", safe)
    safe = SECRET_ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", safe)
    safe = DISCORD_TOKEN_RE.sub("[REDACTED DISCORD TOKEN]", safe)
    return safe[-50_000:].strip()


def service_status(service: str) -> dict[str, Any]:
    command = STATUS_COMMANDS[service]
    result = run_fixed_command(command, timeout=8)
    combined = "\n".join(
        part for part in (result["stdout"], result["stderr"]) if part
    )
    normalized = combined.casefold()
    if "could not be found" in normalized or "not-found" in normalized:
        state = "not found"
    elif result["returncode"] is None:
        state = "unavailable"
    elif result["ok"]:
        state = "active"
    elif "inactive" in normalized:
        state = "inactive"
    elif "failed" in normalized:
        state = "failed"
    else:
        state = "unknown"
    return {
        "name": SERVICE_NAMES[service],
        "state": state,
        "detail": sanitize_output(combined) or "No status output was returned.",
    }


def service_logs(service: str) -> dict[str, str]:
    result = run_fixed_command(LOG_COMMANDS[service], timeout=10)
    output = result["stdout"] if result["stdout"] else result["stderr"]
    return {
        "name": SERVICE_NAMES[service],
        "output": sanitize_output(output) or "No recent logs were returned.",
    }


def restart_service(service: str) -> tuple[bool, str]:
    result = run_fixed_command(RESTART_COMMANDS[service], timeout=20)
    if result["ok"]:
        return True, f"{SERVICE_NAMES[service]} restart requested successfully."
    error = sanitize_output(result["stderr"] or result["stdout"])
    if "password" in error.casefold() or "a terminal is required" in error.casefold():
        error = (
            "Passwordless sudo is not configured for this fixed service restart. "
            "See the dashboard sudoers documentation."
        )
    return False, error or f"Could not restart {SERVICE_NAMES[service]}."


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _uptime() -> str:
    try:
        value = Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
        return _format_duration(float(value))
    except (OSError, ValueError, IndexError):
        result = run_fixed_command(("uptime",), timeout=3)
        return sanitize_output(result["stdout"]) if result["ok"] else "Unavailable"


def _memory_status() -> str:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
        total = values["MemTotal"]
        available = values["MemAvailable"]
        used = total - available
        return f"{format_bytes(used)} used / {format_bytes(total)} total"
    except (OSError, ValueError, KeyError):
        return "Unavailable"


def _git_value(*args: str) -> str:
    result = run_fixed_command(("git", *args), timeout=5, cwd=PROJECT_ROOT)
    return sanitize_output(result["stdout"]) if result["ok"] else "Unavailable"


def format_bytes(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def system_status() -> dict[str, str]:
    disk = shutil.disk_usage(PROJECT_ROOT)
    return {
        "hostname": socket.gethostname() or platform.node() or "Unavailable",
        "uptime": _uptime(),
        "disk": (
            f"{format_bytes(disk.used)} used / {format_bytes(disk.total)} total "
            f"({format_bytes(disk.free)} free)"
        ),
        "memory": _memory_status(),
        "python": platform.python_version(),
        "git_commit": _git_value("rev-parse", "--short", "HEAD"),
        "git_branch": _git_value("branch", "--show-current"),
    }


def _quoted_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _database_counts(path: Path, preferred: set[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "size": format_bytes(path.stat().st_size) if path.is_file() else "Missing",
        "tables": {},
        "error": None,
    }
    if not path.is_file():
        return result
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=5,
        )
        configure_sync_connection(connection, readonly=True)
        available = table_names(connection)
        candidates = preferred | {
            name
            for name in available
            if any(marker in name.casefold() for marker in ("bank", "donation", "donor"))
        }
        for table in sorted(candidates & available):
            count = connection.execute(
                f"SELECT COUNT(*) FROM {_quoted_identifier(table)}"
            ).fetchone()[0]
            result["tables"][table] = int(count)
        connection.close()
    except (OSError, sqlite3.Error) as exc:
        result["error"] = str(exc)
    return result


def operations_database_status() -> dict[str, Any]:
    shared_path = find_database_path()
    bank_path = find_bank_database_path()
    shared = _database_counts(
        shared_path,
        {
            "bot_settings",
            "bot_settings_audit",
            "ai_usage_logs",
            "stats_activity_imports",
            "stats_activity_imported_messages",
        },
    )
    shared["bot_settings_exists"] = "bot_settings" in shared["tables"]
    return {
        "shared": shared,
        "bank": _database_counts(
            bank_path,
            {"bank_transactions", "bank_settings"},
        ),
    }


def backup_database(backup_dir: Path | None = None) -> Path:
    source = find_database_path()
    if not source.is_file():
        raise FileNotFoundError(f"Database not found: {source}")
    destination_dir = backup_dir or BACKUP_DIR
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = destination_dir / f"broeden-backup-{timestamp}.sqlite"
    suffix = 1
    while destination.exists():
        destination = destination_dir / (
            f"broeden-backup-{timestamp}-{suffix}.sqlite"
        )
        suffix += 1
    source_connection = sqlite3.connect(
        f"{source.as_uri()}?mode=ro",
        uri=True,
        timeout=30,
    )
    try:
        target_connection = sqlite3.connect(destination, timeout=30)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
    finally:
        source_connection.close()
    return destination
