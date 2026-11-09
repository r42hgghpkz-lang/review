#!/usr/bin/env python3
"""Browser proof for #k= auth. Prints no secret."""
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
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY = os.path.abspath(os.path.join(HERE, '..'))
PAGE = os.path.abspath(os.path.join(GATEWAY, '..', 'liuguang'))


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
        name = {
            '/review/liuguang': 'index.html',
            '/review/liuguang/': 'index.html',
            '/review/liuguang/index.html': 'index.html',
            '/review/liuguang/gateway.json': 'gateway.json',
            '/review/liuguang/questions.json': 'questions.json',
        }.get(path)
        if not name:
            body = b'not found'
            self.send_response(404)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        blob = open(os.path.join(PAGE, name), 'rb').read()
        kind = 'application/json' if name.endswith('.json') else 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(blob)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(blob)


def main() -> int:
    home = tempfile.mkdtemp(prefix='liuguang-link-')
    password = 'synthetic-' + os.urandom(12).hex()
    secret = os.path.join(home, 'secret')
    fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, password.encode())
    os.close(fd)
    root = os.path.join(home, 'answers')
    static_port = _free_port()
    origin = f'http://127.0.0.1:{static_port}'
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
    proc = subprocess.Popen(
        [sys.executable, os.path.join(GATEWAY, 'server.py')],
        cwd=GATEWAY, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line.startswith('LIUGUANG_READY'):
        proc.kill()
        shutil.rmtree(home, ignore_errors=True)
        sys.stdout.write('gateway failed\n')
        return 1
    api_port = int(line.strip().split('port=')[1])
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', static_port), Static)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        child = os.environ.copy()
        child['LIUGUANG_TEST_PASSWORD'] = password
        gw = 'http://127.0.0.1:%s' % api_port
        child['PAGE_BASE'] = origin + '/review/liuguang/?synthetic=1&gw=' + quote(gw, safe='')
        child['CHROME_PATH'] = os.environ.get('CHROME_PATH', '/usr/bin/google-chrome')
        e2e = subprocess.run(
            ['node', os.path.join(HERE, 'link_fragment.mjs')],
            cwd=HERE, env=child, check=False, capture_output=True, text=True, timeout=90,
        )
        sys.stdout.write(e2e.stdout.replace(password, '***'))
        if e2e.returncode != 0:
            sys.stdout.write(e2e.stderr.replace(password, '***')[-600:])
            return e2e.returncode
        read = subprocess.run(
            [sys.executable, os.path.join(GATEWAY, 'readback.py'), '--root', root, '--password-file', secret, '--json', '--check-perms'],
            cwd=GATEWAY, env=env, check=False, capture_output=True, text=True,
        )
        if read.returncode != 0 or password in read.stdout:
            sys.stdout.write('FAIL readback\n')
            return 1
        records = json.loads(read.stdout)
        texts = [a.get('text') for rec in records for a in rec.get('answers', {}).values()]
        link_hits = [rec for rec in records if any(a.get('text') == 'SYNTHETIC-ONLY-LINK' for a in rec.get('answers', {}).values())]
        sys.stdout.write('READBACK count=%s link_receipts=%s synthetic=%s\n' % (
            len(records), len(link_hits), all(rec.get('synthetic') for rec in records)))
        if len(link_hits) != 1 or 'SYNTHETIC-ONLY-LINK' not in texts or password in ''.join(texts):
            sys.stdout.write('FAIL readback-shape\n')
            return 1
        sys.stdout.write('LINK_READBACK_OK\n')
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        httpd.shutdown()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
