"""The verifier resolves public aliases against the physical private manifest."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from proxy_stack.cli import main, verify
from proxy_stack.model import Settings
from proxy_stack.routes import asset_paths
from proxy_stack.storage import Store


class VerifyRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name).resolve() / 'data')
        self.store.init(Settings('proxy.example.com', 'Test'))
        self.token = self.store.state()['users']['owner']['token']
        self.paths = asset_paths(self.token)
        self.calls = []

    def open(self, request, timeout):
        url = request if isinstance(request, str) else request.full_url
        path = url.removeprefix('https://proxy.example.com')
        self.calls.append((path, 'GET' if isinstance(request, str) else request.get_method()))
        for clean, name, mime in self.paths:
            if path in (clean, '/' + name):
                body = (self.store.current() / name).read_bytes()
                result = io.BytesIO(body)
                result.status = 200
                result.headers = Message()
                for key, value in (('Content-Type', mime), ('Cache-Control', 'no-store'),
                                   ('Referrer-Policy', 'no-referrer'), ('X-Content-Type-Options', 'nosniff')):
                    result.headers[key] = value
                return result
        raise urllib.error.HTTPError(url, 404, 'not found', None, None)

    def test_verify_checks_both_aliases_and_all_embedded_resources(self):
        with patch('proxy_stack.cli.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = self.open
            result = verify(self.store)
        self.assertEqual(result['http_verified'], len(self.paths) * 2)
        for clean, name, _ in self.paths:
            for alias in (clean, '/' + name):
                for method in ('GET', 'HEAD'):
                    self.assertIn((alias, method), self.calls)
        config = json.loads((self.store.current() / self.paths[0][1]).read_text())
        for resource in config['rule-providers'].values():
            self.assertIn((resource['url'].removeprefix('https://proxy.example.com'), 'GET'), self.calls)

    def test_verify_does_not_accept_wrong_body_or_mime_on_canonical_route(self):
        for failure in ('body', 'mime', 'privacy'):
            def bad(request, timeout):
                response = self.open(request, timeout)
                if not isinstance(request, str) and request.full_url.endswith('/quantumult-x/nodes'):
                    if failure == 'body':
                        response = io.BytesIO(b'wrong identity')
                        response.status = 200
                        response.headers = self.open(request, timeout).headers
                    else:
                        response.headers.replace_header('Content-Type' if failure == 'mime' else 'Referrer-Policy', 'text/html' if failure == 'mime' else 'wrong')
                return response
            with self.subTest(failure=failure), patch('proxy_stack.cli.urllib.request.build_opener') as build:
                build.return_value.open.side_effect = bad
                with self.assertRaises(ValueError):
                    verify(self.store)

    def test_links_use_clean_paths_and_full_app_name_without_rotating_identity(self):
        before = self.store.state()['users']
        output = io.StringIO()
        with patch.dict(os.environ, PROXY_STACK_DATA=str(self.store.root)), redirect_stdout(output):
            self.assertEqual(main(['links', '--labelled']), 0)
        text = output.getvalue()
        self.assertEqual(text.count('Quantumult X'), 3)
        self.assertNotIn('/sub/', text)
        for clean, _, _ in self.paths[:4]:
            self.assertIn('https://proxy.example.com' + clean, text)
        self.assertEqual(self.store.state()['users'], before)
