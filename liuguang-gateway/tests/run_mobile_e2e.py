#!/usr/bin/env python3
"""Mobile-viewport browser trial against the local gateway. Not a physical phone and not production."""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY = os.path.abspath(os.path.join(HERE, '..'))
REPO = os.path.abspath(os.path.join(GATEWAY, '..'))
PAGE = os.path.join(REPO, 'liuguang')
sys.path.insert(0, GATEWAY)
import server  # noqa: E402


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Static(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):  # noqa: N802
        path = self.path.split('?', 1)[0]
        mapping = {
            '/review/liuguang': 'index.html',
            '/review/liuguang/': 'index.html',
            '/review/liuguang/index.html': 'index.html',
            '/review/liuguang/gateway.json': 'gateway.json',
            '/review/liuguang/questions.json': 'questions.json',
            '/review/liuguang/README.md': 'README.md',
        }
        name = mapping.get(path)
        if not name:
            body = b'not found'
            self.send_response(404)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        blob = open(os.path.join(PAGE, name), 'rb').read()
        kind = 'application/json' if name.endswith('.json') else 'text/html; charset=utf-8'
        if name.endswith('.md'):
            kind = 'text/plain; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(blob)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(blob)


def main() -> int:
    home = tempfile.mkdtemp(prefix='liuguang-e2e-')
    password = 'synthetic-' + os.urandom(12).hex()
    secret = os.path.join(home, 'secret')
    fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, password.encode())
    os.close(fd)
    root = os.path.join(home, 'answers')
    env = os.environ.copy()
    env.update({
        'LIUGUANG_ANSWERS_ROOT': root,
        'LIUGUANG_SUBMIT_PASSWORD_FILE': secret,
        'LIUGUANG_ALLOWED_ORIGINS': 'http://127.0.0.1',
        'LIUGUANG_BIND': '127.0.0.1',
        'LIUGUANG_PORT': '0',
        'LIUGUANG_KDF_ITERS': '1000',
        'LIUGUANG_ALLOW_TEST_KDF': '1',
        'PYTHONDONTWRITEBYTECODE': '1',
    })
    # The static origin includes a port. Fill it in after the static port is known.
    static_port = _free_port()
    origin = f'http://127.0.0.1:{static_port}'
    env['LIUGUANG_ALLOWED_ORIGINS'] = origin
    proc = subprocess.Popen(
        [sys.executable, os.path.join(GATEWAY, 'server.py')],
        cwd=GATEWAY, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line.startswith('LIUGUANG_READY'):
        proc.kill()
        sys.stderr.write('gateway failed to start\n')
        return 1
    api_port = int(line.strip().split('port=')[1])
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', static_port), Static)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    node_modules = os.path.join(HERE, 'node_modules')
    if not os.path.isdir(node_modules):
        install = subprocess.run(
            ['npm', 'install', '--omit=dev', '--no-audit', '--no-fund'],
            cwd=HERE, check=False, capture_output=True, text=True,
        )
        if install.returncode != 0:
            sys.stderr.write('npm install failed\n')
            proc.terminate()
            return 1
    expected = server._digest({'1.1': {'status': 'answered', 'text': 'SYNTHETIC-ONLY', 'parkUntil': ''}})
    base = f'http://127.0.0.1:{static_port}/review/liuguang/'
    gw = f'http://127.0.0.1:{api_port}'
    child_env = os.environ.copy()
    child_env.update({
        'LIUGUANG_TEST_PASSWORD': password,
        'PAGE_URL': f'{base}?gw={gw}&synthetic=1',
        'TIMEOUT_URL': f'{base}?gw={gw}&synthetic=1&submitTimeoutMs=500',
        'PLAIN_URL': base,
        'EXPECTED_DIGEST': expected,
        'CHROME_PATH': os.environ.get('CHROME_PATH', '/usr/bin/google-chrome'),
    })
    try:
        e2e = subprocess.run(
            ['node', os.path.join(HERE, 'mobile_e2e.mjs')],
            cwd=HERE, env=child_env, check=False, capture_output=True, text=True, timeout=120,
        )
        redacted = _redact(e2e.stdout + e2e.stderr, password)
        sys.stdout.write(redacted)
        if e2e.returncode != 0:
            return e2e.returncode
        read = subprocess.run(
            [sys.executable, os.path.join(GATEWAY, 'readback.py'), '--root', root, '--password-file', secret, '--json', '--check-perms'],
            cwd=GATEWAY,
            env={**env, 'LIUGUANG_KDF_ITERS': '1000', 'LIUGUANG_ALLOW_TEST_KDF': '1'},
            check=False, capture_output=True, text=True,
        )
        if read.returncode != 0:
            sys.stdout.write('FAIL readback\n')
            return 1
        records = json.loads(read.stdout)
        texts = sorted(
            f"{qid}:{ans.get('text')}"
            for rec in records for qid, ans in (rec.get('answers') or {}).items()
        )
        sys.stdout.write('READBACK count=%s synthetic=%s\n' % (
            len(records),
            all(rec.get('synthetic') for rec in records),
        ))
        sys.stdout.write('READBACK texts %s\n' % ' | '.join(texts))
        # Timeout retry must not duplicate that answer's receipt.
        timeout_hits = [rec for rec in records if any(a.get('text') == 'SYNTHETIC-ONLY-TIMEOUT' for a in rec.get('answers', {}).values())]
        if len(timeout_hits) != 1:
            sys.stdout.write('FAIL timeout-receipt-count %s\n' % len(timeout_hits))
            return 1
        if any(a.get('text') == 'SYNTHETIC-ONLY-BADACK' for rec in records for a in rec.get('answers', {}).values()):
            sys.stdout.write('FAIL invalid-ack-persisted\n')
            return 1
        if any(a.get('text') == 'SYNTHETIC-ONLY-OFFLINE' for rec in records for a in rec.get('answers', {}).values()):
            sys.stdout.write('FAIL unconfigured-persisted\n')
            return 1
        if any(a.get('text') == 'SYNTHETIC-ONLY-DENIED' for rec in records for a in rec.get('answers', {}).values()):
            sys.stdout.write('FAIL denied-persisted\n')
            return 1
        sys.stdout.write('MOBILE_READBACK_OK\n')
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        httpd.shutdown()
        shutil.rmtree(home, ignore_errors=True)


def _redact(text: str, password: str) -> str:
    return text.replace(password, '***')


if __name__ == '__main__':
    raise SystemExit(main())
