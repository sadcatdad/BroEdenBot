import unittest
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from utils.knowledge import (
    build_public_ask_context,
    build_staff_knowledge_context,
    load_knowledge,
    load_staff_knowledge,
    search_knowledge,
    search_server_knowledge,
)
from utils.live_knowledge import initialize_live_knowledge_schema_sync


class KnowledgePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "data.db"
        self.environment = patch.dict(
            os.environ,
            {"DATABASE_PATH": str(self.database)},
            clear=False,
        )
        self.environment.start()
        load_knowledge.cache_clear()
        load_staff_knowledge.cache_clear()
        initialize_live_knowledge_schema_sync()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                INSERT INTO knowledge_entries (
                    guild_id, source_channel_id, source_message_id,
                    source_type, visibility, title, content, content_hash,
                    author_id, created_at, edited_at, indexed_at
                ) VALUES
                    (1, 10, 100, 'survival_guide', 'public', 'Gateway Access',
                     'The Gateway uses ID verification to confirm members are at least 18 years old.',
                     'public-hash', 1, NULL, NULL, '2026-07-02'),
                    (1, 11, 101, 'staff', 'staff_only', 'Rest Pass',
                     'Rest Pass verified roles are staff-only procedure notes.',
                     'staff-hash', 1, NULL, NULL, '2026-07-02')
                """
            )

    def tearDown(self):
        load_knowledge.cache_clear()
        load_staff_knowledge.cache_clear()
        self.environment.stop()
        self.temporary_directory.cleanup()

    def test_legacy_file_knowledge_sources_are_not_loaded(self):
        public = load_knowledge()
        staff = load_staff_knowledge()

        self.assertEqual(public, {})
        self.assertNotIn("Bro Eden Rules", staff)
        self.assertNotIn("Bro Eden Survival Guide", staff)
        self.assertNotIn("Ranger's Handbook (Staff Only)", staff)

    def test_public_search_and_ask_context_exclude_staff_only_live_entries(self):
        public_results = search_server_knowledge("rest pass verified roles")
        public_context = build_public_ask_context("rest pass verified roles")

        self.assertFalse(
            any("Rest Pass" in heading for _, heading, _ in public_results)
        )
        self.assertNotIn("Rest Pass", public_context)

    def test_staff_search_and_context_include_staff_only_live_entries(self):
        staff_results = search_knowledge("rest pass verified roles")
        staff_context = build_staff_knowledge_context(
            "rest pass verified roles"
        )

        self.assertTrue(
            any("Rest Pass" in heading for _, heading, _ in staff_results)
        )
        self.assertIn("Rest Pass", staff_context)

    def test_gateway_verification_guidance_is_available_from_live_knowledge(self):
        public_context = build_public_ask_context(
            "What is the Gateway and how do I access the rest of the server?"
        )
        staff_context = build_staff_knowledge_context(
            "Gateway unverified ID verification over 18"
        )

        self.assertIn("The Gateway", public_context)
        self.assertIn("ID verification", public_context)
        self.assertIn("at least 18 years old", public_context)
        self.assertIn("The Gateway", staff_context)


if __name__ == "__main__":
    unittest.main()
