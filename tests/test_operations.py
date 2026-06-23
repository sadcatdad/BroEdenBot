import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from dashboard.app import app
from dashboard.operations import (
    LOG_COMMANDS,
    RESTART_COMMANDS,
    STATUS_COMMANDS,
    backup_database,
    restart_service,
    sanitize_output,
    service_logs,
    service_status,
    system_status,
)
from utils.settings import initialize_settings_from_env


class OperationsHelperTests(unittest.TestCase):
    def test_restart_services_use_only_fixed_commands(self):
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch("dashboard.operations.subprocess.run", return_value=completed) as run:
            self.assertTrue(restart_service("bot")[0])
            self.assertTrue(restart_service("dashboard")[0])
        commands = [tuple(call.args[0]) for call in run.call_args_list]
        self.assertEqual(
            commands,
            [RESTART_COMMANDS["bot"], RESTART_COMMANDS["dashboard"]],
        )
        with self.assertRaises(KeyError):
            restart_service("broedenbot; touch /tmp/nope")

    def test_missing_systemctl_and_journalctl_are_graceful(self):
        with patch(
            "dashboard.operations.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            status_result = service_status("bot")
            log_result = service_logs("dashboard")
        self.assertEqual(status_result["state"], "unavailable")
        self.assertIn("not installed", status_result["detail"])
        self.assertIn("not installed", log_result["output"])

    def test_missing_service_is_reported_as_not_found(self):
        completed = subprocess.CompletedProcess(
            [],
            4,
            stdout="",
            stderr="Unit broedenbot.service could not be found.",
        )
        with patch("dashboard.operations.subprocess.run", return_value=completed):
            result = service_status("bot")
        self.assertEqual(result["state"], "not found")

    def test_restart_password_error_is_helpful(self):
        completed = subprocess.CompletedProcess(
            [],
            1,
            stdout="",
            stderr="sudo: a terminal is required to read the password",
        )
        with patch("dashboard.operations.subprocess.run", return_value=completed):
            ok, message = restart_service("bot")
        self.assertFalse(ok)
        self.assertIn("Passwordless sudo", message)

    def test_log_output_redacts_obvious_secrets(self):
        output = sanitize_output(
            "DISCORD_TOKEN=abc.def.abcdefghijklmnopqrstuvwxyz\n"
            "API_KEY=super-secret\nAuthorization: Bearer token-value"
        )
        self.assertNotIn("super-secret", output)
        self.assertNotIn("token-value", output)
        self.assertIn("[REDACTED]", output)

    def test_fixed_status_and_log_commands(self):
        completed = subprocess.CompletedProcess([], 0, stdout="active", stderr="")
        with patch("dashboard.operations.subprocess.run", return_value=completed) as run:
            service_status("bot")
            service_logs("dashboard")
        commands = [tuple(call.args[0]) for call in run.call_args_list]
        self.assertEqual(
            commands,
            [STATUS_COMMANDS["bot"], LOG_COMMANDS["dashboard"]],
        )

    def test_backup_creates_timestamped_sqlite_copy(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database = root / "data.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE sample (value TEXT)")
            connection.execute("INSERT INTO sample VALUES ('copied')")
            connection.commit()
            connection.close()
            with patch.dict(os.environ, {"DATABASE_PATH": str(database)}):
                backup = backup_database(root / "backups")
            self.assertRegex(
                backup.name,
                r"^broeden-backup-\d{8}-\d{6}(?:-\d+)?\.sqlite$",
            )
            copied = sqlite3.connect(backup)
            value = copied.execute("SELECT value FROM sample").fetchone()[0]
            copied.close()
            self.assertEqual(value, "copied")

    def test_system_status_handles_missing_git_metadata(self):
        with patch("dashboard.operations._git_value", return_value="Unavailable"):
            result = system_status()
        self.assertEqual(result["git_commit"], "Unavailable")
        self.assertEqual(result["git_branch"], "Unavailable")
        self.assertIn("python", result)


class OperationsRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.database = root / "data.db"
        self.backups = root / "backups"
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(self.database),
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "admin",
                "DASHBOARD_PASSWORD": "test-password",
                "DASHBOARD_SECRET_KEY": "test-session-signing-key",
            },
            clear=False,
        )
        self.environment.start()
        initialize_settings_from_env()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        self.temporary_directory.cleanup()

    def login(self):
        page = self.client.get("/login")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        self.client.post(
            "/login",
            data={
                "username": "admin",
                "password": "test-password",
                "csrf": token,
            },
        )

    def csrf(self):
        page = self.client.get("/operations")
        return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    def test_operations_page_requires_auth(self):
        response = self.client.get("/operations", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "http://testserver/login")

    def test_restart_routes_require_auth_and_csrf(self):
        for path in (
            "/operations/restart-bot",
            "/operations/restart-dashboard",
            "/operations/backup-database",
        ):
            response = self.client.post(path, data={"csrf": "bad"}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
        self.login()
        for path in (
            "/operations/restart-bot",
            "/operations/restart-dashboard",
            "/operations/backup-database",
        ):
            response = self.client.post(path, data={"csrf": "bad"})
            self.assertEqual(response.status_code, 400)

    def test_restart_routes_ignore_user_command_input(self):
        self.login()
        token = self.csrf()
        with patch(
            "dashboard.app.restart_service",
            return_value=(True, "Restart requested."),
        ) as restart:
            response = self.client.post(
                "/operations/restart-bot",
                data={
                    "csrf": token,
                    "service": "evil; touch /tmp/nope",
                    "command": "whoami",
                },
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        restart.assert_called_once_with("bot")

    def test_authenticated_backup_route_creates_backup(self):
        self.login()
        token = self.csrf()
        with patch("dashboard.operations.BACKUP_DIR", self.backups):
            response = self.client.post(
                "/operations/backup-database",
                data={"csrf": token},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        backups = list(self.backups.glob("broeden-backup-*.sqlite"))
        self.assertEqual(len(backups), 1)

    def test_operations_page_renders_without_system_tools_or_git(self):
        self.login()
        with (
            patch(
                "dashboard.app.service_status",
                side_effect=[
                    {"name": "broedenbot", "state": "unavailable", "detail": "missing"},
                    {
                        "name": "broeden-dashboard",
                        "state": "not found",
                        "detail": "missing",
                    },
                ],
            ),
            patch(
                "dashboard.app.service_logs",
                side_effect=[
                    {"name": "broedenbot", "output": "journalctl unavailable"},
                    {
                        "name": "broeden-dashboard",
                        "output": "journalctl unavailable",
                    },
                ],
            ),
            patch(
                "dashboard.app.system_status",
                return_value={
                    "hostname": "test-host",
                    "uptime": "Unavailable",
                    "disk": "Unavailable",
                    "memory": "Unavailable",
                    "python": "3.x",
                    "git_commit": "Unavailable",
                    "git_branch": "Unavailable",
                },
            ),
        ):
            response = self.client.get("/operations")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Bot Operations", response.text)
        self.assertIn("Unavailable", response.text)
        self.assertNotIn("DISCORD_TOKEN", response.text)


if __name__ == "__main__":
    unittest.main()
