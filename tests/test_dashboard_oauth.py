import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard.app import app
from dashboard.auth import login_user
from dashboard.oauth import DiscordOAuthError
from dashboard.users import (
    authenticate_password,
    default_discord_role,
    initialize_dashboard_users,
    list_dashboard_users,
)
from utils.settings import initialize_settings_from_env


class DashboardOAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "data.db"
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(self.database),
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "owner",
                "DASHBOARD_PASSWORD": "owner-password",
                "DASHBOARD_SECRET_KEY": "oauth-test-session-key",
                "DASHBOARD_AUTH_MODE": "discord",
                "DISCORD_OAUTH_CLIENT_ID": "123456789012345678",
                "DISCORD_OAUTH_CLIENT_SECRET": "never-render-this-secret",
                "DISCORD_OAUTH_REDIRECT_URI": (
                    "https://garden.broeden.com/auth/discord/callback"
                ),
                "GUILD_ID": "999999999999999999",
                "DASHBOARD_DISCORD_ALLOWED_USER_IDS": "111111111111111111",
                "DASHBOARD_DISCORD_ALLOWED_ROLE_IDS": "",
                "DASHBOARD_DISCORD_DEFAULT_ROLE": "admin",
            },
            clear=False,
        )
        self.environment.start()
        initialize_settings_from_env()
        initialize_dashboard_users()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        self.temporary_directory.cleanup()

    def csrf(self, path: str) -> str:
        response = self.client.get(path)
        return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)

    def password_login(self):
        response = self.client.post(
            "/login",
            data={
                "username": "owner",
                "password": "owner-password",
                "csrf": self.csrf("/login"),
            },
        )
        self.assertEqual(response.status_code, 200)

    def start_oauth(self) -> str:
        response = self.client.get(
            "/auth/discord/login",
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        parameters = parse_qs(urlparse(response.headers["location"]).query)
        return parameters["state"][0]

    def oauth_callback(self, identity: dict, state=None):
        oauth_state = state or self.start_oauth()
        identity = {
            **identity,
            "_guild_id": "999999999999999999",
            "_guild_member": {"roles": [], "user": dict(identity)},
        }
        with patch(
            "dashboard.app.fetch_discord_identity",
            return_value=identity,
        ):
            return self.client.get(
                f"/auth/discord/callback?code=test-code&state={oauth_state}",
                follow_redirects=False,
            )

    def test_login_button_configuration_and_password_fallback(self):
        page = self.client.get("/login")
        self.assertIn("Log in with Discord", page.text)
        self.assertNotIn("never-render-this-secret", page.text)
        with patch.dict(os.environ, {"DISCORD_OAUTH_CLIENT_SECRET": ""}):
            page = self.client.get("/login")
        self.assertNotIn("Log in with Discord", page.text)

        self.password_login()
        owner = authenticate_password("owner", "owner-password")
        self.assertEqual(owner["role"], "owner")
        connection = sqlite3.connect(self.database)
        stored = connection.execute(
            "SELECT password_hash FROM dashboard_users WHERE username = 'owner'"
        ).fetchone()[0]
        connection.close()
        self.assertNotEqual(stored, "owner-password")
        self.assertNotIn("owner-password", stored)

    def test_oauth_login_redirect_contains_fixed_parameters_and_state(self):
        response = self.client.get(
            "/auth/discord/login",
            follow_redirects=False,
        )
        location = response.headers["location"]
        parsed = urlparse(location)
        parameters = parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "discord.com")
        self.assertEqual(parameters["client_id"], ["123456789012345678"])
        self.assertEqual(parameters["scope"], ["identify guilds.members.read"])
        self.assertEqual(parameters["response_type"], ["code"])
        self.assertEqual(
            parameters["redirect_uri"],
            ["https://garden.broeden.com/auth/discord/callback"],
        )
        self.assertTrue(parameters["state"][0])

    def test_callback_rejects_missing_invalid_and_reused_state(self):
        missing = self.client.get(
            "/auth/discord/callback?code=test-code",
        )
        self.assertEqual(missing.status_code, 400)
        state = self.start_oauth()
        invalid = self.client.get(
            "/auth/discord/callback?code=test-code&state=wrong-state",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("could not be verified", invalid.text)
        reused = self.client.get(
            f"/auth/discord/callback?code=test-code&state={state}",
        )
        self.assertEqual(reused.status_code, 400)
        canceled_state = self.start_oauth()
        canceled = self.client.get(
            f"/auth/discord/callback?error=access_denied&state={canceled_state}",
        )
        self.assertEqual(canceled.status_code, 401)
        self.assertIn("canceled", canceled.text)

    def test_callback_exchange_failure_is_friendly(self):
        state = self.start_oauth()
        with patch(
            "dashboard.app.fetch_discord_identity",
            side_effect=DiscordOAuthError("raw upstream detail"),
        ):
            response = self.client.get(
                f"/auth/discord/callback?code=test-code&state={state}",
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("could not be completed", response.text)
        self.assertNotIn("raw upstream detail", response.text)

    def test_allowed_discord_user_is_created_without_owner_and_session_is_complete(self):
        response = self.oauth_callback(
            {
                "id": "111111111111111111",
                "username": "approved",
                "global_name": "Approved User",
                "avatar": "avatar-hash",
            }
        )
        self.assertEqual(response.status_code, 303)
        user = next(
            item
            for item in list_dashboard_users()
            if item["discord_user_id"] == "111111111111111111"
        )
        self.assertEqual(user["role"], "admin")
        self.assertEqual(user["auth_provider"], "discord")
        self.assertIsNotNone(user["last_login_at"])
        request = SimpleNamespace(session={})
        login_user(request, user, auth_provider="discord")
        session = request.session
        self.assertEqual(session["dashboard_user_id"], user["id"])
        self.assertEqual(session["dashboard_username"], "Approved User")
        self.assertEqual(session["dashboard_role"], "admin")
        self.assertEqual(session["auth_provider"], "discord")
        self.assertEqual(session["discord_user_id"], "111111111111111111")
        self.assertTrue(session["discord_verified_at"])

        connection = sqlite3.connect(self.database)
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(dashboard_users)")
        }
        serialized = "\n".join(
            str(value)
            for row in connection.execute("SELECT * FROM dashboard_users")
            for value in row
            if value is not None
        )
        connection.close()
        self.assertNotIn("access_token", columns)
        self.assertNotIn("test-code", serialized)
        self.assertNotIn("never-render-this-secret", serialized)

    def test_unknown_discord_user_is_denied_without_registration(self):
        response = self.oauth_callback(
            {
                "id": "222222222222222222",
                "username": "unknown",
                "global_name": "Unknown",
            }
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("not approved", response.text)
        self.assertIsNone(
            next(
                (
                    item
                    for item in list_dashboard_users()
                    if item["discord_user_id"] == "222222222222222222"
                ),
                None,
            )
        )

    def test_invalid_default_role_falls_back_to_admin_not_owner(self):
        with patch.dict(
            os.environ,
            {"DASHBOARD_DISCORD_DEFAULT_ROLE": "owner"},
        ):
            self.assertEqual(default_discord_role(), "admin")

    def test_linked_user_can_login_and_disabled_linked_user_is_denied(self):
        connection = sqlite3.connect(self.database)
        connection.execute(
            """
            INSERT INTO dashboard_users (
                username, discord_user_id, discord_username, role, status,
                auth_provider
            ) VALUES ('linked', '333333333333333333', 'linked', 'viewer',
                      'active', 'discord')
            """
        )
        connection.execute(
            """
            INSERT INTO dashboard_users (
                username, discord_user_id, discord_username, role, status,
                auth_provider
            ) VALUES ('disabled', '444444444444444444', 'disabled', 'admin',
                      'disabled', 'discord')
            """
        )
        connection.commit()
        connection.close()

        linked = self.oauth_callback(
            {"id": "333333333333333333", "username": "linked"}
        )
        self.assertEqual(linked.status_code, 303)
        self.client.cookies.clear()
        disabled = self.oauth_callback(
            {"id": "444444444444444444", "username": "disabled"}
        )
        self.assertEqual(disabled.status_code, 403)
        self.assertIn("disabled", disabled.text.casefold())

    def test_viewer_is_read_only_and_cannot_open_users_page(self):
        with patch.dict(
            os.environ,
            {
                "DASHBOARD_DISCORD_ALLOWED_USER_IDS": "555555555555555555",
                "DASHBOARD_DISCORD_DEFAULT_ROLE": "viewer",
            },
        ):
            response = self.oauth_callback(
                {
                    "id": "555555555555555555",
                    "username": "viewer",
                    "global_name": "Read Only",
                }
            )
        self.assertEqual(response.status_code, 303)
        settings = self.client.get("/settings")
        self.assertEqual(settings.status_code, 403)
        home = self.client.get("/")
        csrf = re.search(
            r'name="csrf" value="([^"]+)"',
            home.text,
        ).group(1)
        update = self.client.post(
            "/settings/update",
            data={
                "csrf": csrf,
                "key": "ASK_COOLDOWN_SECONDS",
                "value": "99",
            },
        )
        self.assertEqual(update.status_code, 403)
        self.assertEqual(self.client.get("/users").status_code, 403)

    def test_owner_can_view_users_and_secret_is_not_rendered(self):
        self.password_login()
        response = self.client.get("/users")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Dashboard Access", response.text)
        self.assertNotIn("never-render-this-secret", response.text)
        settings = self.client.get("/settings")
        self.assertNotIn("DISCORD_OAUTH_CLIENT_SECRET", settings.text)
        self.assertNotIn("never-render-this-secret", settings.text)


if __name__ == "__main__":
    unittest.main()
