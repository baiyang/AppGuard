"""Signed envelopes and publisher-managed product keys."""

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(data)


def signer(path: Path) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(read_key(path))


def read_key(path: Path) -> bytes:
    value = path.read_text(encoding="ascii").strip()
    if len(value) != 64 or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise ValueError(f"Expected a 32-byte hexadecimal key: {path}")
    return bytes.fromhex(value)


def product_id(value: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 128 or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError("product_id must be a nonempty string of at most 128 characters without surrounding whitespace or control characters")
    return value


def signed(value, key: Ed25519PrivateKey) -> bytes:
    payload = canonical(value)
    return canonical({"payload": encode(payload), "signature": encode(key.sign(payload))})
