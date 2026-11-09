#!/usr/bin/env python3
"""Static publish allowlist and page contract. No secrets, no network."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
GATEWAY = os.path.join(REPO, 'liuguang-gateway')
PAGE = os.path.join(REPO, 'liuguang')
FORBIDDEN_RE = re.compile(
    r'ghp_[A-Za-z0-9]|github_pat_|AKIA[0-9A-Z]{8}|BEGIN (?:OPENSSH|RSA |PRIVATE)|issueTok|REVIEW_ISSUES_TOKEN|api\.github\.com'
)
TUNNEL_URL_RE = re.compile(r'https://[A-Za-z0-9-]+\.trycloudflare\.com')


class PublishExcludeTest(unittest.TestCase):
    def test_publish_tree_omits_gateway_and_private_names(self):
        out = tempfile.mkdtemp(prefix='liuguang-pages-')
        os.rmdir(out)
        proc = subprocess.run(
            [sys.executable, os.path.join(GATEWAY, 'publish_pages_static.py'), '--repo', REPO, '--out', out],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        banned = []
        for dirpath, dirnames, filenames in os.walk(out):
            for name in dirnames + filenames:
                if name in {'liuguang-gateway', 'SENTINEL-DO-NOT-PUBLISH.txt'} or name.endswith('.py'):
                    banned.append(os.path.join(dirpath, name))
                if name in {'answers', 'idempotency', 'receipts', 'sessions'}:
                    banned.append(os.path.join(dirpath, name))
        self.assertEqual(banned, [])
        sentinel = 'This file marks the private gateway tree.'
        for dirpath, _, filenames in os.walk(os.path.join(out, 'liuguang')):
            for name in filenames:
                blob = open(os.path.join(dirpath, name), encoding='utf-8', errors='ignore').read()
                self.assertNotIn(sentinel, blob)
                self.assertIsNone(FORBIDDEN_RE.search(blob), name)
                self.assertIsNone(TUNNEL_URL_RE.search(blob), name)
        gateway = json.loads(open(os.path.join(out, 'liuguang', 'gateway.json'), encoding='utf-8').read())
        self.assertEqual(gateway.get('gateway'), '')
        self.assertTrue(os.path.isfile(os.path.join(out, 'liuguang', 'index.html')))
        self.assertFalse(os.path.exists(os.path.join(out, 'liuguang-gateway')))

    def test_page_keeps_ids_storage_key_and_has_no_secret(self):
        html = open(os.path.join(PAGE, 'index.html'), encoding='utf-8').read()
        client = open(os.path.join(GATEWAY, 'browser', 'submit-client.js'), encoding='utf-8').read()
        self.assertIn(client, html)
        self.assertIn("const STORAGE_KEY = 'liuguang-checklist-20261109-v1';", html)
        self.assertIn('id="btn-submit"', html)
        self.assertIn('credentials: \'omit\'', client)
        self.assertNotIn('Set-Cookie', client)
        self.assertIn('location.hash', client)
        self.assertIn("new URLSearchParams(hash.slice(1)).get('k')", client)
        self.assertNotIn('rv-pw', html)
        self.assertNotIn('localStorage.setItem(linkKey', client)
        self.assertNotIn('SYNTHETIC-ONLY', html)
        self.assertIsNone(FORBIDDEN_RE.search(html))
        self.assertIsNone(TUNNEL_URL_RE.search(html))
        questions = json.loads(open(os.path.join(PAGE, 'questions.json'), encoding='utf-8').read())
        ids = [item['id'] for sec in questions for item in sec['items']]
        self.assertEqual(len(ids), 112)
        for qid in ids:
            self.assertIn(f'"id": "{qid}"', html)
        catalog = json.loads(open(os.path.join(GATEWAY, 'catalog_ids.json'), encoding='utf-8').read())
        self.assertEqual(catalog['ids'], ids)
        self.assertEqual(catalog['storageKey'], 'liuguang-checklist-20261109-v1')
        gateway = json.loads(open(os.path.join(PAGE, 'gateway.json'), encoding='utf-8').read())
        self.assertEqual(gateway, {'gateway': ''})
        same = open(os.path.join(GATEWAY, 'questions.json'), encoding='utf-8').read()
        self.assertEqual(same, open(os.path.join(PAGE, 'questions.json'), encoding='utf-8').read())

    def test_clear_draft_is_not_the_submit_path(self):
        html = open(os.path.join(PAGE, 'index.html'), encoding='utf-8').read()
        self.assertIn('本机草稿保留，没有清空', html)
        self.assertIn("localStorage.removeItem(PENDING_KEY)", html)
        self.assertEqual(html.count("localStorage.removeItem(STORAGE_KEY)"), 2)
        self.assertIn('确认清空本机全部草稿', html)


if __name__ == '__main__':
    unittest.main()
