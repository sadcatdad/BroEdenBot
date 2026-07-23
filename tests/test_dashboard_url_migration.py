import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard.app import app


class DashboardUrlMigrationTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "DASHBOARD_PUBLIC_URL": "https://garden.broeden.com",
                "DASHBOARD_LEGACY_HOSTS": "dashboard.broeden.com",
            },
            clear=False,
        )
        self.environment.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.environment.stop()

    def test_legacy_host_redirect_preserves_path_and_query(self):
        response = self.client.get(
            "/events?month=2026-07",
            headers={"host": "dashboard.broeden.com"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 308)
        self.assertEqual(
            response.headers["location"],
            "https://garden.broeden.com/events?month=2026-07",
        )

    def test_canonical_host_does_not_redirect_and_renders_canonical_link(self):
        response = self.client.get(
            "/login?next=events",
            headers={"host": "garden.broeden.com"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            '<link rel="canonical" href="https://garden.broeden.com/login?next=events">',
            response.text,
        )
        self.assertIn("<h1>The Garden</h1>", response.text)

    def test_unrelated_hosts_are_not_redirected(self):
        response = self.client.get(
            "/login",
            headers={"host": "service.up.railway.app"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
