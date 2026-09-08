"""Versioned, signed envelopes and deployment-specific content key wrapping."""

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(data)


def signer(path: Path) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(path.read_text().strip()))


def signed(value, key: Ed25519PrivateKey) -> bytes:
    payload = canonical(value)
    return canonical({"payload": encode(payload), "signature": encode(key.sign(payload))})


def wrap_key(content_key: bytes, deployment_public: str, build_id: str) -> dict:
    ephemeral = X25519PrivateKey.generate()
    recipient = X25519PublicKey.from_public_bytes(bytes.fromhex(deployment_public))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"appguard-wrap-v1").derive(ephemeral.exchange(recipient))
    nonce = os.urandom(12)
    return {
        "ephemeral_public": ephemeral.public_key().public_bytes_raw().hex(),
        "nonce": encode(nonce),
        "ciphertext": encode(AESGCM(key).encrypt(nonce, content_key, build_id.encode())),
    }
