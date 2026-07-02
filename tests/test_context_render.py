import os
import unittest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from utils.context_render import parse_ai_json_response


class ParseAiJsonResponseTests(unittest.TestCase):
    def test_parses_clean_json(self):
        parsed = parse_ai_json_response('{"summary": "done", "activityOverview": ["x"]}')
        self.assertEqual(parsed["summary"], "done")
        self.assertEqual(parsed["activityOverview"], ["x"])

    def test_strips_code_fences(self):
        parsed = parse_ai_json_response('```json\n{"summary": "fenced"}\n```')
        self.assertEqual(parsed["summary"], "fenced")

    def test_tolerates_trailing_prose(self):
        parsed = parse_ai_json_response('{"summary": "done"}\n\nNote: staff review advised.')
        self.assertEqual(parsed["summary"], "done")

    def test_salvages_truncation_inside_first_string(self):
        # Model hit its output limit mid-summary: recover the partial value
        # instead of failing and dumping raw JSON.
        parsed = parse_ai_json_response(
            '{\n"summary": "astral was active. Their nickname changed from'
        )
        self.assertIn("summary", parsed)
        self.assertTrue(parsed["summary"].startswith("astral was active"))

    def test_salvages_truncation_mid_array_keeps_complete_fields(self):
        parsed = parse_ai_json_response(
            '{"summary":"text","activityOverview":["a","b"],"positiveContributions":["partial'
        )
        self.assertEqual(parsed["summary"], "text")
        self.assertEqual(parsed["activityOverview"], ["a", "b"])

    def test_salvages_truncation_after_dangling_key(self):
        parsed = parse_ai_json_response('{"summary":"text","activityOverview":')
        self.assertEqual(parsed["summary"], "text")
        self.assertNotIn("activityOverview", parsed)

    def test_salvages_outer_object_not_nested_fragment(self):
        # A truncated messageReferences array must not cause us to return a
        # single nested reference object as if it were the whole summary.
        parsed = parse_ai_json_response(
            '{"summary":"s","messageReferences":[{"label":"x","timestamp":"t"}'
        )
        self.assertEqual(parsed["summary"], "s")
        self.assertIn("messageReferences", parsed)

    def test_raises_on_non_json(self):
        with self.assertRaises(ValueError):
            parse_ai_json_response("totally not json at all")


if __name__ == "__main__":
    unittest.main()
