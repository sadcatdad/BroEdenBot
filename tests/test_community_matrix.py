import io
import unittest
from dataclasses import replace
from unittest.mock import patch

from PIL import Image, ImageDraw

from tests.community_fixtures import evaluation_rows, evaluation_data
from utils.community_contribution import calculate_scorecard
from utils.community_matrix import render_community_matrix, matrix_alt_text
from utils.context_render import build_community_score_embed


class CommunityMatrixTests(unittest.TestCase):
    def setUp(self):
        rows = evaluation_rows()
        self.score = calculate_scorecard(evaluation_data(rows), rows, total_matching=2500)
        self.metadata = {'title': 'Community Evaluation: Example member',
                         'timeframe_text': 'Sep 1 – Nov 29, 2026\n2,500 of 4,784 messages reviewed'}

    def recorded_render(self, score):
        calls = []
        original = ImageDraw.Draw
        class Recorder:
            def __init__(self, image, *args, **kwargs):
                self.draw = original(image, *args, **kwargs)
            def __getattr__(self, name):
                return getattr(self.draw, name)
            def text(self, xy, text, **kwargs):
                calls.append((text, kwargs.get('font').size))
                return self.draw.text(xy, text, **kwargs)
        with patch('utils.community_matrix.ImageDraw.Draw', side_effect=Recorder):
            data = render_community_matrix(score, self.metadata)
        return data, calls

    def test_matrix_renders_real_ratings_points_and_total_in_a_small_png(self):
        data, calls = self.recorded_render(self.score)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.format, 'PNG')
            self.assertEqual(image.width, 1280)
            self.assertLessEqual(image.height, 1600)
            self.assertGreater(len(image.getcolors(2_000_000)), 20)
        self.assertLess(len(data), 1_000_000)
        texts = [text for text, _ in calls]
        self.assertIn('93', [text for text, size in calls if size == 76])
        self.assertIn('+22.5', texts)
        self.assertIn('+5.25', texts)
        self.assertIn('No supported conduct deductions.', texts)
        self.assertIn('2,500 of 4,784 messages reviewed', texts)
        self.assertIn('93/100', matrix_alt_text(self.score))

    def test_unscored_and_review_states_never_render_a_fake_zero_or_private_allegation(self):
        insufficient = calculate_scorecard({}, [], total_matching=0)
        reviewed = replace(self.score, score=None, status='review_required',
                           explanation='A numerical score is withheld pending staff review.',
                           categories=tuple(replace(c, explanation='Private allegation') for c in self.score.categories))
        for score in [insufficient, reviewed]:
            data, calls = self.recorded_render(score)
            self.assertIn('N/A', [text for text, size in calls if size == 76])
            self.assertNotIn('Private allegation', str(calls))
            self.assertNotIn('0/100', matrix_alt_text(score))
            self.assertNotIn('Private allegation', matrix_alt_text(score))
            self.assertTrue(data.startswith(b'\x89PNG'))

    def test_zero_is_a_real_score_and_deductions_and_ceiling_remain_visible(self):
        score = replace(self.score, score=0, penalty=100, ceiling=59,
                        deductions=((100, 'Repeated supported conduct incidents.'),))
        _, calls = self.recorded_render(score)
        texts = [text for text, _ in calls]
        self.assertEqual([text for text, size in calls if size == 76], ['0'])
        self.assertTrue(any('ceiling: 59/100' in text for text in texts))
        self.assertTrue(any('Repeated supported conduct' in text for text in texts))
        self.assertIn('0/100', matrix_alt_text(score))

    def test_long_names_reasons_and_coverage_are_bounded_and_credentials_redacted(self):
        self.metadata['title'] = 'Community Evaluation: ' + '🌈LongName' * 60
        self.metadata['timeframe_text'] = '\n'.join(['Long coverage details ' * 100] * 5)
        score = replace(self.score,
            deductions=tuple((3, 'PASSWORD=private-value ' + 'x' * 300) for _ in range(10)),
            categories=tuple(replace(c, explanation='API_KEY=private-key ' + 'y' * 500) for c in self.score.categories))
        data, calls = self.recorded_render(score)
        self.assertNotIn('private-value', str(calls))
        self.assertNotIn('private-key', matrix_alt_text(score))
        self.assertLessEqual(len(matrix_alt_text(score)), 1024)
        with Image.open(io.BytesIO(data)) as image:
            self.assertLessEqual(image.height, 1700)

    def test_image_embed_has_attachment_url_and_compact_accessible_results(self):
        embed = build_community_score_embed(self.score, self.metadata,
            matrix_image_url='attachment://community-contribution.png')
        self.assertEqual(embed.image.url, 'attachment://community-contribution.png')
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn('Support and helpfulness: 3/4', fields['Rubric results'])
        self.assertIn('22.5', fields['Calculation'])
        self.assertNotIn('Connection and conversation', fields)


if __name__ == '__main__':
    unittest.main()
