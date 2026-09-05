import csv
import io
import os
import re
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from dashboard.app import app
from dashboard.oauth import DiscordOAuthError, fetch_discord_identity
from dashboard.rbac import initialize_rbac_schema, permissions_for_user, role_names_for_user
from dashboard.security import (
    LOGIN_ATTEMPTS,
    clear_login_attempts,
    reserve_login_attempt,
)
from dashboard.users import initialize_dashboard_users, verify_password
from utils.csv_export import SafeCSVDictWriter, SafeCSVWriter
from utils.settings import initialize_settings_from_env


class DashboardHardeningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "data.db"
        environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(self.path),
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "owner",
                "DASHBOARD_PASSWORD": "test-password",
                "DASHBOARD_AUTH_MODE": "hybrid",
                "DISCORD_OAUTH_CLIENT_ID": "123456789012345678",
                "DISCORD_OAUTH_CLIENT_SECRET": "test-only-secret",
                "DISCORD_OAUTH_REDIRECT_URI": "http://testserver/auth/discord/callback",
                "GUILD_ID": "123456789012345678",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        initialize_settings_from_env()
        initialize_dashboard_users()
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def csrf(self):
        return re.search(
            r'name="csrf" value="([^"]+)"', self.client.get("/login").text
        )[1]

    def test_login_throttle_survives_new_session_and_ignores_untrusted_forwarding(self):
        with patch(
            "dashboard.app.authenticate_password", return_value=None
        ) as authenticate:
            for index in range(LOGIN_ATTEMPTS):
                response = self.client.post(
                    "/login",
                    data={
                        "csrf": self.csrf(),
                        "username": f"guess-{index}",
                        "password": "wrong",
                    },
                )
                self.assertEqual(response.status_code, 401)
            self.client.cookies.clear()
            response = self.client.post(
                "/login",
                headers={"X-Forwarded-For": "198.51.100.2"},
                data={
                    "csrf": self.csrf(),
                    "username": "owner",
                    "password": "test-password",
                },
            )
            self.assertEqual(response.status_code, 429)
            self.assertGreater(int(response.headers["Retry-After"]), 0)
            self.assertEqual(authenticate.call_count, LOGIN_ATTEMPTS)

    def test_login_attempts_are_atomic_expire_and_reset(self):
        with patch("dashboard.security.time") as clock:
            clock.time.return_value = 1000
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(reserve_login_attempt, ["same-client"] * 12))
            self.assertEqual(results.count(0), LOGIN_ATTEMPTS)
            self.assertEqual(reserve_login_attempt("different-client"), 0)
            clock.time.return_value = 1301
            self.assertEqual(reserve_login_attempt("same-client"), 0)
            clear_login_attempts("same-client")
            with sqlite3.connect(self.path) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM dashboard_login_attempts"
                    ).fetchone()[0],
                    0,
                )

    def test_successful_password_login_clears_failed_attempts(self):
        reserve_login_attempt("testclient")
        response = self.client.post(
            "/login",
            data={
                "csrf": self.csrf(),
                "username": "owner",
                "password": "test-password",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM dashboard_login_attempts").fetchone()[
                    0
                ],
                0,
            )

    def test_security_headers_on_login_redirect_and_authenticated_pages(self):
        for path in ["/login", "/", "/api/discord/roles"]:
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            self.assertIn(
                "script-src 'self';", response.headers["Content-Security-Policy"]
            )

    def test_non_ascii_csrf_and_oauth_state_are_rejected_without_server_errors(self):
        self.csrf()
        response = self.client.post(
            "/login", data={"csrf": "☃", "username": "owner", "password": "wrong"}
        )
        self.assertEqual(response.status_code, 401)
        self.client.get("/auth/discord/login", follow_redirects=False)
        response = self.client.get(
            "/auth/discord/callback", params={"state": "☃", "code": "test"}
        )
        self.assertEqual(response.status_code, 400)

    def test_oversized_and_invalid_content_lengths_are_rejected(self):
        for length, status in [("1048577", 413), ("-1", 400), ("not-a-number", 400)]:
            response = self.client.post(
                "/login", content=b"x", headers={"Content-Length": length}
            )
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_streamed_body_cannot_bypass_limit_with_missing_or_false_length(self):
        for headers in [{}, {"Content-Length": "1"}]:
            chunks = (b"x" * 64 * 1024 for _ in range(17))
            response = self.client.post(
                "/login",
                content=chunks,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    **headers,
                },
            )
            self.assertEqual(response.status_code, 413)

    def test_excess_urlencoded_fields_are_rejected_by_patched_parser(self):
        response = self.client.post(
            "/login",
            content="&".join(f"x{i}=y" for i in range(1001)),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(response.status_code, 400)

    def test_downgraded_legacy_owner_does_not_retain_owner_permissions(self):
        initialize_rbac_schema()
        with sqlite3.connect(self.path) as db:
            user_id = db.execute(
                "SELECT id FROM dashboard_users WHERE username='owner'"
            ).fetchone()[0]
        self.assertIn("access.manage", permissions_for_user(user_id))
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE dashboard_users SET role='viewer' WHERE id=?", (user_id,)
            )
        self.assertNotIn("access.manage", permissions_for_user(user_id))
        self.assertNotIn("Owner", role_names_for_user(user_id))

    def test_database_contexts_close_even_on_rollback(self):
        from dashboard.users import _connect as users_connect
        from dashboard.rbac import _connect as rbac_connect
        from utils.events import _connect as events_connect

        for connect in [users_connect, rbac_connect, events_connect]:
            db = connect()
            with self.assertRaises(RuntimeError):
                with db:
                    raise RuntimeError("rollback")
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")

    def test_visual_editor_controls_use_visual_capability(self):
        initialize_rbac_schema()
        with sqlite3.connect(self.path) as db:
            user_id = db.execute(
                "SELECT id FROM dashboard_users WHERE username='owner'"
            ).fetchone()[0]
            db.execute(
                "UPDATE dashboard_users SET role='viewer' WHERE id=?", (user_id,)
            )
            db.executemany(
                "INSERT INTO dashboard_user_permission_overrides (user_id, permission_key, allowed) VALUES (?, ?, 1)",
                [(user_id, key) for key in ["visual.view", "visual.manage"]],
            )
        self.assertNotIn("access.manage", permissions_for_user(user_id))
        response = self.client.post(
            "/login",
            data={
                "csrf": self.csrf(),
                "username": "owner",
                "password": "test-password",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        assets = self.client.get("/visual/assets")
        self.assertEqual(assets.status_code, 200)
        self.assertIn("Upload asset", assets.text)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE dashboard_user_permission_overrides SET allowed=0 WHERE user_id=? AND permission_key='visual.manage'",
                (user_id,),
            )
        self.assertNotIn("Upload asset", self.client.get("/visual/assets").text)


class ExportHardeningTests(unittest.TestCase):
    def test_log_sanitizers_share_redaction_of_prefixed_and_quoted_credentials(self):
        from cogs.bot_admin import sanitize_logs
        from dashboard.operations import sanitize_output
        from utils.privacy import redact_sensitive_text

        text = """DISCORD_TOKEN=example-token
GEMINI_API_KEY=example-key
DASHBOARD_SECRET_KEY='signing key with spaces'
{"password": "password with spaces"}
Authorization: Bearer example-bearer
"""
        for sanitize in [sanitize_logs, sanitize_output, redact_sensitive_text]:
            output = sanitize(text)
            for value in [
                "example-token",
                "example-key",
                "signing key with spaces",
                "password with spaces",
                "example-bearer",
            ]:
                self.assertNotIn(value, output)

    def test_both_writers_preserve_numbers_and_neutralize_formulas(self):
        values = [
            "=1+1",
            " +SUM(1,1)",
            "\ufeff@SUM(1,1)",
            "\tcommand",
            "\rcommand",
            "-name",
            0,
            -2,
            None,
            "normal, name",
        ]
        output = io.StringIO()
        SafeCSVWriter(output).writerow(values)
        row = next(csv.reader(io.StringIO(output.getvalue())))
        self.assertEqual(row[:6], ["'" + value for value in values[:6]])
        self.assertEqual(row[6:], ["0", "-2", "", "normal, name"])
        output = io.StringIO()
        writer = SafeCSVDictWriter(output, ["name", "points"])
        writer.writeheader()
        writer.writerows(
            [{"name": "=1+1", "points": -2}, {"name": "Member", "points": 0}]
        )
        self.assertEqual(
            list(csv.DictReader(io.StringIO(output.getvalue()))),
            [
                {"name": "'=1+1", "points": "-2"},
                {"name": "Member", "points": "0"},
            ],
        )

    def test_password_hash_metadata_is_bounded(self):
        with patch("dashboard.users.hashlib.pbkdf2_hmac") as derive:
            self.assertFalse(
                verify_password(
                    "password", "pbkdf2_sha256$999999999$" + "00" * 16 + "$" + "00" * 32
                )
            )
            self.assertFalse(
                verify_password(
                    "password", "pbkdf2_sha256$600000$" + "00" * 16 + "$" + "☃" * 64
                )
            )
            derive.assert_not_called()


class OAuthResponseHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_token_payloads_fail_closed(self):
        for payload in [
            [],
            None,
            "token",
            {"access_token": None},
            {"access_token": 123},
        ]:
            client = AsyncMock()
            client.post.return_value = httpx.Response(
                200, json=payload, request=httpx.Request("POST", "https://discord.com")
            )
            with (
                patch("dashboard.oauth.discord_oauth_configured", return_value=True),
                patch("dashboard.oauth.httpx.AsyncClient") as factory,
            ):
                factory.return_value.__aenter__.return_value = client
                with self.assertRaises(DiscordOAuthError):
                    await fetch_discord_identity("test")
                client.get.assert_not_called()


class ImageHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_avatar_download_stops_when_chunked_response_exceeds_limit(self):
        from utils.stats_visuals.avatars import fetch_avatars

        chunks_read = []

        async def chunks(size):
            for _ in range(100):
                chunks_read.append(size)
                yield b"x" * size

        response = SimpleNamespace(
            status=200,
            content_length=None,
            content=SimpleNamespace(iter_chunked=chunks),
        )
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        session = MagicMock()
        session.get.return_value = context
        with patch("utils.stats_visuals.avatars.aiohttp.ClientSession") as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            result = await fetch_avatars(
                ["https://cdn.discordapp.com/avatars/test/oversized.png"]
            )
        self.assertEqual(result.data, {})
        self.assertLess(len(chunks_read), 100)
        session.get.assert_called_once_with(
            "https://cdn.discordapp.com/avatars/test/oversized.png",
            allow_redirects=False,
        )

    async def test_untrusted_avatar_hosts_are_never_contacted(self):
        from utils.stats_visuals.avatars import fetch_avatars

        urls = [
            "http://127.0.0.1/private",
            "https://cdn.discordapp.com.evil.invalid/a.png",
            "https://user@cdn.discordapp.com/a.png",
            "https://cdn.discordapp.com:444/a.png",
        ]
        with patch("utils.stats_visuals.avatars.aiohttp.ClientSession") as session:
            result = await fetch_avatars(urls)
        session.assert_not_called()
        self.assertEqual(set(result.failed_urls), set(urls))

    async def test_oversized_image_dimensions_are_checked_before_pixel_decoding(self):
        from dashboard.events_manager import normalize_event_image
        from utils.brofiles import _normalized_media
        from utils.visual_studio.storage import inspect_upload

        source = MagicMock()
        source.size = (10000, 10000)
        source.__enter__.return_value = source
        with (
            patch("PIL.Image.open", return_value=source),
            patch("PIL.ImageOps.exif_transpose") as decode,
        ):
            for operation in [
                lambda: normalize_event_image(b"image", "image/png"),
                lambda: _normalized_media(b"image", "upload.png", "banner"),
                lambda: inspect_upload(
                    b"image", filename="upload.png", asset_type="other"
                ),
            ]:
                with self.assertRaises(ValueError):
                    operation()
            decode.assert_not_called()

    async def test_decompression_bombs_use_safe_avatar_fallback(self):
        from utils.stats_visuals.avatars import prepare_avatar

        with patch(
            "PIL.Image.open", side_effect=Image.DecompressionBombError("too large")
        ):
            self.assertIsNone(prepare_avatar(b"image", 64))

    async def test_discord_media_redirects_are_rejected(self):
        from utils.visual_studio.storage import _NoMediaRedirects

        with self.assertRaises(ValueError):
            _NoMediaRedirects().redirect_request(
                None, None, 302, "", {}, "http://127.0.0.1/private"
            )


if __name__ == "__main__":
    unittest.main()
