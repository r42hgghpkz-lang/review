#!/usr/bin/env python3
"""Ephemeral Cloudflare Quick Tunnel trial.

This is not a stable route and not a production deployment. The URL is
discovered at runtime, used once, then the process is stopped. Nothing is
written into gateway.json.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY = os.path.abspath(os.path.join(HERE, '..'))
REPO = os.path.abspath(os.path.join(GATEWAY, '..'))
PAGE = os.path.join(REPO, 'liuguang')
URL_RE = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')
sys.path.insert(0, GATEWAY)


class Static(object):
    pass


def _free_port():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _static_server(port):
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
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
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if name.endswith('.html') else 'application/json')
            self.send_header('Content-Length', str(len(blob)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(blob)

    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _cloudflared():
    candidates = [
        os.environ.get('LIUGUANG_CLOUDFLARED', ''),
        '/workspace/reviewer/cloudflared',
        '/tmp/cloudflared',
        shutil.which('cloudflared') or '',
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    dest = '/tmp/cloudflared'
    url = 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64'
    proc = subprocess.run(['curl', '-fsSL', '-o', dest, url], check=False, capture_output=True, text=True, timeout=90)
    if proc.returncode != 0 or not os.path.isfile(dest) or os.path.getsize(dest) < 1000000:
        sys.stdout.write('QUICK_TUNNEL_UNAVAILABLE download-failed\n')
        return ''
    os.chmod(dest, 0o755)
    return dest


def _post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST', headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, raw


def main() -> int:
    binary = _cloudflared()
    if not binary:
        return 2
    home = tempfile.mkdtemp(prefix='liuguang-tunnel-')
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
        'PYTHONDONTWRITEBYTECODE': '1',
    })
    env.pop('LIUGUANG_KDF_ITERS', None)
    env.pop('LIUGUANG_ALLOW_TEST_KDF', None)
    gateway = subprocess.Popen(
        [sys.executable, os.path.join(GATEWAY, 'server.py')],
        cwd=GATEWAY, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    assert gateway.stdout is not None
    ready = gateway.stdout.readline()
    if not ready.startswith('LIUGUANG_READY'):
        gateway.kill()
        shutil.rmtree(home, ignore_errors=True)
        sys.stdout.write('QUICK_TUNNEL_UNAVAILABLE gateway-failed\n')
        return 2
    api_port = int(ready.strip().split('port=')[1])
    httpd = _static_server(static_port)
    tunnel = subprocess.Popen(
        [binary, 'tunnel', '--url', f'http://127.0.0.1:{api_port}', '--no-autoupdate'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    public = ''
    deadline = time.time() + 40
    log = []
    assert tunnel.stdout is not None

    def drain():
        for line in tunnel.stdout:
            log.append(line)

    threading.Thread(target=drain, daemon=True).start()
    while time.time() < deadline and not public:
        text = ''.join(log)
        match = URL_RE.search(text)
        if match:
            public = match.group(0)
            break
        time.sleep(0.2)
    if not public:
        tunnel.terminate()
        gateway.terminate()
        httpd.shutdown()
        shutil.rmtree(home, ignore_errors=True)
        sys.stdout.write('QUICK_TUNNEL_UNAVAILABLE no-url\n')
        sys.stdout.write(''.join(log)[-1500:])
        return 2
    sys.stdout.write('TUNNEL_HOST ' + public + '\n')
    try:
        health_body = b''
        health_status = 0
        last_error = ''
        health_deadline = time.time() + 45
        while time.time() < health_deadline:
            try:
                health = urllib.request.urlopen(public + '/v1/health', timeout=10)
                health_body = health.read()
                health_status = health.status
                break
            except Exception as exc:
                last_error = type(exc).__name__
                time.sleep(2)
        if health_status != 200:
            sys.stdout.write('QUICK_TUNNEL_HEALTH_FAILED %s\n' % last_error)
            return 2
        sys.stdout.write('HEALTH %s %s\n' % (health_status, health_body.decode()[:180]))
        if b'SYNTHETIC' in health_body or b'receipt' in health_body:
            sys.stdout.write('FAIL health-leaked\n')
            return 1
        # Duplicate retry over the public hop, synthetic body only.
        status, hdrs, raw = _post(public + '/v1/session', {'password': password}, {
            'Content-Type': 'application/json',
            'Origin': origin,
        })
        if 'set-cookie' in hdrs or status != 200:
            sys.stdout.write('FAIL tunnel-session %s\n' % status)
            return 1
        token = json.loads(raw)['token']
        key = 'synthetic-tunnel-retry-1'
        body = {'v': 1, 'kind': 'liuguang-checklist', 'storageKey': 'liuguang-checklist-20261109-v1', 'synthetic': True,
                'answers': {'3.1': {'status': 'answered', 'text': 'SYNTHETIC-ONLY-TUNNEL-API', 'parkUntil': ''}}}
        headers = {'Content-Type': 'application/json', 'Origin': origin, 'Authorization': 'Bearer ' + token, 'Idempotency-Key': key}
        s1, _, r1 = _post(public + '/v1/submissions', body, headers)
        s2, _, r2 = _post(public + '/v1/submissions', body, headers)
        a1, a2 = json.loads(r1), json.loads(r2)
        same = s1 == 200 and s2 == 200 and a1.get('persisted') and a2.get('replay') and a1.get('receiptId') == a2.get('receiptId')
        sys.stdout.write('PASS tunnel-duplicate\n' if same else 'FAIL tunnel-duplicate\n')
        if not same:
            return 1
        evil, _, _ = _post(public + '/v1/submissions', body, {**headers, 'Origin': 'https://evil.example'})
        sys.stdout.write('PASS tunnel-origin-block\n' if evil == 403 else 'FAIL tunnel-origin-block %s\n' % evil)
        if evil != 403:
            return 1
        page = f'{origin}/review/liuguang/?gw={public}&synthetic=1'
        if not os.path.isdir(os.path.join(HERE, 'node_modules')):
            subprocess.run(['npm', 'install', '--omit=dev', '--no-audit', '--no-fund'], cwd=HERE, check=True)
        browser = subprocess.run(
            ['node', os.path.join(HERE, 'trial_browser.mjs')],
            cwd=HERE,
            env={**os.environ, 'LIUGUANG_TEST_PASSWORD': password, 'PAGE_URL': page, 'CHROME_PATH': os.environ.get('CHROME_PATH', '/usr/bin/google-chrome')},
            check=False, capture_output=True, text=True, timeout=90,
        )
        sys.stdout.write(browser.stdout.replace(password, '***'))
        if browser.returncode != 0:
            sys.stdout.write(browser.stderr.replace(password, '***')[-800:])
            return browser.returncode
        read = subprocess.run(
            [sys.executable, os.path.join(GATEWAY, 'readback.py'), '--root', root, '--password-file', secret, '--json', '--check-perms'],
            cwd=GATEWAY, env=env, check=False, capture_output=True, text=True,
        )
        if read.returncode != 0:
            sys.stdout.write('FAIL tunnel-readback\n')
            return 1
        records = json.loads(read.stdout)
        texts = sorted(a.get('text') for rec in records for a in rec.get('answers', {}).values())
        sys.stdout.write('READBACK count=%s perms=ok synthetic=%s\n' % (len(records), all(r.get('synthetic') for r in records)))
        sys.stdout.write('READBACK texts %s\n' % ' | '.join(texts))
        if 'SYNTHETIC-ONLY-TUNNEL' not in texts or 'SYNTHETIC-ONLY-TUNNEL-API' not in texts:
            sys.stdout.write('FAIL tunnel-missing-text\n')
            return 1
        api_hits = [r for r in records if any(a.get('text') == 'SYNTHETIC-ONLY-TUNNEL-API' for a in r.get('answers', {}).values())]
        if len(api_hits) != 1:
            sys.stdout.write('FAIL tunnel-duplicate-count %s\n' % len(api_hits))
            return 1
        sys.stdout.write('QUICK_TUNNEL_TRIAL_OK\n')
        return 0
    finally:
        tunnel.terminate()
        try:
            tunnel.wait(timeout=5)
        except subprocess.TimeoutExpired:
            tunnel.kill()
        gateway.terminate()
        try:
            gateway.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gateway.kill()
        httpd.shutdown()
        # The quick tunnel hostname must not remain a configured route.
        time.sleep(1)
        try:
            urllib.request.urlopen(public + '/v1/health', timeout=8)
            sys.stdout.write('TUNNEL_STILL_UP\n')
        except Exception:
            sys.stdout.write('TUNNEL_STOPPED\n')
        shutil.rmtree(home, ignore_errors=True)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.stdout.write('QUICK_TUNNEL_UNAVAILABLE ' + type(exc).__name__ + '\n')
        raise SystemExit(2)
