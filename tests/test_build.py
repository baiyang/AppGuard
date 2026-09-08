"""Publisher build validation; no Docker daemon or running application required."""

import ast
import base64
import json
import marshal
import asyncio
import inspect
from pathlib import Path
from types import FunctionType

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from appguard.build import build, code_objects, collect_files


@pytest.fixture
def project(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text('"""Example."""\nfrom __future__ import annotations\ndef answer(value: int) -> int:\n    """Return a result."""\n    return value + 42\n')
    config = tmp_path / "guard.toml"
    config.write_text('product_id="test"\ninclude=["*.py"]\n[protected_functions]\n"main.py"=["answer"]\n')
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
    bootstrap_key = bytes.fromhex((output / "bundle/bootstrap.key").read_text().strip())
    data = AESGCM(bootstrap_key).decrypt(blob[:12], blob[12:], f"{result['build_id']}:module:main.py".encode())
    calls = []
    def invoke(identifier, namespace, args, kwargs):
        calls.append(identifier)
        protected = manifest["functions"][identifier]
        body = (output / "bundle/functions" / protected["file"]).read_bytes()
        original = marshal.loads(AESGCM(bytes.fromhex(release["content_key"])).decrypt(
            body[:12], body[12:], f"{result['build_id']}:function:{identifier}".encode()))
        return FunctionType(original, namespace)(*args, **kwargs)
    namespace = {"__appguard_invoke__": invoke}
    exec(marshal.loads(data), namespace)
    assert namespace["answer"](3) == 45
    assert calls == ["main.py:answer"]
    assert all(42 not in code.co_consts for code in code_objects(marshal.loads(data)).values())
    assert inspect.signature(namespace["answer"]).parameters["value"].annotation == "int"
    stub = (output / "bundle/tree/main.py").read_text()
    assert "return value" not in stub
    assert "execute_module" in stub
    assert b"Return a result" not in blob
    assert (output / "release.json").stat().st_mode & 0o077 == 0
    assert list((output / "bundle").rglob("*.key")) == [output / "bundle/bootstrap.key"]
    assert bootstrap_key.hex() != release["content_key"]


def test_missing_protected_function_fails_build(project, tmp_path):
    source, config, private, _ = project
    config.write_text(config.read_text().replace('"answer"', '"missing"'))
    with pytest.raises(ValueError, match="Missing protected functions"):
        build(source, config, private, tmp_path / "release")


def test_build_never_overwrites_release(project, tmp_path):
    source, config, private, _ = project
    output = tmp_path / "release"
    build(source, config, private, output)
    with pytest.raises(ValueError, match="already exists"):
        build(source, config, private, output)


def test_removed_configuration_field_is_rejected(project, tmp_path):
    source, config, private, _ = project
    config.write_text(config.read_text().replace('[protected_functions]', 'resources=["main.py"]\n[protected_functions]'))
    with pytest.raises(ValueError, match="Unsupported build configuration fields"):
        build(source, config, private, tmp_path / "release")


@pytest.mark.parametrize("include", ["backend", "backend/", "backend/*", "backend/**", "backend/**/*"])
def test_directory_selection_encrypts_python_and_copies_other_files(project, tmp_path, include):
    source, config, private, _ = project
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
    config.write_text(f'product_id="test"\ninclude=["{include}"]\n[protected_functions]\n"backend/nested/module.py"=["answer"]\n')
    output = tmp_path / "release"
    result = build(source, config, private, output)
    tree = output / "bundle/tree/backend"
    assert result["modules"] == 1
    assert result["protected_functions"] == 1
    assert (tree / "nested/module.py").read_text().startswith("# AppGuard protected module.")
    assert "1234" not in (tree / "nested/module.py").read_text()
    for relative, content in originals.items():
        assert (tree / relative).read_bytes() == content
    assert (tree / "scripts/command.sh").stat().st_mode & 0o777 == 0o755
    assert {path.name for path in output.iterdir()} == {"bundle", "release.json"}
    assert {path.name for path in (output / "bundle").iterdir()} == {
        "tree", "modules", "functions", "manifest.json", "publisher.pub", "bootstrap.key",
    }


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


@pytest.mark.parametrize("pattern", ["../outside", "/absolute/path", ""])
def test_selection_patterns_cannot_escape_source(tmp_path, pattern):
    with pytest.raises(ValueError, match="relative to source"):
        collect_files(tmp_path, [pattern], [])


def test_excluding_protected_python_file_fails_build(project, tmp_path):
    source, config, private, _ = project
    config.write_text(config.read_text().replace('[protected_functions]', 'exclude=["main.py"]\n[protected_functions]'))
    with pytest.raises(ValueError, match="Protection files were not included"):
        build(source, config, private, tmp_path / "release")


def test_signatures_generators_methods_and_coroutines(project, tmp_path):
    source, config, private, _ = project
    (source / "main.py").write_text('''
class Example:
    def work(self, x=2, /, *items, scale=3, **options):
        return (x + sum(items)) * scale + options.get("offset", 0)
def stream():
    try:
        value = yield "first"
        yield value
    except ValueError:
        yield "caught"
    return 17
async def calculate(x=4):
    return x * 2
''')
    config.write_text('product_id="test"\ninclude=["*.py"]\n[protected_functions]\n"main.py"=["Example.work", "stream", "calculate"]\n')
    output = tmp_path / "release"
    build(source, config, private, output)
    manifest = json.loads(base64.b64decode(json.loads((output / "bundle/manifest.json").read_text())["payload"]))
    bootstrap_key = bytes.fromhex((output / "bundle/bootstrap.key").read_text().strip())
    content_key = bytes.fromhex(json.loads((output / "release.json").read_text())["content_key"])
    def decrypt(section, identifier, key):
        entry = manifest[section][identifier]
        blob = (output / "bundle" / section / entry["file"]).read_bytes()
        kind = "module" if section == "modules" else "function"
        return marshal.loads(AESGCM(key).decrypt(blob[:12], blob[12:], f"{manifest['build_id']}:{kind}:{identifier}".encode()))
    namespace = {"__name__": "main"}
    namespace["__appguard_invoke__"] = lambda identifier, globals, args, kwargs: FunctionType(
        decrypt("functions", identifier, content_key), globals)(*args, **kwargs)
    exec(decrypt("modules", "main.py", bootstrap_key), namespace)
    obj = namespace["Example"]()
    assert obj.work() == 6
    assert obj.work(2, 4, scale=2, offset=1) == 13
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


def test_protected_closure_fails_explicitly(project, tmp_path):
    source, config, private, _ = project
    (source / "main.py").write_text("def outer(x):\n    def inner():\n        return x\n    return inner\n")
    config.write_text('product_id="test"\ninclude=["*.py"]\n[protected_functions]\n"main.py"=["outer.<locals>.inner"]\n')
    with pytest.raises(ValueError, match="closures are unsupported"):
        build(source, config, private, tmp_path / "release")


def test_empty_protection_fails_instead_of_shipping_bootstrap_only(project, tmp_path):
    source, config, private, _ = project
    config.write_text('product_id="test"\ninclude=["*.py"]\n')
    with pytest.raises(ValueError, match="At least one protected function"):
        build(source, config, private, tmp_path / "release")
