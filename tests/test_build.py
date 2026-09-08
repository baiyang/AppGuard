"""Publisher build validation; no Docker daemon or running application required."""

import ast
import base64
import json
import marshal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from appguard.build import build


@pytest.fixture
def project(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text('"""Example."""\nfrom __future__ import annotations\ndef answer(value: int) -> int:\n    """Return a result."""\n    return value + 42\n')
    config = tmp_path / "guard.toml"
    config.write_text('product_id="test"\napplication="main:app"\ninclude=["*.py"]\n[checkpoints]\n"main.py"=["answer"]\n')
    key = Ed25519PrivateKey.generate()
    private = tmp_path / "issuer.key"
    private.write_text(key.private_bytes_raw().hex())
    return source, config, private, key


def test_bundle_is_encrypted_signed_and_instrumented(project, tmp_path):
    source, config, private, key = project
    output = tmp_path / "release"
    result = build(source, config, private, output)
    envelope = json.loads((output / "bundle/manifest.json").read_text())
    payload = base64.b64decode(envelope["payload"])
    key.public_key().verify(base64.b64decode(envelope["signature"]), payload)
    manifest = json.loads(payload)
    entry = manifest["modules"]["main.py"]
    blob = (output / "bundle/modules" / entry["file"]).read_bytes()
    release = json.loads((output / "release.json").read_text())
    data = AESGCM(bytes.fromhex(release["content_key"])).decrypt(blob[:12], blob[12:], f"{result['build_id']}:main.py".encode())
    calls = []
    namespace = {"__appguard_check__": lambda: calls.append(True)}
    exec(marshal.loads(data), namespace)
    assert namespace["answer"](3) == 45
    assert calls == [True]
    stub = (output / "bundle/tree/main.py").read_text()
    assert "return value" not in stub
    assert "execute_module" in stub
    assert b"Return a result" not in blob
    assert (output / "release.json").stat().st_mode & 0o077 == 0
    assert not list((output / "bundle").rglob("*.key"))


def test_missing_checkpoint_fails_build(project, tmp_path):
    source, config, private, _ = project
    config.write_text(config.read_text().replace('"answer"', '"missing"'))
    with pytest.raises(ValueError, match="Missing checkpoints"):
        build(source, config, private, tmp_path / "release")


def test_build_never_overwrites_release(project, tmp_path):
    source, config, private, _ = project
    output = tmp_path / "release"
    build(source, config, private, output)
    with pytest.raises(ValueError, match="already exists"):
        build(source, config, private, output)


def test_plain_python_resources_rejected(project, tmp_path):
    source, config, private, _ = project
    config.write_text(config.read_text().replace('[checkpoints]', 'resources=["main.py"]\n[checkpoints]'))
    with pytest.raises(ValueError, match="Source or secret"):
        build(source, config, private, tmp_path / "release")
