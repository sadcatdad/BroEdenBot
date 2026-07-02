import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.ai_kb import (
    delete_kb_source,
    get_kb_source,
    get_kb_status,
    list_kb_sources,
    search_kb,
    upsert_kb_source,
)


class AIKnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "data.db"
        self.environment = patch.dict(
            os.environ,
            {"DATABASE_PATH": str(self.database)},
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary_directory.cleanup()

    def test_upsert_search_status_and_delete_source(self):
        result = upsert_kb_source(
            source_name="faq",
            source_type="faq",
            visibility="public",
            raw_text="# FAQ\n\nUse <#123456789012345678> for support tickets.",
        )

        self.assertEqual(result["chunk_count"], 1)
        self.assertEqual(get_kb_status()["public_chunks"], 1)
        self.assertEqual(list_kb_sources()[0]["source_name"], "faq")
        self.assertIn("support tickets", get_kb_source("faq")["raw_content"])

        matches = search_kb(query="support tickets", visibility="public")
        self.assertEqual(matches[0]["source_name"], "faq")
        self.assertEqual(matches[0]["source_visibility"], "public")

        self.assertEqual(delete_kb_source("faq"), 1)
        self.assertEqual(search_kb(query="support tickets", visibility="public"), [])

    def test_public_search_does_not_return_staff_only_chunks(self):
        upsert_kb_source(
            source_name="staff-note",
            source_type="staff_note",
            visibility="staff",
            raw_text="Internal escalation checklist for staff only.",
        )
        upsert_kb_source(
            source_name="staff-source-channel",
            source_type="staff",
            visibility="staff_only",
            raw_text="Private source channel escalation runbook.",
        )

        self.assertEqual(search_kb(query="escalation checklist", visibility="public"), [])
        self.assertEqual(
            search_kb(query="escalation checklist", visibility="staff")[0]["source_name"],
            "staff-note",
        )
        staff_only_matches = search_kb(
            query="source channel runbook",
            visibility="staff",
        )
        self.assertEqual(staff_only_matches[0]["source_name"], "staff-source-channel")
        self.assertEqual(staff_only_matches[0]["source_visibility"], "staff_only")

    def test_invalid_values_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid source type"):
            upsert_kb_source(
                source_name="bad",
                source_type="event_ai",
                visibility="public",
                raw_text="hello",
            )
        with self.assertRaisesRegex(ValueError, "Invalid visibility"):
            upsert_kb_source(
                source_name="bad",
                source_type="faq",
                visibility="private",
                raw_text="hello",
            )
