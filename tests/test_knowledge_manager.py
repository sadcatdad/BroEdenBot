import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard.app import app
from utils.knowledge_manager import (
    DOCUMENT_BY_KEY,
    MAX_DOCUMENT_BYTES,
    KnowledgeDocument,
    document_details,
    list_documents,
    queue_knowledge_reindex,
    recent_knowledge_audit,
    save_document,
    process_knowledge_reindex,
)


class KnowledgeManagerTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "data.db"
        (self.root / "data" / "knowledge").mkdir(parents=True)
        (self.root / "data" / "staff_knowledge").mkdir(parents=True)
        (self.root / "docs").mkdir()
        (self.root / "data" / "knowledge" / "rules.md").write_text(
            "# Rules\n\nBe excellent to each other.\n",
            encoding="utf-8",
        )
        (self.root / "data" / "knowledge" / "survival_guide.md").write_text(
            "# Survival Guide\n\nOpen a ticket when you need help.\n",
            encoding="utf-8",
        )
        (self.root / "data" / "staff_knowledge" / "rangers_handbook.md").write_text(
            "# Ranger Handbook\n\nPrivate staff guidance.\n",
            encoding="utf-8",
        )
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
        self.project_root = patch(
            "utils.knowledge_manager.PROJECT_ROOT",
            self.root,
        )
        self.environment.start()
        self.project_root.start()

    def tearDown(self):
        self.project_root.stop()
        self.environment.stop()
        self.temporary_directory.cleanup()


class KnowledgeManagerHelperTests(KnowledgeManagerTestCase):
    def test_listing_and_detail_handle_existing_and_missing_documents(self):
        documents = list_documents()
        rules = next(item for item in documents if item["doc_key"] == "rules")
        missing = next(
            item for item in documents if item["doc_key"] == "message-context"
        )
        self.assertEqual(rules["status"], "found")
        self.assertGreater(rules["word_count"], 0)
        self.assertEqual(missing["status"], "missing")
        self.assertEqual(document_details("rules")["content"].splitlines()[0], "# Rules")

    def test_unknown_keys_and_external_symlinks_are_rejected(self):
        with self.assertRaises(KeyError):
            document_details("../../.env")
        outside = Path(self.temporary_directory.name).parent / "outside-knowledge.md"
        outside.write_text("outside", encoding="utf-8")
        rules = self.root / "data" / "knowledge" / "rules.md"
        rules.unlink()
        rules.symlink_to(outside)
        try:
            with self.assertRaisesRegex(ValueError, "leaves the project"):
                document_details("rules")
        finally:
            outside.unlink(missing_ok=True)

    def test_edit_creates_backup_writes_utf8_and_records_audit(self):
        backup = save_document(
            "rules",
            "# Rules\n\nCafé members are welcome.\n",
            "admin",
        )
        self.assertIsNotNone(backup)
        self.assertTrue(backup.is_file())
        self.assertIn("Be excellent", backup.read_text(encoding="utf-8"))
        saved = (self.root / "data" / "knowledge" / "rules.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Café", saved)
        audit = recent_knowledge_audit()
        self.assertEqual(audit[0]["action"], "edit")
        self.assertEqual(audit[0]["doc_key"], "rules")
        self.assertTrue(audit[0]["backup_path"].startswith("backups/knowledge/"))

    def test_read_only_oversized_binary_and_secret_content_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "read-only"):
            save_document("checklists", "No", "admin")
        with self.assertRaisesRegex(ValueError, "1 MB"):
            save_document("rules", "x" * (MAX_DOCUMENT_BYTES + 1), "admin")
        with self.assertRaisesRegex(ValueError, "Binary"):
            save_document("rules", "hello\x00world", "admin")
        with self.assertRaisesRegex(ValueError, "credential or secret"):
            save_document("rules", "DISCORD_TOKEN=very-secret-value", "admin")

    def test_unsupported_allowlisted_extension_is_rejected(self):
        document = KnowledgeDocument(
            "temporary-json",
            "Temporary JSON",
            "data/knowledge/temporary.json",
            "Bot Docs",
            True,
            "internal",
            "Test-only unsupported file.",
        )
        DOCUMENT_BY_KEY[document.doc_key] = document
        try:
            with self.assertRaisesRegex(ValueError, "Markdown and text"):
                save_document(document.doc_key, "{}", "admin")
        finally:
            DOCUMENT_BY_KEY.pop(document.doc_key, None)

    def test_reindex_uses_fixed_payloads_and_records_audit(self):
        one_id = queue_knowledge_reindex("rules", "admin")
        all_id = queue_knowledge_reindex(None, "admin")
        connection = sqlite3.connect(self.database)
        rows = connection.execute(
            """
            SELECT id, action_type, payload_json
            FROM dashboard_actions
            ORDER BY id
            """
        ).fetchall()
        connection.close()
        self.assertEqual([row[0] for row in rows], [one_id, all_id])
        self.assertTrue(all(row[1] == "reindex_knowledge" for row in rows))
        self.assertEqual(json.loads(rows[0][2]), {"doc_key": "rules"})
        self.assertEqual(json.loads(rows[1][2]), {"scope": "all"})
        actions = {row["action"] for row in recent_knowledge_audit()}
        self.assertIn("reindex_requested", actions)
        self.assertIn("reindex_all_requested", actions)
        with self.assertRaises(KeyError):
            queue_knowledge_reindex("; rm -rf /", "admin")
        with self.assertRaisesRegex(ValueError, "not used"):
            queue_knowledge_reindex("checklists", "admin")

    def test_bot_side_reindex_reloads_existing_caches_only(self):
        with patch(
            "utils.knowledge_manager.reload_knowledge",
            return_value={"public_sources": 2, "staff_sources": 3},
        ) as reload_mock:
            ok, message = process_knowledge_reindex({"doc_key": "rules"})
            self.assertTrue(ok)
            self.assertIn("Bro Eden Rules", message)
            reload_mock.assert_called_once_with()
        with self.assertRaisesRegex(ValueError, "Invalid"):
            process_knowledge_reindex({"doc_key": "rules", "command": "whoami"})

    def test_preview_redacts_obvious_secrets_and_handles_binary(self):
        rules = self.root / "data" / "knowledge" / "rules.md"
        rules.write_text(
            "DISCORD_TOKEN=do-not-display\nsafe text",
            encoding="utf-8",
        )
        details = document_details("rules")
        self.assertNotIn("do-not-display", details["content"])
        self.assertIn("[REDACTED]", details["content"])
        rules.write_bytes(b"text\x00binary")
        details = document_details("rules")
        self.assertEqual(details["status"], "unreadable")
        self.assertIn("Binary", details["error"])


