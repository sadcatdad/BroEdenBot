import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from starlette.requests import Request
from dashboard.app import app
from dashboard.assets import static_asset_url


class DashboardAssetTests(unittest.TestCase):
    def request(self,root=''):
        return Request(dict(type='http',scheme='http',server=('internal-origin',3000),
            path='/scouts',root_path=root,headers=[],router=app.router))

    def test_urls_use_content_hashes_and_stay_same_origin_behind_proxy(self):
        path=Path(__file__).resolve().parents[1]/'dashboard/static/scouts.css'
        digest=hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        self.assertEqual(static_asset_url(self.request(),'scouts.css'),f'/static/scouts.css?v={digest}')
        self.assertEqual(static_asset_url(self.request('/garden'),'scouts.css'),f'/garden/static/scouts.css?v={digest}')

    def test_changing_asset_changes_url_while_identical_bytes_keep_it_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'scouts.css';path.write_text('old layout')
            with patch('dashboard.assets.STATIC_DIR',root):
                first=static_asset_url(self.request(),'scouts.css')
                self.assertEqual(first,static_asset_url(self.request(),'scouts.css'))
                path.write_text('new complete layout')
                second=static_asset_url(self.request(),'scouts.css')
                self.assertNotEqual(first,second)

    def test_template_assets_cannot_escape_static_directory(self):
        with self.assertRaises(ValueError):static_asset_url(self.request(),'../app.py')

    def test_all_template_static_assets_use_content_versioning(self):
        import re
        templates=Path(__file__).resolve().parents[1]/'dashboard/templates'
        for path in templates.glob('*.html'):
            with self.subTest(template=path.name):
                source=path.read_text()
                self.assertNotRegex(source,r"url_for\(['\"]static['\"]")
                self.assertNotRegex(source,r'''(?:src|href)=["']/static/''')
                for asset in re.findall(r"static_asset_url\(request, '([^']+)'\)",source):
                    self.assertTrue((templates.parent/'static'/asset).is_file(),asset)

    def test_templates_do_not_use_executable_inline_scripts_blocked_by_csp(self):
        import re
        templates=Path(__file__).resolve().parents[1]/'dashboard/templates'
        for path in templates.glob('*.html'):
            for attrs,body in re.findall(r'<script\b([^>]*)>(.*?)</script>',path.read_text(),re.S):
                with self.subTest(template=path.name):
                    self.assertTrue('src=' in attrs or 'type="application/json"' in attrs,
                        'Move executable scripts into versioned static files.')
