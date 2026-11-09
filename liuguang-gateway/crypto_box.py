"""AES-GCM packing compatible with the reviewer's salt16||iv12||ciphertext layout.

The key is a separate runtime secret. This module never opens the chapter
review password and never embeds a key in a static page.
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Same iteration baseline as the chapter cipher. Tests may go lower only when
# the process explicitly opts in; production startup rejects that.
PRODUCTION_KDF_ITERS = 600_000


def derive_key(secret: str, salt: bytes, iters: int) -> bytes:
    if iters < 1:
        raise ValueError('kdf iterations')
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iters)
    return kdf.derive(secret.encode('utf-8'))


def encrypt(secret: str, data: bytes, *, salt: bytes, iters: int, key: bytes | None = None) -> str:
    if len(salt) != 16:
        raise ValueError('salt')
    aes_key = key if key is not None else derive_key(secret, salt, iters)
    iv = os.urandom(12)
    ct = AESGCM(aes_key).encrypt(iv, data, None)
    return base64.b64encode(salt + iv + ct).decode('ascii')


def decrypt(secret: str, blob: str, *, iters: int, key: bytes | None = None) -> bytes:
    raw = base64.b64decode(''.join(blob.split()))
    if len(raw) < 16 + 12 + 16:
        raise ValueError('ciphertext')
    salt, iv, ct = raw[:16], raw[16:28], raw[28:]
    aes_key = key if key is not None else derive_key(secret, salt, iters)
    return AESGCM(aes_key).decrypt(iv, ct, None)
