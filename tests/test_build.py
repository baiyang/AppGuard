"""Publisher module encryption, package selection and compatibility checks."""

import asyncio
import base64
import hashlib
import inspect
import json
import marshal
import os
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from appguard.build import build, collect_files


@pytest.fixture
def project(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text('"""Example."""\nfrom __future__ import annotations\ndef answer(value: int) -> int:\n    """Return a result."""\n    return value + 42\n')
    config = tmp_path / "guard.toml"
    config.write_text('product_id="test"\ninclude=["*.py"]\n')
    key = Ed25519PrivateKey.generate()
    private = tmp_path / "issuer.key"
    private.write_text(key.private_bytes_raw().hex())
    code_key = tmp_path / "code.key"
    code_key.write_text(os.urandom(32).hex())
    return source, config, private, key, code_key


def read_manifest(output):
    return json.loads(base64.b64decode(json.loads((output / "bundle/manifest.json").read_text())["payload"]))


def decrypt_module(output, code_key, relative="main.py"):
    manifest = read_manifest(output)
    entry = manifest["modules"][relative]
    blob = (output / "bundle/modules" / entry["file"]).read_bytes()
    aad = f"{manifest['product_id']}:{manifest['build_id']}:module:{relative}".encode()
    return marshal.loads(AESGCM(bytes.fromhex(code_key.read_text())).decrypt(blob[:12], blob[12:], aad))


def test_bundle_is_encrypted_signed_and_contains_no_keys(project, tmp_path):
    source, config, private, key, code_key = project
    output = tmp_path / "release"
    result = build(source, config, private, output, code_key)
    envelope = json.loads((output / "bundle/manifest.json").read_text())
    payload = base64.b64decode(envelope["payload"])
    key.public_key().verify(base64.b64decode(envelope["signature"]), payload)
    manifest = json.loads(payload)
    assert set(manifest) == {"format", "kind", "product_id", "build_id", "python", "layout", "modules", "code_key_sha256"}
    assert (manifest["format"], manifest["kind"], manifest["layout"], manifest["python"]) == (2, "manifest", "modules-v1", "3.11")
    assert manifest["code_key_sha256"] == hashlib.sha256(bytes.fromhex(code_key.read_text())).hexdigest()
    assert result["product_id"] == "test"
    assert result["build_id"] == manifest["build_id"]
    entry = manifest["modules"]["main.py"]
    blob = (output / "bundle/modules" / entry["file"]).read_bytes()
    assert entry["sha256"] == hashlib.sha256(blob).hexdigest()
    code = decrypt_module(output, code_key)
    assert code.co_filename == "/app/main.py"
    namespace = {}
    exec(code, namespace)
    assert namespace["answer"](3) == 45
    assert inspect.signature(namespace["answer"]).parameters["value"].annotation == "int"
    assert namespace["answer"].__doc__ == "Return a result."
    assert "__appguard_check__" not in namespace
    assert "__appguard_invoke__" not in namespace
    assert (output / "bundle/tree/main.py").read_text() == (
        "# AppGuard protected module.\n"
        "from guard_runtime import execute_module as __appguard_load__\n"
        "__appguard_load__('main.py', globals())\n"
    )
    assert b"Return a result" not in blob
    assert {path.name for path in output.iterdir()} == {"bundle"}
    assert {path.name for path in (output / "bundle").iterdir()} == {"tree", "modules", "manifest.json"}
    for path in output.rglob("*"):
        if path.is_file():
            assert code_key.read_text().encode() not in path.read_bytes()
            assert private.read_text().encode() not in path.read_bytes()


def test_code_key_is_reused_across_builds_with_fresh_nonces(project, tmp_path):
    source, config, private, _, code_key = project
    first, second = tmp_path / "first", tmp_path / "second"
    build(source, config, private, first, code_key)
    build(source, config, private, second, code_key)
    a, b = read_manifest(first), read_manifest(second)
    assert a["build_id"] != b["build_id"]
    assert a["code_key_sha256"] == b["code_key_sha256"]
    assert a["modules"]["main.py"]["sha256"] != b["modules"]["main.py"]["sha256"]
    assert decrypt_module(first, code_key) == decrypt_module(second, code_key)


@pytest.mark.parametrize("change", ["product", "build", "module", "key", "ciphertext"])
def test_encryption_authenticates_product_build_module_and_content(project, tmp_path, change):
    source, config, private, _, code_key = project
    output = tmp_path / "release"
    build(source, config, private, output, code_key)
    manifest = read_manifest(output)
    blob = (output / "bundle/modules" / manifest["modules"]["main.py"]["file"]).read_bytes()
    product, build_id, relative = manifest["product_id"], manifest["build_id"], "main.py"
    key = bytes.fromhex(code_key.read_text())
    if change == "product":
        product = "other"
    elif change == "build":
        build_id = "other"
    elif change == "module":
        relative = "other.py"
    elif change == "key":
        key = os.urandom(32)
    else:
        blob = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(InvalidTag):
        AESGCM(key).decrypt(blob[:12], blob[12:], f"{product}:{build_id}:module:{relative}".encode())


def test_build_never_overwrites_release(project, tmp_path):
    source, config, private, _, code_key = project
    output = tmp_path / "release"
    build(source, config, private, output, code_key)
    manifest = (output / "bundle/manifest.json").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        build(source, config, private, output, code_key)
    assert (output / "bundle/manifest.json").read_bytes() == manifest


@pytest.mark.parametrize("field", ["resources", "checkpoints", "protected_functions"])
def test_removed_configuration_fields_are_rejected(project, tmp_path, field):
    source, config, private, _, code_key = project
    config.write_text(config.read_text() + f"{field}=[]\n")
    with pytest.raises(ValueError, match="Unsupported build configuration fields"):
        build(source, config, private, tmp_path / "release", code_key)


@pytest.mark.parametrize("value", ['""', '" test"', '123', '"test\\nname"'])
def test_invalid_product_is_rejected(project, tmp_path, value):
    source, config, private, _, code_key = project
    config.write_text(f'product_id={value}\ninclude=["*.py"]\n')
    with pytest.raises(ValueError, match="product_id"):
        build(source, config, private, tmp_path / "release", code_key)


@pytest.mark.parametrize("include", ["backend", "backend/", "backend/*", "backend/**", "backend/**/*"])
def test_directory_selection_encrypts_python_and_copies_other_files(project, tmp_path, include):
    source, config, private, _, code_key = project
    backend = source / "backend"
    (backend / "nested").mkdir(parents=True)
    (backend / "nested/module.py").write_text("def answer():\n    return 1234\n")
    originals = {
        "templates/nested/page.html": b"<html>example</html>",
        "static/style.css": b"body { color: black; }",
        "static/image.png": b"\x89PNG\r\n\x00\xff",
        "prompts/query.md": b"Use the provided evidence.",
        "config/example.yaml": b"enabled: true\n",
        "scripts/command.sh": b"#!/bin/sh\nexit 0\n",
    }
    for relative, content in originals.items():
        path = backend / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (backend / "scripts/command.sh").chmod(0o755)
    config.write_text(f'product_id="test"\ninclude=["{include}"]\n')
    output = tmp_path / "release"
    result = build(source, config, private, output, code_key)
    tree = output / "bundle/tree/backend"
    assert result["modules"] == 1
    assert (tree / "nested/module.py").read_text().startswith("# AppGuard protected module.")
    assert "1234" not in (tree / "nested/module.py").read_text()
    for relative, content in originals.items():
        assert (tree / relative).read_bytes() == content
    assert (tree / "scripts/command.sh").stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("exclude", ["backend/private", "backend/private/", "backend/private/**"])
def test_excluded_directory_prunes_all_descendants(tmp_path, monkeypatch, exclude):
    backend = tmp_path / "backend"
    (backend / "private/nested").mkdir(parents=True)
    (backend / "public").mkdir()
    (backend / "private/nested/hidden.py").write_text("secret = True")
    (backend / "private/token.txt").write_text("secret")
    (backend / "public/page.html").write_text("page")
    original = Path.iterdir

    def checked_iterdir(path):
        assert path != backend / "private", "Excluded directory must not be traversed"
        return original(path)

    monkeypatch.setattr(Path, "iterdir", checked_iterdir)
    selected = collect_files(tmp_path, ["backend/", "backend/private/nested/hidden.py"], [exclude])
    assert selected == [backend / "public/page.html"]


def test_overlapping_includes_deduplicate_and_apply_file_exclusions(tmp_path):
    backend = tmp_path / "backend"
    (backend / "assets").mkdir(parents=True)
    (backend / "main.py").write_text("value = 1")
    (backend / ".env").write_text("secret")
    (backend / "assets/public.txt").write_text("public")
    (backend / "assets/private.txt").write_text("private")
    (tmp_path / "logo.png").write_bytes(b"image")
    selected = collect_files(tmp_path, ["backend/", "backend/assets/", "backend/main.py", "logo.png"],
                             ["**/.env*", "backend/assets/private.*"])
    assert selected == sorted([backend / "main.py", backend / "assets/public.txt", tmp_path / "logo.png"])


def test_source_root_directory_is_a_valid_include(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/data.json").write_text("{}")
    (tmp_path / "main.py").write_text("value = 1")
    assert collect_files(tmp_path, ["."], []) == [tmp_path / "main.py", tmp_path / "nested/data.json"]


def test_symlink_files_and_directories_are_not_copied(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (source / "public.txt").write_text("public")
    (source / "linked").symlink_to(outside, target_is_directory=True)
    (source / "linked.txt").symlink_to(outside / "secret.txt")
    selected = collect_files(source, ["*", "linked/secret.txt"], [])
    assert selected == [source / "public.txt"]


@pytest.mark.parametrize("pattern", ["../outside", "/absolute/path", "", 123])
def test_selection_patterns_cannot_escape_source(tmp_path, pattern):
    with pytest.raises(ValueError, match="relative to source"):
        collect_files(tmp_path, [pattern], [])


def test_no_python_module_fails_before_creating_output(project, tmp_path):
    source, config, private, _, code_key = project
    config.write_text(config.read_text() + 'exclude=["main.py"]\n')
    output = tmp_path / "release"
    with pytest.raises(ValueError, match="No Python modules"):
        build(source, config, private, output, code_key)
    assert not output.exists()


@pytest.mark.parametrize("key_name", ["issuer", "code"])
def test_private_key_hardlinks_cannot_be_packaged(project, tmp_path, key_name):
    source, config, private, _, code_key = project
    (source / "sensitive.txt").hardlink_to(private if key_name == "issuer" else code_key)
    config.write_text('product_id="test"\ninclude=["."]\n')
    with pytest.raises(ValueError, match="key files must not be included"):
        build(source, config, private, tmp_path / "release", code_key)


@pytest.mark.parametrize("suffix", [".pyc", ".pyo", ".pyw"])
def test_python_files_that_bypass_encryption_must_be_excluded(project, tmp_path, suffix):
    source, config, private, _, code_key = project
    (source / ("other" + suffix)).write_bytes(b"plaintext bytecode or source")
    config.write_text('product_id="test"\ninclude=["."]\n')
    with pytest.raises(ValueError, match="Unprotected Python file"):
        build(source, config, private, tmp_path / "release", code_key)


def test_syntax_error_removes_partial_output(project, tmp_path):
    source, config, private, _, code_key = project
    (source / "z_invalid.py").write_text("def broken(:\n")
    output = tmp_path / "release"
    with pytest.raises(SyntaxError):
        build(source, config, private, output, code_key)
    assert not output.exists()


def test_whole_modules_preserve_python_semantics(project, tmp_path):
    source, config, private, _, code_key = project
    (source / "main.py").write_text('''
class Base:
    def work(self):
        return 3
class Example(Base):
    def work(self, x=2, /, *items, scale=3, **options):
        return super().work() + (x + sum(items)) * scale + options.get("offset", 0)
def stream():
    try:
        value = yield "first"
        yield value
    except ValueError:
        yield "caught"
    return 17
async def calculate(x=4):
    return x * 2
async def async_stream():
    yield 7
def outer(x):
    def inner():
        return x
    return inner
''')
    output = tmp_path / "release"
    build(source, config, private, output, code_key)
    namespace = {"__name__": "main"}
    exec(decrypt_module(output, code_key), namespace)
    obj = namespace["Example"]()
    assert obj.work() == 9
    assert obj.work(2, 4, scale=2, offset=1) == 16
    assert inspect.isgeneratorfunction(namespace["stream"])
    iterator = namespace["stream"]()
    assert next(iterator) == "first"
    assert iterator.send("second") == "second"
    assert iterator.throw(ValueError()) == "caught"
    with pytest.raises(StopIteration) as completed:
        next(iterator)
    assert completed.value.value == 17
    assert inspect.iscoroutinefunction(namespace["calculate"])
    assert asyncio.run(namespace["calculate"]()) == 8
    assert namespace["outer"](9)() == 9
    assert inspect.isasyncgenfunction(namespace["async_stream"])

    async def collect():
        return [value async for value in namespace["async_stream"]()]

    assert asyncio.run(collect()) == [7]
