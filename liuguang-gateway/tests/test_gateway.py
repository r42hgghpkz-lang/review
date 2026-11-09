#!/usr/bin/env python3
"""Gateway protocol tests. Passwords and tokens are generated at runtime and never printed."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed

GATEWAY = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
REPO = os.path.abspath(os.path.join(GATEWAY, '..'))
sys.path.insert(0, GATEWAY)

import crypto_box  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

ORIGIN = 'https://r42hgghpkz-lang.github.io'
STORAGE = 'liuguang-checklist-20261109-v1'


def _secret(value: str, path: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, value.encode())
    os.close(fd)
    os.chmod(path, 0o600)


class Proc(unittest.TestCase):
    port = 0
    proc = None
    root = ''
    home = ''
    password = ''
    stderr: list[str] = []

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix='liuguang-gw-')
        cls.root = os.path.join(cls.home, 'answers')
        os.makedirs(cls.root, 0o755)
        os.chmod(cls.root, 0o755)
        loose = os.path.join(cls.root, 'preexisting.txt')
        open(loose, 'w', encoding='utf-8').write('not-an-answer')
        os.chmod(loose, 0o644)
        cls.password = 'synthetic-' + os.urandom(12).hex()
        _secret(cls.password, os.path.join(cls.home, 'secret'))
        cls.proc, cls.port, cls.stderr = _spawn(cls.root, os.path.join(cls.home, 'secret'), ORIGIN)

    @classmethod
    def tearDownClass(cls):
        if cls.proc and cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
        shutil.rmtree(cls.home, ignore_errors=True)

    def api(self, method, path, body=None, headers=None, origin=ORIGIN):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        hdrs = {}
        if origin is not None:
            hdrs['Origin'] = origin
        if headers:
            hdrs.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault('Content-Type', 'application/json')
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        header_map = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, header_map, raw

    def login(self, password=None, origin=ORIGIN):
        status, hdrs, raw = self.api('POST', '/v1/session', {'password': password if password is not None else self.password}, origin=origin)
        self.assertNotIn('set-cookie', hdrs)
        if status != 200:
            raise AssertionError(f'session status {status}')
        ack = json.loads(raw)
        if not ack.get('ok') or not ack.get('token') or ack.get('cookie') is not False:
            raise AssertionError('session ack rejected')
        if self.password and self.password in raw.decode():
            raise AssertionError('password echoed')
        return ack['token']

    def submit(self, token, key, answers, synthetic=True):
        body = {'v': 1, 'kind': 'liuguang-checklist', 'storageKey': STORAGE, 'answers': answers}
        if synthetic:
            body['synthetic'] = True
        return self.api('POST', '/v1/submissions', body, {
            'Authorization': 'Bearer ' + token,
            'Idempotency-Key': key,
        })


class ProtocolTest(Proc):
    def test_directories_tightened_from_0755(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.root, 'preexisting.txt')).st_mode), 0o600)
        marker = json.loads(open(os.path.join(self.root, 'PACKAGE.json'), encoding='utf-8').read())
        self.assertEqual(marker['package'], 'liuguang')
        self.assertEqual(marker['isolatedFrom'], 'wenshu')

    def test_health_and_cookie_flag(self):
        status, hdrs, raw = self.api('GET', '/v1/health', origin=None)
        self.assertEqual(status, 200)
        self.assertNotIn('set-cookie', hdrs)
        body = json.loads(raw)
        self.assertEqual(body['auth'], 'bearer-session')
        self.assertFalse(body['cookie'])
        self.assertEqual(body['readback'], 'cli-only')

    def test_ack_then_durable_readback(self):
        token = self.login()
        text = 'SYNTHETIC-ONLY-ALPHA'
        answers = {'1.1': {'status': 'answered', 'text': text, 'parkUntil': ''},
                   '2.1': {'status': 'parked', 'text': 'SYNTHETIC-ONLY-KEEP', 'parkUntil': '2026-12-01'}}
        status, hdrs, raw = self.submit(token, 'synthetic-ack-key-0001', answers)
        self.assertEqual(status, 200, raw)
        self.assertNotIn('set-cookie', hdrs)
        ack = json.loads(raw)
        self.assertTrue(ack['ok'] and ack['persisted'] is True and ack['replay'] is False)
        self.assertTrue(ack['receiptId'].startswith('lg-'))
        self.assertEqual(ack['idempotencyKey'], 'synthetic-ack-key-0001')
        self.assertEqual(ack['digest'], server._digest(answers))
        self.assertNotIn(text.encode(), raw)
        out = _readback(self.root, os.path.join(self.home, 'secret'), '--latest', '--json', '--check-perms')
        rec = json.loads(out)[0]
        self.assertEqual(rec['receiptId'], ack['receiptId'])
        self.assertEqual(rec['answers']['1.1']['text'], text)
        self.assertTrue(rec['synthetic'])
        self.assertEqual(rec['storageKey'], STORAGE)
        modes = [stat.S_IMODE(os.stat(os.path.join(self.root, 'receipts', name)).st_mode)
                 for name in os.listdir(os.path.join(self.root, 'receipts'))]
        self.assertTrue(modes)
        self.assertTrue(all(mode == 0o600 for mode in modes))

    def test_duplicate_retry_keeps_one_receipt(self):
        token = self.login()
        key = 'synthetic-retry-key-01'
        first = {'1.1': {'status': 'answered', 'text': 'SYNTHETIC-ONLY-FIRST', 'parkUntil': ''}}
        second = {'1.1': {'status': 'answered', 'text': 'SYNTHETIC-ONLY-SECOND', 'parkUntil': ''}}
        status, _, raw = self.submit(token, key, first)
        self.assertEqual(status, 200)
        ack1 = json.loads(raw)
        before = open(os.path.join(self.root, 'receipts', ack1['receiptId'] + '.bin'), 'rb').read()
        status, _, raw = self.submit(token, key, second)
        ack2 = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertTrue(ack2['replay'])
        self.assertEqual(ack2['receiptId'], ack1['receiptId'])
        self.assertEqual(ack2['digest'], ack1['digest'])
        self.assertNotEqual(ack2['digest'], server._digest(second))
        after = open(os.path.join(self.root, 'receipts', ack1['receiptId'] + '.bin'), 'rb').read()
        self.assertEqual(before, after)
        rec = json.loads(_readback(self.root, os.path.join(self.home, 'secret'), '--id', ack1['receiptId'], '--json'))[0]
        self.assertEqual(rec['answers']['1.1']['text'], 'SYNTHETIC-ONLY-FIRST')

    def test_concurrent_same_key_one_receipt(self):
        token = self.login()
        key = 'synthetic-concurrent-01'
        answers = {'10.6': {'status': 'answered', 'text': 'SYNTHETIC-ONLY-RACE', 'parkUntil': ''}}
        barrier = threading.Barrier(8)

        def once():
            barrier.wait(timeout=5)
            status, _, raw = self.submit(token, key, answers)
            return status, json.loads(raw)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = [item.result(timeout=20) for item in as_completed([pool.submit(once) for _ in range(8)])]
        ids = {ack['receiptId'] for status, ack in results if status == 200 and ack.get('persisted')}
        self.assertEqual(len(ids), 1)
        receipt_id = next(iter(ids))
        names = os.listdir(os.path.join(self.root, 'receipts'))
        self.assertEqual(names.count(receipt_id + '.bin'), 1)

    def test_invalid_auth_and_read_protection(self):
        before = set(os.listdir(os.path.join(self.root, 'receipts'))) if os.path.isdir(os.path.join(self.root, 'receipts')) else set()
        status, hdrs, raw = self.api('POST', '/v1/session', {'password': 'not-the-secret'})
        self.assertEqual(status, 401)
        self.assertNotIn('set-cookie', hdrs)
        self.assertNotIn(b'not-the-secret', raw)
        status, _, raw = self.api('POST', '/v1/submissions', {'v': 1}, headers={'Authorization': 'Bearer not-a-session'})
        self.assertEqual(status, 401)
        token = self.login()
        for path in ('/v1/submissions', '/v1/receipts', '/v1/receipts/latest', '/answers', '/receipts/lg-x.bin'):
            status, _, raw = self.api('GET', path, headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(status, 404, path)
            self.assertNotIn(b'SYNTHETIC-ONLY', raw)
        status, hdrs, raw = self.api('GET', '/v1/health')
        self.assertNotIn(b'SYNTHETIC', raw)
        receipt_dir = os.path.join(self.root, 'receipts')
        after = set(os.listdir(receipt_dir)) if os.path.isdir(receipt_dir) else set()
        self.assertEqual(before, after)

    def test_origin_and_preflight(self):
        status, hdrs, _ = self.api('OPTIONS', '/v1/submissions', origin=ORIGIN)
        self.assertEqual(status, 204)
        self.assertEqual(hdrs.get('access-control-allow-origin'), ORIGIN)
        self.assertNotIn('access-control-allow-credentials', hdrs)
        allowed_headers = hdrs.get('access-control-allow-headers', '').lower()
        self.assertIn('authorization', allowed_headers)
        self.assertIn('idempotency-key', allowed_headers)
        self.assertNotIn('*', hdrs.get('access-control-allow-origin', ''))
        status, hdrs, _ = self.api('POST', '/v1/session', {'password': self.password}, origin='https://evil.example')
        self.assertEqual(status, 403)
        self.assertNotIn('access-control-allow-origin', hdrs)
        status, _, _ = self.api('POST', '/v1/session', {'password': self.password}, origin=None)
        self.assertEqual(status, 403)
        status, hdrs, _ = self.api('OPTIONS', '/v1/session', origin='https://r42hgghpkz-lang.github.io.evil.example')
        self.assertEqual(status, 403)
        self.assertNotEqual(hdrs.get('access-control-allow-origin'), 'https://r42hgghpkz-lang.github.io.evil.example')

    def test_validation_does_not_persist(self):
        token = self.login()
        before = os.listdir(os.path.join(self.root, 'receipts'))
        cases = [
            ('synthetic-bad-qid-0001', {'99.9': {'status': 'answered', 'text': 'SYNTHETIC-ONLY-NO', 'parkUntil': ''}}),
            ('synthetic-bad-date-001', {'1.2': {'status': 'parked', 'text': 'SYNTHETIC-ONLY-NO', 'parkUntil': 'tomorrow'}}),
            ('synthetic-empty-key-01', {}),
        ]
        for key, answers in cases:
            body_answers = answers or {'1.1': {'status': 'open', 'text': '', 'parkUntil': ''}}
            if answers == {}:
                status, _, raw = self.submit(token, key, {'1.1': {'status': 'open', 'text': '', 'parkUntil': ''}})
            else:
                status, _, raw = self.submit(token, key, body_answers)
            self.assertEqual(status, 400, raw)
            self.assertFalse(json.loads(raw)['persisted'])
        # truly empty object
        status, _, raw = self.api('POST', '/v1/submissions', {
            'v': 1, 'kind': 'liuguang-checklist', 'storageKey': STORAGE, 'answers': {},
        }, {'Authorization': 'Bearer ' + token, 'Idempotency-Key': 'synthetic-empty-obj-01'})
        self.assertEqual(status, 400)
        self.assertEqual(os.listdir(os.path.join(self.root, 'receipts')), before)

    def test_source_has_no_chapter_issue_token(self):
        blob = ''
        for name in os.listdir(GATEWAY):
            if name.endswith('.py'):
                blob += open(os.path.join(GATEWAY, name), encoding='utf-8').read()
        for banned in ('issueTok', 'REVIEW_ISSUES_TOKEN', 'api.github.com', '/workspace/reviewer/.password'):
            self.assertNotIn(banned, blob)


class GuardTest(unittest.TestCase):
    def test_refuses_git_wenshu_and_source_tree(self):
        home = tempfile.mkdtemp(prefix='liuguang-guard-')
        try:
            password = 'synthetic-' + os.urandom(8).hex()
            secret = os.path.join(home, 'secret')
            _secret(password, secret)
            git_root = os.path.join(home, 'repo')
            os.makedirs(os.path.join(git_root, '.git'))
            code = _spawn_expect_fail(os.path.join(git_root, 'answers'), secret, password)
            self.assertNotEqual(code, 0)
            wenshu = os.path.join(home, 'wenshu', 'answers')
            code = _spawn_expect_fail(wenshu, secret, password)
            self.assertNotEqual(code, 0)
            inside = os.path.join(GATEWAY, 'answers-must-not-exist')
            code = _spawn_expect_fail(inside, secret, password)
            self.assertNotEqual(code, 0)
            self.assertFalse(os.path.exists(inside))
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_kdf_baseline_and_roundtrip(self):
        env = os.environ.copy()
        env.pop('LIUGUANG_KDF_ITERS', None)
        env.pop('LIUGUANG_ALLOW_TEST_KDF', None)
        saved = dict(os.environ)
        try:
            os.environ.pop('LIUGUANG_KDF_ITERS', None)
            os.environ.pop('LIUGUANG_ALLOW_TEST_KDF', None)
            self.assertEqual(store.kdf_iters_from_env(), crypto_box.PRODUCTION_KDF_ITERS)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        secret = 'synthetic-kdf-secret'
        salt = b'0123456789abcdef'
        blob = crypto_box.encrypt(secret, b'{"synthetic":true}', salt=salt, iters=crypto_box.PRODUCTION_KDF_ITERS)
        self.assertEqual(crypto_box.decrypt(secret, blob, iters=crypto_box.PRODUCTION_KDF_ITERS), b'{"synthetic":true}')
        raw = __import__('base64').b64decode(blob)
        self.assertEqual(raw[:16], salt)
        self.assertEqual(len(raw[16:28]), 12)

    def test_low_kdf_refused_without_test_opt_in(self):
        home = tempfile.mkdtemp(prefix='liuguang-kdf-')
        try:
            secret = os.path.join(home, 'secret')
            _secret('synthetic-low-kdf', secret)
            proc = subprocess.run(
                [sys.executable, os.path.join(GATEWAY, 'server.py')],
                cwd=GATEWAY,
                env=_env(os.path.join(home, 'answers'), secret, ORIGIN, extra={
                    'LIUGUANG_KDF_ITERS': '1000',
                    'LIUGUANG_ALLOW_TEST_KDF': '',
                }),
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('baseline', proc.stderr)
            self.assertNotIn('synthetic-low-kdf', proc.stderr)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_rate_limit_blocks_password_guessing(self):
        home = tempfile.mkdtemp(prefix='liuguang-rate-')
        proc = None
        try:
            password = 'synthetic-' + os.urandom(8).hex()
            secret = os.path.join(home, 'secret')
            _secret(password, secret)
            root = os.path.join(home, 'answers')
            proc, port, _ = _spawn(root, secret, ORIGIN)
            conn_status = []
            for _i in range(server.AUTH_FAIL_LIMIT):
                status, _, raw = _post(port, '/v1/session', {'password': 'nope'}, ORIGIN)
                conn_status.append(status)
                self.assertNotIn(password.encode(), raw)
            self.assertTrue(all(code == 401 for code in conn_status))
            status, _, raw = _post(port, '/v1/session', {'password': password}, ORIGIN)
            self.assertEqual(status, 429)
            self.assertNotIn(password.encode(), raw)
            self.assertFalse(os.path.isdir(os.path.join(root, 'receipts')) and os.listdir(os.path.join(root, 'receipts')))
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            shutil.rmtree(home, ignore_errors=True)


def _env(root, secret, origin, extra=None):
    env = os.environ.copy()
    env.update({
        'LIUGUANG_ANSWERS_ROOT': root,
        'LIUGUANG_SUBMIT_PASSWORD_FILE': secret,
        'LIUGUANG_ALLOWED_ORIGINS': origin,
        'LIUGUANG_BIND': '127.0.0.1',
        'LIUGUANG_PORT': '0',
        'LIUGUANG_KDF_ITERS': '1000',
        'LIUGUANG_ALLOW_TEST_KDF': '1',
        'PYTHONDONTWRITEBYTECODE': '1',
    })
    if extra:
        env.update(extra)
    return env


def _spawn(root, secret, origin):
    proc = subprocess.Popen(
        [sys.executable, os.path.join(GATEWAY, 'server.py')],
        cwd=GATEWAY,
        env=_env(root, secret, origin),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    bucket: list[str] = []

    def drain():
        assert proc.stderr is not None
        for line in proc.stderr:
            bucket.append(line)

    threading.Thread(target=drain, daemon=True).start()
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line.startswith('LIUGUANG_READY'):
        proc.kill()
        time.sleep(0.2)
        detail = ''.join(bucket)[-800:]
        raise AssertionError('server did not become ready ' + detail)
    port = int(line.strip().split('port=')[1])
    return proc, port, bucket


def _spawn_expect_fail(root, secret, password):
    proc = subprocess.run(
        [sys.executable, os.path.join(GATEWAY, 'server.py')],
        cwd=GATEWAY,
        env=_env(root, secret, ORIGIN),
        capture_output=True, text=True, timeout=10,
    )
    if password in proc.stderr or password in proc.stdout:
        raise AssertionError('secret appeared in guard output')
    return proc.returncode


def _post(port, path, body, origin):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    data = json.dumps(body).encode()
    conn.request('POST', path, body=data, headers={
        'Content-Type': 'application/json',
        'Origin': origin,
        'Content-Length': str(len(data)),
    })
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw


def _readback(root, secret, *args):
    env = os.environ.copy()
    env['LIUGUANG_KDF_ITERS'] = '1000'
    env['LIUGUANG_ALLOW_TEST_KDF'] = '1'
    proc = subprocess.run(
        [sys.executable, os.path.join(GATEWAY, 'readback.py'), '--root', root, '--password-file', secret, *args],
        cwd=GATEWAY, env=env, capture_output=True, text=True, timeout=15, check=False,
    )
    if proc.returncode != 0:
        raise AssertionError('readback failed ' + proc.stderr[-400:])
    return proc.stdout


if __name__ == '__main__':
    unittest.main(verbosity=2)
