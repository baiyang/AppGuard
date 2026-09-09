"""Exercise publisher commands as users invoke them, including invalid input."""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from appguard.__main__ import timestamp
from appguard.crypto import read_key


ROOT = Path(__file__).resolve().parents[1]


def cli(*arguments):
    return subprocess.run([sys.executable, "-m", "appguard", *map(str, arguments)],
                          cwd=ROOT, capture_output=True, text=True, timeout=15)


@pytest.fixture
def issuer(tmp_path):
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "issuer.key"
    path.write_text(key.private_bytes_raw().hex())
    return path, key


def test_key_generation_is_private_and_never_overwrites(tmp_path):
    private, public, code = tmp_path / "issuer.key", tmp_path / "issuer.pub", tmp_path / "code.key"
    result = cli("keygen", "--out", private)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"private_key": str(private), "public_key": str(public)}
    assert Ed25519PrivateKey.from_private_bytes(read_key(private)).public_key().public_bytes_raw() == read_key(public)
    result = cli("code-keygen", "--out", code)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"code_key": str(code)}
    assert len(read_key(code)) == 32
    assert read_key(code) != read_key(private)
    for path in (private, public, code):
        assert path.stat().st_mode & 0o077 == 0
    for command, path in (("keygen", private), ("code-keygen", code)):
        before = path.read_bytes()
        result = cli(command, "--out", path)
        assert result.returncode == 1
        assert "Traceback" not in result.stderr
        assert path.read_bytes() == before


def test_key_generation_public_collision_leaves_no_partial_private_key(tmp_path):
    private, public = tmp_path / "issuer.key", tmp_path / "issuer.pub"
    public.write_text("existing")
    result = cli("keygen", "--out", private)
    assert result.returncode == 1
    assert not private.exists()
    assert public.read_text() == "existing"
    assert cli("keygen", "--out", tmp_path / "other.pub").returncode == 1
    assert not (tmp_path / "other.pub").exists()


def test_issue_is_product_scoped_and_has_no_code_key_or_build_id(issuer, tmp_path):
    private, key = issuer
    output = tmp_path / "license.lic"
    result = cli("issue", "--product", "example", "--issuer-key", private, "--customer", "Customer A",
                 "--expires", "2099-01-01T00:00:00Z", "--out", output)
    assert result.returncode == 0, result.stderr
    envelope = json.loads(output.read_text())
    payload = base64.b64decode(envelope["payload"], validate=True)
    key.public_key().verify(base64.b64decode(envelope["signature"], validate=True), payload)
    license = json.loads(payload)
    assert set(license) == {"format", "kind", "license_id", "product_id", "customer", "issued_at", "not_before", "expires_at"}
    assert license["format"] == 2
    assert license["kind"] == "license"
    assert license["product_id"] == "example"
    assert license["customer"] == "Customer A"
    assert license["issued_at"] == license["not_before"]
    assert license["expires_at"] == timestamp("2099-01-01T00:00:00Z")
    assert output.stat().st_mode & 0o077 == 0
    metadata = cli("inspect", output)
    assert metadata.returncode == 0
    assert json.loads(metadata.stdout) == {"verified": False, "metadata": license}


@pytest.mark.parametrize("arguments", [
    ["--product", ""], ["--product", " bad"], ["--customer", " "], ["--customer", " Customer "],
    ["--customer", "a" * 513], ["--customer", "line\nbreak"],
    ["--expires", "garbage"], ["--expires", "2099-01-01T00:00:00"],
    ["--expires", "1969-01-01T00:00:00Z"], ["--expires", "2000-01-01T00:00:00Z"],
    ["--expires", "9999-12-31T23:59:59-01:00"],
    ["--not-before", "2100-01-01T00:00:00Z"], ["--not-before", "2099-01-01T00:00:00Z"],
])
def test_invalid_license_inputs_fail_cleanly(issuer, tmp_path, arguments):
    private, _ = issuer
    output = tmp_path / "license.lic"
    result = cli("issue", "--product", "example", "--issuer-key", private, "--customer", "Customer A",
                 "--expires", "2099-01-01T00:00:00Z", "--out", output, *arguments)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert not output.exists()


def test_not_before_and_timezone_are_preserved(issuer, tmp_path):
    private, _ = issuer
    output = tmp_path / "license.lic"
    result = cli("issue", "--product", "example", "--issuer-key", private, "--customer", "Customer A",
                 "--not-before", "2098-01-01T08:00:00+08:00", "--expires", "2099-01-01T08:00:00+08:00", "--out", output)
    assert result.returncode == 0, result.stderr
    license = json.loads(base64.b64decode(json.loads(output.read_text())["payload"]))
    assert license["not_before"] == timestamp("2098-01-01T00:00:00Z")
    assert license["expires_at"] == timestamp("2099-01-01T00:00:00Z")


@pytest.mark.parametrize("document", ["not json", "[]", "null", '{}', '{"payload":3}', '{"payload":"!"}', '{"payload":"W10="}'])
def test_inspect_invalid_documents_fail_cleanly(tmp_path, document):
    path = tmp_path / "input.json"
    path.write_text(document)
    result = cli("inspect", path)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("value", ["00", "gg" * 32, "00 " * 32, "00" * 33])
def test_key_length_and_encoding_are_validated(tmp_path, value):
    path = tmp_path / "code.key"
    path.write_text(value)
    with pytest.raises(ValueError, match="32-byte hexadecimal"):
        read_key(path)


def test_cli_build_requires_explicit_code_key_and_handles_syntax_errors(issuer, tmp_path):
    private, _ = issuer
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("def broken(:")
    config = tmp_path / "guard.toml"
    config.write_text('product_id="example"\ninclude=["*.py"]\n')
    code = tmp_path / "code.key"
    code.write_text(os.urandom(32).hex())
    output = tmp_path / "release"
    arguments = ("build", "--source", source, "--config", config, "--issuer-key", private, "--out", output)
    assert cli(*arguments).returncode == 2
    result = cli(*arguments, "--code-key", code)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert not output.exists()
    (source / "main.py").write_text("answer = 42")
    result = cli(*arguments, "--code-key", code)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["product_id"] == "example"
