import copy
import unittest
from utils.community_contribution import calculate_scorecard, verified_public_quotes
from tests.community_fixtures import evaluation_rows, evaluation_data


class CommunityContributionTests(unittest.TestCase):
    def setUp(self):
        self.rows = evaluation_rows()
        self.data = evaluation_data(self.rows)

    def score(self, **kwargs):
        return calculate_scorecard(self.data, self.rows, total_matching=len(self.rows), **kwargs)

    def test_deterministic_total_ignores_model_total(self):
        self.data['communityContributionScore'] = 1
        result = self.score()
        self.assertEqual(result.score, 93)  # 70 + 22.5, rounded half-up
        self.assertEqual(result.bonus, 22.5)
        self.assertEqual(result.confidence, 'High')

    def test_neutral_categories_start_at_70(self):
        for c in self.data['contributionRubric']['categories'].values():
            c.update(rating=0, evidenceMessageIds=[])
        result = self.score()
        self.assertEqual(result.score, 70)
        self.assertEqual(result.penalty, 0)

    def test_ratings_require_separate_exchanges_dates_and_sustained_evidence(self):
        for c in self.data['contributionRubric']['categories'].values():
            c.update(rating=4, evidenceMessageIds=['1000'])
        result = self.score()
        self.assertTrue(all(c.rating == 1 for c in result.categories))
        self.assertEqual(result.score, 78)

    def test_other_member_and_invented_evidence_do_not_award_points(self):
        for c in self.data['contributionRubric']['categories'].values():
            c['evidenceMessageIds'] = ['invented', 'other-member-id']
        self.assertEqual(self.score().score, 70)

    def test_missing_or_malformed_rubric_never_becomes_zero(self):
        for broken in ({}, {'categories': {}}, {'categories': [], 'conductIncidents': []}):
            self.data['contributionRubric'] = broken
            self.assertIsNone(self.score().score)
        self.data = evaluation_data(self.rows)
        self.data['contributionRubric']['categories']['support']['rating'] = True
        self.assertIsNone(self.score().score)

    def test_few_sessions_and_short_windows_have_honest_limits(self):
        self.rows = evaluation_rows(3)
        self.data = evaluation_data(self.rows)
        self.assertIsNone(self.score().score)
        self.rows = evaluation_rows(7)
        self.data = evaluation_data(self.rows, rating=4)
        for c in self.data['contributionRubric']['categories'].values():
            c['evidenceMessageIds'] = [r['message_id'] for r in self.rows]
        self.assertEqual(self.score().score, 89)
        self.assertEqual(self.score().ceiling, 89)

    def test_capped_and_partial_coverage_reduce_confidence_not_score(self):
        full = self.score()
        capped = calculate_scorecard(self.data, self.rows, total_matching=5000)
        partial = self.score(failed_chunks=1)
        self.assertEqual(full.score, capped.score)
        self.assertEqual(full.score, partial.score)
        self.assertEqual(capped.confidence, 'Medium')
        self.assertEqual(partial.confidence, 'Low')

    def test_pattern_replaces_overlapping_incidents_and_applies_ceiling(self):
        self.data['contributionRubric']['conductIncidents'] = [
            {'type': 'isolated_disrespect', 'explanation': 'An unaddressed personal attack.',
             'evidenceMessageIds': ['1000']},
            {'type': 'repeated_antagonism', 'explanation': 'Repeated personal attacks.',
             'evidenceMessageIds': ['1000', '1007']},
            {'type': 'repeated_antagonism', 'explanation': 'Same pattern again.',
             'evidenceMessageIds': ['1000', '1007']},
        ]
        result = self.score()
        self.assertEqual(result.penalty, 10)
        self.assertEqual(result.score, 69)
        self.assertEqual(len(result.deductions), 1)

    def test_severe_or_contextless_concerns_withhold_public_judgment(self):
        incident = {'type': 'persistent_harm', 'explanation': 'Must not be publicly exposed.',
                    'evidenceMessageIds': ['1000', '1007']}
        self.data['contributionRubric']['conductIncidents'] = [incident]
        result = self.score()
        self.assertEqual(result.status, 'review_required')
        self.assertIsNone(result.score)
        self.assertEqual(result.deductions, ())
        incident['type'] = 'isolated_disrespect'
        self.assertEqual(self.score(context_available_ids=set()).status, 'review_required')

    def test_a_single_exchange_cannot_establish_a_repeated_pattern(self):
        self.data['contributionRubric']['conductIncidents'] = [
            {'type': 'repeated_antagonism', 'evidenceMessageIds': ['1000']},
            {'type': 'persistent_harm', 'evidenceMessageIds': ['1000']},
        ]
        self.assertEqual(self.score().penalty, 0)
        self.assertEqual(self.score().status, 'scored')

    def test_quotes_must_be_verbatim_target_messages_and_use_source_metadata(self):
        quotes = copy.deepcopy(self.data['contextQuotes'])
        quotes[0].update(channelName='invented', timestamp='invented', jumpUrl='https://example.com')
        quotes.extend([{'messageId': 'other', 'quote': 'Private other-user text'},
                       {'messageId': '1000', 'quote': 'Invented words'}])
        result = verified_public_quotes(quotes, self.rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['timestamp'], self.rows[0]['timestamp'])
        self.assertEqual(result[0]['jumpUrl'], self.rows[0]['jump_url'])


if __name__ == '__main__':
    unittest.main()
