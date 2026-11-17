import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from dashboard.app import app
from utils.notion_library import NotionClient, NotionUnavailable, articles, connect, sources, sync_library
from dashboard.rbac import initialize_rbac_schema
from utils.settings import initialize_settings_from_env


class FakeNotion:
    def get(self, path):
        return {'properties': {'title': {'type': 'title', 'title': [{'plain_text': 'Private handbook'}]}}, 'last_edited_time': '2026-11-17'}

    def blocks(self, page, budget):
        return [{'type': 'paragraph', 'text': '<script>alert(1)</script> secretneedle'}]


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATABASE_PATH': str(Path(self.temp.name)/'data.db'),
            'DASHBOARD_ENABLED': 'true', 'DASHBOARD_USERNAME': 'admin', 'DASHBOARD_PASSWORD': 'test-password',
            'DASHBOARD_SECRET_KEY': 'test-key'})
        self.env.start()
        initialize_settings_from_env()
        initialize_rbac_schema()
        sync_library(FakeNotion())

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def login(self, client):
        text = client.get('/login').text
        csrf = re.search(r'name="csrf" value="([^"]+)"', text)[1]
        client.post('/login', data={'username': 'admin', 'password': 'test-password', 'csrf': csrf})

    def test_audience_and_search_do_not_leak(self):
        self.assertEqual(articles(set(), 'secretneedle'), [])
        self.assertEqual(len(articles({'library.scout'}, 'secretneedle')), 1)
        self.assertEqual({r['audience'] for r in articles({'library.scout', 'library.captain'})}, {'scout', 'captain'})
        self.assertEqual(articles({'library.member'}), [])

    def test_manifest_removal_and_audience_change_apply_without_sync(self):
        items = sources()
        items[0]['audience'] = 'owner'
        with patch('utils.notion_library.sources', return_value=items[:1]):
            self.assertEqual(articles({'library.ranger'}), [])
            self.assertEqual(len(articles({'library.owner'})), 1)

    def test_access_revoked_withdraws_copy_but_temporary_failure_preserves(self):
        fake = FakeNotion()
        fake.get = lambda path: (_ for _ in ()).throw(NotionUnavailable('Notion returned HTTP 503'))
        self.assertEqual(sync_library(fake)['failed'], 7)
        self.assertEqual(len(articles({'library.scout'})), 1)
        fake.get = lambda path: (_ for _ in ()).throw(NotionUnavailable('Notion returned HTTP 404'))
        sync_library(fake)
        self.assertEqual(articles({'library.scout'}), [])

    def test_routes_escape_content_and_enforce_current_permissions(self):
        with TestClient(app) as client:
            self.assertEqual(client.get('/library', follow_redirects=False).status_code, 303)
            self.login(client)
            response = client.get('/library')
            self.assertEqual(response.status_code, 200)
            self.assertIn('private, no-store', response.headers['cache-control'])
            scout = next(s for s in sources() if s['audience'] == 'scout')['page_id']
            page = client.get('/library/' + scout)
            self.assertIn('&lt;script&gt;', page.text)
            self.assertNotIn('<script>alert', page.text)
            self.assertNotIn('api.notion.com', page.text)
            # A permission change applies to the next request, not just login.
            with patch('dashboard.auth.current_permissions', return_value={'library.member'}), patch('dashboard.library_routes.current_permissions', return_value={'library.member'}):
                self.assertEqual(client.get('/library/' + scout).status_code, 404)
                self.assertNotIn('Private handbook', client.get('/library?q=secretneedle').text)

    def test_blocks_paginate_recurse_and_never_copy_file_urls_or_children(self):
        client = NotionClient('fake-token')
        responses = [
            {'results': [{'id': 'child', 'type': 'callout', 'callout': {'rich_text': [{'plain_text': 'Hello'}]}, 'has_children': True}], 'has_more': True, 'next_cursor': 'next'},
            {'results': [{'type': 'paragraph', 'paragraph': {'rich_text': [{'plain_text': 'Nested'}]}}], 'has_more': False},
            {'results': [{'type': 'child_page', 'has_children': True}, {'type': 'file', 'file': {'file': {'url': 'https://private-signed-url'}}}], 'has_more': False},
        ]
        with patch.object(client, 'get', side_effect=responses) as get:
            nodes = client.blocks('page', [20])
        self.assertEqual(nodes[0]['children'][0]['text'], 'Nested')
        self.assertEqual(len(nodes), 2)
        self.assertNotIn('private-signed-url', json.dumps(nodes))
        self.assertEqual(get.call_args_list[-1].args[1]['start_cursor'], 'next')

    def test_migration_does_not_regrant_removed_permissions(self):
        import dashboard.rbac as rbac
        with connect() as db:
            db.execute("DELETE FROM dashboard_role_permissions WHERE permission_key='library.scout' AND role_id=(SELECT id FROM dashboard_roles WHERE role_key='scout')")
        rbac._INITIALIZED_PATHS.clear()
        initialize_rbac_schema()
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dashboard_role_permissions WHERE permission_key='library.scout' AND role_id=(SELECT id FROM dashboard_roles WHERE role_key='scout')").fetchone()[0], 0)