class KnowledgeManagerRouteTests(KnowledgeManagerTestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def login(self):
        page = self.client.get("/login")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = self.client.post(
            "/login",
            data={
                "username": "admin",
                "password": "test-password",
                "csrf": token,
            },
        )
        self.assertEqual(response.status_code, 200)

    def csrf(self, path="/knowledge"):
        page = self.client.get(path)
        return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    def test_auth_is_required_for_pages_and_actions(self):
        for path in (
            "/knowledge",
            "/knowledge/rules",
            "/knowledge/rules/edit",
            "/knowledge/rules/preview",
        ):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
        for path in (
            "/knowledge/rules/edit",
            "/knowledge/rules/reindex",
            "/knowledge/reindex-all",
        ):
            response = self.client.post(
                path,
                data={"csrf": "bad"},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)

    def test_pages_render_metadata_missing_docs_and_safe_preview(self):
        self.login()
        listing = self.client.get("/knowledge")
        self.assertEqual(listing.status_code, 200)
        self.assertIn("Bro Eden Rules", listing.text)
        self.assertIn("Message Context Guide", listing.text)
        self.assertIn("missing", listing.text)
        detail = self.client.get("/knowledge/rules")
        self.assertIn("data/knowledge/rules.md", detail.text)
        rules = self.root / "data" / "knowledge" / "rules.md"
        rules.write_text("<script>alert('nope')</script>", encoding="utf-8")
        preview = self.client.get("/knowledge/rules/preview")
        self.assertNotIn("<script>", preview.text)
        self.assertIn("&lt;script&gt;", preview.text)

    def test_unknown_and_read_only_documents_are_rejected(self):
        self.login()
        self.assertEqual(self.client.get("/knowledge/not-a-doc").status_code, 404)
        self.assertEqual(self.client.get("/knowledge/checklists/edit").status_code, 403)

    def test_posts_require_csrf(self):
        self.login()
        for path in (
            "/knowledge/rules/edit",
            "/knowledge/rules/reindex",
            "/knowledge/reindex-all",
        ):
            response = self.client.post(path, data={"csrf": "bad"})
            self.assertEqual(response.status_code, 400)

    def test_edit_and_reindex_routes_create_expected_records(self):
        self.login()
        token = self.csrf("/knowledge/rules/edit")
        response = self.client.post(
            "/knowledge/rules/edit",
            data={"csrf": token, "content": "# Rules\n\nUpdated safely.\n"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn(
            "Updated safely",
            (self.root / "data" / "knowledge" / "rules.md").read_text(
                encoding="utf-8"
            ),
        )
        token = self.csrf("/knowledge/rules")
        response = self.client.post(
            "/knowledge/rules/reindex",
            data={"csrf": token, "doc_key": "../../.env", "command": "whoami"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        connection = sqlite3.connect(self.database)
        row = connection.execute(
            """
            SELECT action_type, payload_json
            FROM dashboard_actions
            WHERE action_type = 'reindex_knowledge'
            """
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], "reindex_knowledge")
        self.assertEqual(json.loads(row[1]), {"doc_key": "rules"})

    def test_existing_dashboard_pages_and_ai_cogs_still_import(self):
        self.login()
        for path in ("/", "/settings", "/bank", "/imports", "/stats"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
        from cogs.ask import Ask
        from cogs.guide import Guide
        from cogs.mod_ai import ModAI
        from cogs.staff_ai import StaffAI

        self.assertTrue(all((Ask, Guide, ModAI, StaffAI)))


if __name__ == "__main__":
    unittest.main()
