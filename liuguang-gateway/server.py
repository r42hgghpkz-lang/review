#!/usr/bin/env python3
"""Liuguang submission gateway.

The public page sends a password the author types. This process checks it
against a runtime file, then returns a random bearer token kept in
sessionStorage. It does not set a cookie, because Mobile Safari does not
reliably send cross-site cookies from GitHub Pages.

Nothing in this file is safe to publish on GitHub Pages. Run it only on the
private host. Acknowledgements are returned after the private package has
fsynced the receipt.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import crypto_box
import store

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_BODY = 256 * 1024
TEXT_MAX = 4000
SESSION_HOURS = 12
AUTH_FAIL_LIMIT = 12
AUTH_FAIL_WINDOW = 15 * 60
IDEM_RE = re.compile(r'^[A-Za-z0-9_-]{16,80}$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
STORAGE_KEY = store.STORAGE_KEY


def _read_secret(path: str) -> str:
    if not path or not os.path.isabs(path):
        raise SystemExit('secret file path must be absolute')
    try:
        st = os.lstat(path)
    except OSError:
        raise SystemExit('secret file is missing')
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise SystemExit('secret file must be a regular file, not a symlink')
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise SystemExit('secret file mode must be 0600')
    with open(path, encoding='utf-8') as fh:
        value = fh.read().strip('\n')
    if value.endswith('\r'):
        value = value[:-1]
    if not 8 <= len(value) <= 256:
        raise SystemExit('secret file must contain 8 to 256 characters')
    return value


def _load_catalog() -> set[str]:
    doc = json.loads(open(os.path.join(HERE, 'catalog_ids.json'), encoding='utf-8').read())
    if doc.get('storageKey') != STORAGE_KEY or doc.get('count') != 112:
        raise SystemExit('catalog')
    ids = doc['ids']
    if not isinstance(ids, list) or len(ids) != 112 or len(set(ids)) != 112:
        raise SystemExit('catalog')
    return set(ids)


def _password_ok(got: object, expected: str) -> bool:
    if not isinstance(got, str):
        return False
    raw = got.encode('utf-8', 'replace')[:1024]
    return hmac.compare_digest(hashlib.sha256(raw).digest(), hashlib.sha256(expected.encode('utf-8')).digest())


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp(now: datetime.datetime) -> str:
    return now.strftime('%Y-%m-%dT%H:%M:%SZ')


def _valid_date(value: str) -> bool:
    if value == '':
        return True
    if not DATE_RE.fullmatch(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _normalize(body: dict, catalog: set[str]) -> tuple[dict | None, str | None]:
    allowed = {'v', 'kind', 'storageKey', 'answers', 'synthetic'}
    if set(body) - allowed:
        return None, 'invalid'
    if body.get('v') != 1 or body.get('kind') != 'liuguang-checklist':
        return None, 'invalid'
    if body.get('storageKey') != STORAGE_KEY:
        return None, 'bad-storage-key'
    if 'synthetic' in body and body['synthetic'] is not True:
        return None, 'invalid'
    answers = body.get('answers')
    if not isinstance(answers, dict) or len(answers) > 112:
        return None, 'invalid'
    if not answers:
        return None, 'empty'
    norm = {}
    for qid, raw in answers.items():
        if qid not in catalog or not isinstance(raw, dict):
            return None, 'unknown-question'
        status = raw.get('status')
        text = raw.get('text', '')
        park = raw.get('parkUntil', '')
        if status not in ('open', 'answered', 'parked'):
            return None, 'bad-status'
        if not isinstance(text, str) or len(text) > TEXT_MAX or '\x00' in text:
            return None, 'text-too-long' if isinstance(text, str) and len(text) > TEXT_MAX else 'invalid'
        if not isinstance(park, str) or not _valid_date(park):
            return None, 'bad-date'
        norm[qid] = {'status': status, 'text': text, 'parkUntil': park}
    norm = {
        qid: item for qid, item in norm.items()
        if item['status'] != 'open' or item['text'].strip() or item['parkUntil']
    }
    if not norm:
        return None, 'empty'
    return norm, None


def _digest(answers: dict) -> str:
    canon = json.dumps(answers, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return 'sha256:' + hashlib.sha256(canon).hexdigest()


def _counts(answers: dict) -> dict:
    return {
        'submitted': len(answers),
        'answered': sum(1 for a in answers.values() if a['status'] == 'answered'),
        'parked': sum(1 for a in answers.values() if a['status'] == 'parked'),
        'open': sum(1 for a in answers.values() if a['status'] == 'open'),
    }


class Gateway:
    def __init__(self, package: store.Package, password: str, origins: set[str], catalog: set[str]):
        self.package = package
        self.password = password
        self.origins = origins
        self.catalog = catalog
        self._fail_lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}

    def origin_ok(self, origin: str | None) -> bool:
        return bool(origin) and origin in self.origins

    def too_many_failures(self, ip: str) -> bool:
        now = _now().timestamp()
        with self._fail_lock:
            recent = [t for t in self._fails.get(ip, []) if now - t < AUTH_FAIL_WINDOW]
            self._fails[ip] = recent
            return len(recent) >= AUTH_FAIL_LIMIT

    def note_failure(self, ip: str) -> None:
        now = _now().timestamp()
        with self._fail_lock:
            recent = [t for t in self._fails.get(ip, []) if now - t < AUTH_FAIL_WINDOW]
            recent.append(now)
            self._fails[ip] = recent

    def clear_failures(self, ip: str) -> None:
        with self._fail_lock:
            self._fails.pop(ip, None)


def _cors(origin: str) -> dict[str, str]:
    return {
        'Access-Control-Allow-Origin': origin,
        'Access-Control-Allow-Methods': 'POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Authorization, Content-Type, Idempotency-Key',
        'Access-Control-Max-Age': '600',
        'Vary': 'Origin',
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    gateway: Gateway

    def log_message(self, fmt: str, *args) -> None:
        try:
            code = args[1] if len(args) > 1 else '-'
        except Exception:
            code = '-'
        path = self.path.split('?', 1)[0].replace('\n', '').replace('\r', '')[:200]
        sys.stderr.write(f'{_stamp(_now())} {self.command} {path} {code}\n')

    def _send(self, code: int, obj: dict, origin: str | None = None, extra: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Robots-Tag', 'noindex')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Connection', 'close')
        if origin and self.gateway.origin_ok(origin):
            for k, v in _cors(origin).items():
                self.send_header(k, v)
        if extra:
            for k, v in extra.items():
                if k.lower() == 'set-cookie':
                    continue
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _origin(self) -> str | None:
        value = self.headers.get('Origin')
        if value is None:
            return None
        return value.strip()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split('?', 1)[0]
        if path == '/v1/health':
            self._send(200, {
                'ok': True,
                'service': 'liuguang-submit',
                'auth': 'bearer-session',
                'cookie': False,
                'readback': 'cli-only',
            })
            return
        # No HTTP readback of receipts, sessions, or answers.
        self._send(404, {'ok': False, 'error': 'not-found', 'persisted': False})

    def do_OPTIONS(self) -> None:  # noqa: N802
        origin = self._origin()
        path = self.path.split('?', 1)[0]
        if not self.gateway.origin_ok(origin):
            self._send(403, {'ok': False, 'error': 'origin', 'persisted': False})
            return
        if path not in ('/v1/session', '/v1/submissions'):
            self._send(404, {'ok': False, 'error': 'not-found', 'persisted': False}, origin)
            return
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        for k, v in _cors(origin or '').items():
            self.send_header(k, v)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        origin = self._origin()
        path = self.path.split('?', 1)[0]
        if not self.gateway.origin_ok(origin):
            self._send(403, {'ok': False, 'error': 'origin', 'persisted': False})
            return
        if path == '/v1/session':
            self._session(origin or '')
            return
        if path == '/v1/submissions':
            self._submit(origin or '')
            return
        self._send(404, {'ok': False, 'error': 'not-found', 'persisted': False}, origin)

    def _read_body(self) -> tuple[dict | None, str | None]:
        if self.headers.get('Transfer-Encoding'):
            return None, 'too-large'
        ctype = (self.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower()
        if ctype != 'application/json':
            return None, 'invalid'
        raw_len = self.headers.get('Content-Length')
        if raw_len is None:
            return None, 'bad-json'
        try:
            n = int(raw_len)
        except ValueError:
            return None, 'bad-json'
        if n < 0 or n > MAX_BODY:
            return None, 'too-large'
        data = self.rfile.read(n) if n else b''
        if len(data) != n:
            return None, 'bad-json'
        try:
            obj = json.loads(data.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError):
            return None, 'bad-json'
        if not isinstance(obj, dict):
            return None, 'bad-json'
        return obj, None

    def _bearer(self) -> str | None:
        header = self.headers.get('Authorization') or ''
        if not header.startswith('Bearer '):
            return None
        token = header[7:].strip()
        if not token or len(token) > 200 or '\n' in token or '\r' in token:
            return None
        return token

    def _session(self, origin: str) -> None:
        ip = self.client_address[0]
        if self.gateway.too_many_failures(ip):
            self._send(429, {'ok': False, 'error': 'unauthorized', 'persisted': False}, origin)
            return
        body, err = self._read_body()
        if err or body is None:
            self._send(400, {'ok': False, 'error': err or 'bad-json', 'persisted': False}, origin)
            return
        if not _password_ok(body.get('password'), self.gateway.password):
            self.gateway.note_failure(ip)
            self._send(401, {'ok': False, 'error': 'unauthorized', 'persisted': False}, origin)
            return
        self.gateway.clear_failures(ip)
        token = secrets.token_urlsafe(32)
        exp = _stamp(_now() + datetime.timedelta(hours=SESSION_HOURS))
        self.gateway.package.put_session(token, exp)
        self._send(200, {'ok': True, 'token': token, 'expiresAt': exp, 'cookie': False}, origin)

    def _submit(self, origin: str) -> None:
        token = self._bearer()
        if not token or not self.gateway.package.session_ok(token, _now()):
            self._send(401, {'ok': False, 'error': 'unauthorized', 'persisted': False}, origin)
            return
        idem = (self.headers.get('Idempotency-Key') or '').strip()
        if not IDEM_RE.fullmatch(idem):
            self._send(400, {'ok': False, 'error': 'bad-key', 'persisted': False}, origin)
            return
        body, err = self._read_body()
        if err or body is None:
            code = 413 if err == 'too-large' else 400
            self._send(code, {'ok': False, 'error': err or 'bad-json', 'persisted': False}, origin)
            return
        norm, why = _normalize(body, self.gateway.catalog)
        if why or norm is None:
            self._send(400, {'ok': False, 'error': why or 'invalid', 'persisted': False}, origin)
            return
        now = _now()
        receipt = {
            'v': 1,
            'kind': 'liuguang-checklist',
            'storageKey': STORAGE_KEY,
            'catalog': store.CATALOG_ID,
            'receiptId': f"lg-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}",
            'idempotencyKey': idem,
            'storedAt': _stamp(now),
            'digest': _digest(norm),
            'origin': origin,
            'answers': norm,
            'counts': _counts(norm),
            'synthetic': body.get('synthetic') is True,
        }
        try:
            result = self.gateway.package.commit(receipt)
        except store.StoreError:
            self._send(500, {'ok': False, 'error': 'persist', 'persisted': False}, origin)
            return
        stored = result.receipt
        # Ack only with fields that were read back from the durable record.
        self._send(200, {
            'ok': True,
            'persisted': True,
            'replay': result.replay,
            'receiptId': stored['receiptId'],
            'idempotencyKey': stored['idempotencyKey'],
            'digest': stored['digest'],
            'storedAt': stored['storedAt'],
            'counts': stored.get('counts') or {},
        }, origin)


def load_gateway() -> tuple[Gateway, str, int]:
    os.umask(0o077)
    root = os.environ.get('LIUGUANG_ANSWERS_ROOT', '/workspace/tmp/liuguang-design-20261109/answers')
    password_file = os.environ.get('LIUGUANG_SUBMIT_PASSWORD_FILE', '')
    if not password_file:
        raise SystemExit('LIUGUANG_SUBMIT_PASSWORD_FILE is required')
    data_file = os.environ.get('LIUGUANG_DATA_KEY_FILE', '') or password_file
    password = _read_secret(password_file)
    data_secret = password if data_file == password_file else _read_secret(data_file)
    origins = {
        item.strip()
        for item in os.environ.get(
            'LIUGUANG_ALLOWED_ORIGINS',
            'https://r42hgghpkz-lang.github.io',
        ).split(',')
        if item.strip()
    }
    if not origins:
        raise SystemExit('LIUGUANG_ALLOWED_ORIGINS is empty')
    bind = os.environ.get('LIUGUANG_BIND', '127.0.0.1')
    port = int(os.environ.get('LIUGUANG_PORT', '8791'))
    iters = store.kdf_iters_from_env()
    if iters != crypto_box.PRODUCTION_KDF_ITERS:
        sys.stderr.write('kdf iterations are below the review baseline (test opt-in)\n')
    resolved = store.prepare_root(root, HERE)
    package = store.Package(resolved, data_secret, iters)
    if bind not in ('127.0.0.1', '::1', 'localhost'):
        sys.stderr.write('bind is not loopback; terminate TLS and the public route in front of this process\n')
    return Gateway(package, password, origins, _load_catalog()), bind, port


def main() -> None:
    gateway, bind, port = load_gateway()
    Handler.gateway = gateway

    class Server(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = Server((bind, port), Handler)
    host, bound = httpd.server_address[:2]
    sys.stdout.write(f'LIUGUANG_READY bind={host} port={bound}\n')
    sys.stdout.flush()
    httpd.serve_forever()


if __name__ == '__main__':
    main()
