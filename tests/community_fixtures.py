"""Representative archive rows and rubric output for community evaluations."""
from datetime import datetime, timedelta, timezone
from utils.community_contribution import CATEGORIES


def evaluation_rows(count=28):
    start = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    return [{
        'message_id': str(1000 + i), 'guild_id': '42', 'channel_id': '99',
        'channel_name': 'general', 'parent_channel_id': None,
        'author_id': '123', 'author_name': 'alice', 'author_display_name': 'Alice',
        'timestamp': (start + timedelta(days=i)).isoformat(),
        'content': 'I can help with that.', 'is_deleted': 0, 'is_bot': 0,
        'is_webhook': 0, 'source': 'live_discord',
        'jump_url': f'https://discord.com/channels/42/99/{1000+i}',
    } for i in range(count)]


def evaluation_data(rows=None, rating=3):
    rows = rows if rows is not None else evaluation_rows()
    evidence = [r['message_id'] for r in rows[::7]]
    return {
        'summary': 'Helpful in discussions.', 'strengths': ['Welcomes newcomers.'],
        'growthOpportunities': [], 'limitations': 'Selected archived activity only.',
        'contributionRubric': {
            'categories': {key: {'rating': rating, 'explanation': 'Offers thoughtful, welcoming replies.',
                                'evidenceMessageIds': evidence} for key, _, _ in CATEGORIES},
            'conductIncidents': [],
        },
        'contextQuotes': [{'messageId': rows[0]['message_id'], 'quote': 'I can help with that.',
                           'timestamp': rows[0]['timestamp'], 'channelName': 'general'}] if rows else [],
    }
