import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiosqlite

from utils.ai_kb import initialize_ai_kb_schema_async, search_kb
from utils.knowledge import search_knowledge, search_server_knowledge
from utils.live_knowledge import (
    delete_knowledge_source,
    initialize_live_knowledge_schema,
    message_to_knowledge,
    upsert_knowledge_entry_from_message,
    upsert_knowledge_source,
)


class LiveKnowledgeFormattingTests(unittest.TestCase):
    def test_message_content_embed_and_useful_attachment_become_markdown(self):
        embed = SimpleNamespace(
            title="Survival Guide",
            description="Start in The Gateway.",
            fields=[
                SimpleNamespace(name="Tickets", value="Use support for help."),
            ],
        )
        attachment = SimpleNamespace(
            filename="guide.pdf",
            url="https://cdn.discordapp.com/guide.pdf",
        )
        message = SimpleNamespace(
            content="Welcome notes",
            embeds=[embed],
            attachments=[attachment],
            channel=SimpleNamespace(name="guide"),
        )

        title, content = message_to_knowledge(message)

        self.assertEqual(title, "Survival Guide")
        self.assertIn("Welcome notes", content)
        self.assertIn("# Survival Guide", content)
        self.assertIn("## Tickets\nUse support for help.", content)
        self.assertIn("[guide.pdf](https://cdn.discordapp.com/guide.pdf)", content)

    def test_empty_image_only_message_is_ignored(self):
        message = SimpleNamespace(
            content="",
            embeds=[],
            attachments=[
                SimpleNamespace(
                    filename="decorative.png",
                    url="https://cdn.discordapp.com/decorative.png",
                )
            ],
            channel=SimpleNamespace(name="rules"),
        )

        self.assertEqual(message_to_knowledge(message), ("", ""))

    def test_forum_thread_title_is_indexable_even_without_body(self):
        thread = SimpleNamespace(name="VC Guide", parent=SimpleNamespace(id=10), owner_id=20)
        message = SimpleNamespace(
            content="",
            embeds=[],
            attachments=[],
            channel=thread,
        )

        title, content = message_to_knowledge(message)

        self.assertEqual(title, "VC Guide")
        self.assertEqual(content, "# VC Guide")


class LiveKnowledgeDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "data.db"
        self.environment = patch.dict(
            os.environ,
            {"DATABASE_PATH": str(self.database)},
            clear=False,
        )
        self.environment.start()

    async def asyncTearDown(self):
        self.environment.stop()
        self.temporary_directory.cleanup()

    async def _connect(self):
        connection = await aiosqlite.connect(self.database)
        connection.row_factory = aiosqlite.Row
        await initialize_ai_kb_schema_async(connection)
        await initialize_live_knowledge_schema(connection)
        return connection

    async def test_source_entry_schema_and_ai_kb_mirror(self):
        connection = await self._connect()
        try:
            await upsert_knowledge_source(
                connection,
                guild_id=1,
                channel_id=2,
                channel_name="rules",
                source_type="rules",
                visibility="public",
                sync_mode="live",
            )
            cursor = await connection.execute("SELECT * FROM knowledge_sources")
            source = await cursor.fetchone()
            await cursor.close()

            message = SimpleNamespace(
                id=99,
                guild=SimpleNamespace(id=1),
                channel=SimpleNamespace(id=2, name="rules"),
                author=SimpleNamespace(id=42, bot=False),
                created_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
                edited_at=None,
                content="# Rules\n\nBe kind in chat.",
                embeds=[],
                attachments=[],
            )
            indexed = await upsert_knowledge_entry_from_message(
                connection,
                message=message,
                source=source,
            )
            await connection.commit()
        finally:
            await connection.close()

        self.assertTrue(indexed)
        with sqlite3.connect(self.database) as sync_connection:
            sync_connection.row_factory = sqlite3.Row
            row = sync_connection.execute(
                "SELECT * FROM knowledge_entries WHERE source_message_id = 99"
            ).fetchone()
            self.assertEqual(row["source_type"], "rules")
            self.assertEqual(row["visibility"], "public")

        matches = search_kb(query="kind chat", visibility="public")
        self.assertEqual(matches[0]["source_name"], "live-discord:1:99")

    async def test_specific_forum_thread_source_keeps_thread_identity(self):
        connection = await self._connect()
        try:
            await upsert_knowledge_source(
                connection,
                guild_id=1,
                channel_id=500,
                channel_name="Survival Guide Post",
                source_type="survival_guide",
                visibility="public",
                sync_mode="live",
            )
            cursor = await connection.execute("SELECT * FROM knowledge_sources")
            source = await cursor.fetchone()
            await cursor.close()

            thread = SimpleNamespace(
                id=500,
                name="Survival Guide Post",
                parent=SimpleNamespace(id=20),
                owner_id=30,
            )
            message = SimpleNamespace(
                id=501,
                guild=SimpleNamespace(id=1),
                channel=thread,
                author=SimpleNamespace(id=42, bot=False),
                created_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
                edited_at=None,
                content="Forum post source material.",
                embeds=[],
                attachments=[],
            )
            indexed = await upsert_knowledge_entry_from_message(
                connection,
                message=message,
                source=source,
            )
            await connection.commit()

            self.assertTrue(indexed)
            cursor = await connection.execute(
                """
                SELECT source_channel_id
                FROM knowledge_entries
                WHERE source_message_id = 501
                """
            )
            row = await cursor.fetchone()
            await cursor.close()
            self.assertEqual(row["source_channel_id"], 500)

            deleted = await delete_knowledge_source(
                connection,
                guild_id=1,
                channel_id=500,
            )
            await connection.commit()
        finally:
            await connection.close()

        self.assertEqual(deleted, 1)
        self.assertEqual(search_kb(query="source material", visibility="public"), [])

    async def test_live_search_respects_staff_only_visibility(self):
        connection = await self._connect()
        try:
            await connection.execute(
                """
                INSERT INTO knowledge_entries (
                    guild_id, source_channel_id, source_message_id,
                    source_type, visibility, title, content, content_hash,
                    author_id, created_at, edited_at, indexed_at
                ) VALUES
                    (1, 10, 100, 'vc_guide', 'public', 'VC Guide',
                     'Banana voice chat setup guidance.', 'a', 1, NULL, NULL, '2026-07-02'),
                    (1, 11, 101, 'staff', 'staff_only', 'Escalation',
                     'Pear escalation runbook for staff only.', 'b', 1, NULL, NULL, '2026-07-02')
                """
            )
            await connection.commit()
        finally:
            await connection.close()

        public_results = search_server_knowledge("pear escalation runbook")
        staff_results = search_knowledge("pear escalation runbook")
        guide_results = search_server_knowledge("banana voice chat")

        self.assertFalse(any("Escalation" in heading for _, heading, _ in public_results))
        self.assertTrue(any("Escalation" in heading for _, heading, _ in staff_results))
        self.assertTrue(any("VC Guide" in heading for _, heading, _ in guide_results))


if __name__ == "__main__":
    unittest.main()
