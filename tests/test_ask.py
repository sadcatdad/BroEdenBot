import unittest

import aiosqlite
import discord

from cogs.ask import (
    Ask,
    AskResponseView,
    _fallback_answer_from_sources,
    _feedback_sources,
    _format_public_response,
)
from utils.ask_feedback import create_ask_feedback, record_ask_feedback


class AskFormattingTests(unittest.TestCase):
    def test_public_response_keeps_question_answer_format_without_sources(self):
        embed = _format_public_response(
            "where can i find server info?",
            "Use the server guide channel.",
        )

        self.assertIn("**Question:**", embed.description)
        self.assertIn("**Answer:**", embed.description)
        self.assertNotIn("\n\n", embed.description)
        self.assertNotIn("Source:", embed.description)
        self.assertEqual(
            embed.footer.text,
            "Private answer • Based only on public Bro Eden knowledge sources",
        )

    def test_feedback_buttons_disable_after_feedback(self):
        view = AskResponseView(owner_id=123, question="where is support?")
        helped_button = next(
            child
            for child in view.children
            if isinstance(child, discord.ui.Button) and child.label == "This Helped"
        )
        helped_button.label = "Marked Helpful"

        view._mark_feedback_used()

        labels_to_disabled = {
            child.label: child.disabled
            for child in view.children
            if isinstance(child, discord.ui.Button)
        }
        self.assertFalse(labels_to_disabled["Open Ticket"])
        self.assertFalse(labels_to_disabled["Search Guide"])
        self.assertTrue(labels_to_disabled["Marked Helpful"])
        self.assertTrue(labels_to_disabled["Still Confused"])

    def test_prompt_rejects_member_facing_source_sections(self):
        prompt = Ask._build_prompt("where is server info?", "Guide context")

        self.assertIn("Do not include source labels", prompt)
        self.assertIn('"Source:" section', prompt)
        self.assertIn("public Bro Eden knowledge source context", prompt)

    def test_empty_ai_fallback_uses_public_source_excerpts(self):
        answer = _fallback_answer_from_sources(
            [
                {
                    "source_name": "live-discord:1:10",
                    "excerpt": "Server boosters get special perks in Bro Eden.",
                },
                {
                    "source_name": "live-discord:1:11",
                    "content": "Use tickets when you need staff help.",
                },
            ]
        )

        self.assertIn("matching public knowledge", answer)
        self.assertIn("Server boosters get special perks", answer)
        self.assertIn("Use tickets when you need staff help", answer)

    def test_feedback_sources_keep_review_context(self):
        sources = _feedback_sources(
            [
                {
                    "id": 42,
                    "source_name": "Public Channel Index",
                    "section_title": "Server Guides",
                    "chunk_index": 3,
                    "score": 12,
                    "content": "not stored in feedback source summary",
                }
            ]
        )

        self.assertEqual(
            sources,
            [
                {
                    "chunk_id": 42,
                    "source_name": "Public Channel Index",
                    "section_title": "Server Guides",
                    "chunk_index": 3,
                    "score": 12,
                }
            ],
        )


class AskFeedbackStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_and_record_feedback(self):
        async with aiosqlite.connect(":memory:") as connection:
            feedback_id = await create_ask_feedback(
                connection,
                guild_id=1,
                channel_id=2,
                user_id=3,
                question="where is support?",
                answer="Open a ticket.",
                kb_sources=[{"chunk_id": 10, "source_name": "FAQ"}],
                model_used="gemini-2.5-flash",
                tier_used="default",
            )
            self.assertIsNotNone(feedback_id)

            updated = await record_ask_feedback(
                connection,
                feedback_id,
                "confused",
            )
            self.assertTrue(updated)

            cursor = await connection.execute(
                """
                SELECT question, answer, feedback, kb_sources_json
                FROM ask_feedback
                WHERE id = ?
                """,
                (feedback_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()

        self.assertEqual(row[0], "where is support?")
        self.assertEqual(row[1], "Open a ticket.")
        self.assertEqual(row[2], "confused")
        self.assertIn("FAQ", row[3])


if __name__ == "__main__":
    unittest.main()
