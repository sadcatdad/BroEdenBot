import unittest

from cogs.leaderboards import normalize_leaderboard_name, parse_points
from cogs.poll import (
    deserialize_options,
    parse_duration,
    parse_poll_options,
    serialize_options,
)
from utils.ui import progress_bar, truncate


class PollHelperTests(unittest.TestCase):
    def test_duration_parser_accepts_compound_values(self):
        self.assertEqual(parse_duration("1h 30m"), 5_400)

    def test_duration_parser_rejects_partial_garbage(self):
        self.assertEqual(parse_duration("1h later"), 0)

    def test_options_are_unique_and_bounded(self):
        self.assertEqual(parse_poll_options("Yes, No"), ["Yes", "No"])
        with self.assertRaises(ValueError):
            parse_poll_options("Yes, yes")

    def test_legacy_options_are_read_without_eval(self):
        self.assertEqual(deserialize_options("['A', 'B']"), ["A", "B"])
        encoded = serialize_options(["A", "B"])
        self.assertEqual(deserialize_options(encoded), ["A", "B"])
        with self.assertRaises((ValueError, SyntaxError)):
            deserialize_options("__import__('os').system('echo unsafe')")


class LeaderboardHelperTests(unittest.TestCase):
    def test_points_are_finite_positive_and_rounded(self):
        self.assertEqual(parse_points("2.345"), 2.35)
        self.assertIsNone(parse_points("nan"))
        self.assertIsNone(parse_points("0"))

    def test_names_are_compacted(self):
        self.assertEqual(
            normalize_leaderboard_name("  Weekly   Wins "),
            "Weekly Wins",
        )


class UIHelperTests(unittest.TestCase):
    def test_progress_bar(self):
        self.assertEqual(progress_bar(5, 10, width=4), "▰▰▱▱")

    def test_truncate(self):
        self.assertEqual(truncate("abcdef", 4), "abc…")


if __name__ == "__main__":
    unittest.main()
