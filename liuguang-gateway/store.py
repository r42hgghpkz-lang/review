"""Durable Liuguang private package. Directories 0700, files 0600.

Receipts are acknowledged only after the file and the idempotency record
have been fsynced and renamed into place. The package must sit outside any
git checkout and outside Wenshu.
"""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass

from crypto_box import PRODUCTION_KDF_ITERS, decrypt, derive_key, encrypt

PACKAGE_NAME = 'liuguang'
STORAGE_KEY = 'liuguang-checklist-20261109-v1'
CATALOG_ID = '20261109-v1'


class StoreError(Exception):
    pass


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def tighten_tree(root: str) -> None:
    """Force the private package to 0700/0600, including a pre-existing 0755 tree."""
    os.chmod(root, 0o700)
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o700)
        for name in dirnames:
            os.chmod(os.path.join(dirpath, name), 0o700)
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), 0o600)


def _git_ancestor(path: str) -> str | None:
    cur = path if os.path.isdir(path) else os.path.dirname(path)
    if not cur:
        return None
    cur = os.path.abspath(cur)
    if os.path.isdir(cur):
        cur = os.path.realpath(cur)
    while True:
        if os.path.exists(os.path.join(cur, '.git')):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def assert_safe_root(root: str, gateway_dir: str) -> str:
    if not root or not os.path.isabs(root):
        raise StoreError('answers root must be an absolute path')
    resolved = os.path.abspath(root)
    parent = os.path.dirname(resolved)
    if os.path.isdir(parent):
        resolved = os.path.join(os.path.realpath(parent), os.path.basename(resolved))
    parts = {p.lower() for p in resolved.split(os.sep) if p}
    if 'wenshu' in parts:
        raise StoreError('answers root must stay outside Wenshu')
    if _git_ancestor(resolved):
        raise StoreError('answers root is inside a git checkout; keep the private package outside the repository')
    gateway = os.path.realpath(gateway_dir)
    if resolved == gateway or resolved.startswith(gateway + os.sep):
        raise StoreError('answers root must not live inside the gateway source tree')
    return resolved


