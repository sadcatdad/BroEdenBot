import unittest

from utils.knowledge import (
    build_public_ask_context,
    build_staff_knowledge_context,
    load_knowledge,
    load_staff_knowledge,
    search_knowledge,
    search_server_knowledge,
)


class KnowledgePrivacyTests(unittest.TestCase):
    def test_rangers_handbook_is_staff_only(self):
        public = load_knowledge()
        staff = load_staff_knowledge()

        self.assertNotIn("Ranger's Handbook (Staff Only)", public)
        self.assertIn("Ranger's Handbook (Staff Only)", staff)

    def test_public_search_and_ask_context_exclude_handbook(self):
        public_results = search_server_knowledge("rest pass verified roles")
        public_context = build_public_ask_context("rest pass verified roles")

        self.assertFalse(
            any("Ranger's Handbook" in source for source, _, _ in public_results)
        )
        self.assertNotIn("Ranger's Handbook", public_context)
        self.assertNotIn("Rest Pass", public_context)

    def test_staff_search_and_context_include_handbook(self):
        staff_results = search_knowledge("rest pass verified roles")
        staff_context = build_staff_knowledge_context(
            "rest pass verified roles"
        )

        self.assertTrue(
            any("Ranger's Handbook" in source for source, _, _ in staff_results)
        )
        self.assertIn("Ranger's Handbook", staff_context)
        self.assertIn("Rest Pass", staff_context)

    def test_gateway_verification_guidance_is_available(self):
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
