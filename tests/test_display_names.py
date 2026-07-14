import unittest

from PIL import ImageFont

from utils.display_names import normalize_display_name, normalize_for_font


class DisplayNameNormalizationTests(unittest.TestCase):
    def test_mathematical_and_script_letters_become_readable(self):
        self.assertEqual(normalize_display_name("𝓙𝓾𝓼𝓽𝓲𝓷𝓮"), "Justine")
        self.assertEqual(normalize_display_name("𝕽𝖎𝖛𝖊𝖗"), "River")
        self.assertEqual(normalize_display_name("Ⓐ-Ｌｉｓｔ"), "A-List")

    def test_emoji_and_supported_decorations_are_preserved(self):
        self.assertEqual(normalize_display_name("🎸 𝓡𝓲𝓯𝓯 ✨"), "🎸 Riff ✨")

    def test_control_characters_and_multiline_names_are_safe(self):
        self.assertEqual(
            normalize_display_name("A\n\u202eB\tC"),
            "A B C",
        )

    def test_empty_name_uses_requested_fallback(self):
        self.assertEqual(normalize_display_name("", fallback="Member"), "Member")

    def test_font_filter_removes_only_missing_glyph_boxes(self):
        font = ImageFont.truetype("assets/OpenSansEmoji.ttf", 20)
        self.assertEqual(
            normalize_for_font("🎸 𝓡𝓲𝓯𝓯 ✨", font),
            "🎸 Riff",
        )


if __name__ == "__main__":
    unittest.main()