def atomic_write(path: str, data: bytes) -> None:
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix='.w-', dir=directory)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        dfd = os.open(directory, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass


def _fsync_dir(path: str) -> None:
    dfd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


@dataclass
class CommitResult:
    receipt: dict
    replay: bool


class Package:
    def __init__(self, root: str, data_secret: str, iters: int, *, create_kdf: bool = True):
        self.root = root
        self.data_secret = data_secret
        self.iters = iters
        self.salt = self._load_salt(create_kdf)
        self.key = derive_key(data_secret, self.salt, iters)
        self.lock_path = os.path.join(root, '.lock')

    def _load_salt(self, create: bool) -> bytes:
        salt_path = os.path.join(self.root, '.kdf-salt')
        meta_path = os.path.join(self.root, '.kdf-meta')
        salt_exists = os.path.exists(salt_path)
        meta_exists = os.path.exists(meta_path)
        if salt_exists != meta_exists:
            raise StoreError('kdf metadata')
        if salt_exists:
            meta = json.loads(open(meta_path, encoding='utf-8').read())
            if int(meta.get('iters', 0)) != self.iters:
                raise StoreError('kdf metadata does not match this process')
            salt = open(salt_path, 'rb').read()
            if len(salt) != 16:
                raise StoreError('kdf salt')
            return salt
        if not create:
            raise StoreError('package is not initialized')
        salt = os.urandom(16)
        atomic_write(meta_path, json.dumps({'iters': self.iters, 'kdf': 'pbkdf2-sha256'}).encode())
        atomic_write(salt_path, salt)
        return salt

    def locked(self):
        return _Lock(self.lock_path)

    def encrypt_json(self, obj: dict) -> str:
        raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return encrypt(self.data_secret, raw, salt=self.salt, iters=self.iters, key=self.key)

    def decrypt_json(self, blob: str) -> dict:
        raw = decrypt(self.data_secret, blob, iters=self.iters, key=self.key)
        obj = json.loads(raw.decode('utf-8'))
        if not isinstance(obj, dict):
            raise StoreError('record')
        return obj

    def commit(self, receipt: dict) -> CommitResult:
        """Persist a new receipt, or return the existing one for this idempotency key.

        The caller may send the acknowledgement only after this returns.
        """
        key = receipt['idempotencyKey']
        digest_name = hashlib.sha256(key.encode('utf-8')).hexdigest()
        idem_dir = os.path.join(self.root, 'idempotency')
        rec_dir = os.path.join(self.root, 'receipts')
        os.makedirs(idem_dir, 0o700, exist_ok=True)
        os.makedirs(rec_dir, 0o700, exist_ok=True)
        os.chmod(idem_dir, 0o700)
        os.chmod(rec_dir, 0o700)
        idem_path = os.path.join(idem_dir, digest_name)
        with self.locked():
            if os.path.exists(idem_path):
                prior = self.decrypt_json(open(idem_path, encoding='utf-8').read())
                if prior.get('idempotencyKey') != key:
                    raise StoreError('idempotency mismatch')
                rec_path = os.path.join(rec_dir, prior['receiptId'] + '.bin')
                stored = self.decrypt_json(open(rec_path, encoding='utf-8').read())
                return CommitResult(stored, True)
            receipt_id = receipt['receiptId']
            rec_path = os.path.join(rec_dir, receipt_id + '.bin')
            blob = self.encrypt_json(receipt)
            atomic_write(rec_path, blob.encode('ascii'))
            pointer = {
                'idempotencyKey': key,
                'receiptId': receipt_id,
                'digest': receipt['digest'],
                'storedAt': receipt['storedAt'],
            }
            atomic_write(idem_path, self.encrypt_json(pointer).encode('ascii'))
            atomic_write(os.path.join(self.root, 'latest.bin'), self.encrypt_json({
                'receiptId': receipt_id,
                'digest': receipt['digest'],
                'storedAt': receipt['storedAt'],
            }).encode('ascii'))
            _fsync_dir(rec_dir)
            _fsync_dir(idem_dir)
            _fsync_dir(self.root)
            # Re-read after the directory fsync so the ack matches durable bytes.
            stored = self.decrypt_json(open(rec_path, encoding='utf-8').read())
            return CommitResult(stored, False)

    def read_receipt(self, receipt_id: str) -> dict:
        if not _safe_receipt_id(receipt_id):
            raise StoreError('receipt id')
        path = os.path.join(self.root, 'receipts', receipt_id + '.bin')
        return self.decrypt_json(open(path, encoding='utf-8').read())

    def latest_id(self) -> str | None:
        path = os.path.join(self.root, 'latest.bin')
        if not os.path.exists(path):
            return None
        return self.decrypt_json(open(path, encoding='utf-8').read())['receiptId']

    def list_ids(self) -> list[str]:
        rec_dir = os.path.join(self.root, 'receipts')
        if not os.path.isdir(rec_dir):
            return []
        ids = []
        for name in os.listdir(rec_dir):
            if name.endswith('.bin'):
                ids.append(name[:-4])
        return sorted(ids)

    def put_session(self, token: str, expires_at: str) -> None:
        digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
        directory = os.path.join(self.root, 'sessions')
        os.makedirs(directory, 0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        path = os.path.join(directory, digest)
        with self.locked():
            atomic_write(path, json.dumps({'expiresAt': expires_at}, separators=(',', ':')).encode())

    def session_ok(self, token: str, now: datetime.datetime) -> bool:
        if not token or len(token) > 200:
            return False
        digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
        path = os.path.join(self.root, 'sessions', digest)
        try:
            rec = json.loads(open(path, encoding='utf-8').read())
            exp = datetime.datetime.strptime(rec['expiresAt'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return exp > now


def _safe_receipt_id(receipt_id: str) -> bool:
    if not receipt_id or len(receipt_id) > 80 or '/' in receipt_id or '\\' in receipt_id:
        return False
    return receipt_id.startswith('lg-') and all(c.isalnum() or c in '-T' for c in receipt_id)


def prepare_root(root: str, gateway_dir: str) -> str:
    resolved = assert_safe_root(root, gateway_dir)
    os.makedirs(resolved, 0o700, exist_ok=True)
    os.chmod(resolved, 0o700)
    marker_path = os.path.join(resolved, 'PACKAGE.json')
    if os.path.exists(marker_path):
        marker = json.loads(open(marker_path, encoding='utf-8').read())
        if marker.get('package') != PACKAGE_NAME or marker.get('isolatedFrom') != 'wenshu':
            raise StoreError('answers directory is not a liuguang private package')
    else:
        atomic_write(marker_path, json.dumps({
            'package': PACKAGE_NAME,
            'isolatedFrom': 'wenshu',
            'storageKey': STORAGE_KEY,
            'catalog': CATALOG_ID,
        }, indent=2).encode())
    tighten_tree(resolved)
    if _mode(resolved) != 0o700:
        raise StoreError('answers root mode')
    return resolved


def kdf_iters_from_env() -> int:
    raw = os.environ.get('LIUGUANG_KDF_ITERS', '').strip()
    if not raw:
        return PRODUCTION_KDF_ITERS
    try:
        iters = int(raw)
    except ValueError as exc:
        raise StoreError('LIUGUANG_KDF_ITERS') from exc
    if iters != PRODUCTION_KDF_ITERS and os.environ.get('LIUGUANG_ALLOW_TEST_KDF') != '1':
        raise StoreError('refusing kdf iterations below the review baseline')
    if iters < 1000:
        raise StoreError('kdf iterations')
    return iters


class _Lock:
    def __init__(self, path: str):
        self.path = path
        self.fd = -1

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        os.fchmod(self.fd, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *args):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = -1
