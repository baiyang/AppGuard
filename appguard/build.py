"""Produce encrypted whole modules, signed metadata and source-free launch stubs."""

import fnmatch
import hashlib
import marshal
import os
import shutil
import sys
import tomllib
import uuid
from glob import has_magic
from pathlib import Path
from types import CodeType

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .crypto import product_id, read_key, signed, signer


def encrypt_entry(directory: Path, identifier: str, code: CodeType, key: bytes, aad: bytes) -> dict:
    nonce = os.urandom(12)
    encrypted = nonce + AESGCM(key).encrypt(nonce, marshal.dumps(code), aad)
    filename = hashlib.sha256(identifier.encode()).hexdigest() + ".agc"
    (directory / filename).write_bytes(encrypted)
    return {"file": filename, "sha256": hashlib.sha256(encrypted).hexdigest()}


def collect_files(source: Path, include: list[str], exclude: list[str]) -> list[Path]:
    """Expand files/globs and recursively visit included directories once."""
    for pattern in [*include, *exclude]:
        if not isinstance(pattern, str) or not pattern or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError("Include/exclude patterns must be nonempty paths relative to source")
    patterns = [pattern.rstrip("/") for pattern in exclude]
    visited: set[Path] = set()
    files: list[Path] = []

    def visit(path: Path) -> None:
        if path in visited:
            return
        visited.add(path)
        relative = path.relative_to(source)
        ancestors = [relative, *relative.parents]
        for ancestor in ancestors:
            if (source / ancestor).is_symlink():
                return
            name = ancestor.as_posix()
            if any(fnmatch.fnmatchcase(name, pattern)
                   or (pattern.endswith("/**") and fnmatch.fnmatchcase(name, pattern[:-3]))
                   for pattern in patterns):
                return
        if path.is_dir():
            for child in sorted(path.iterdir()):
                visit(child)
        elif path.is_file():
            files.append(path)

    for pattern in include:
        matches = source.glob(pattern) if has_magic(pattern) else [source / pattern]
        for path in sorted(matches):
            visit(path)
    return sorted(files)


def build(source: Path, config_path: Path, key_path: Path, output: Path, code_key_path: Path) -> dict:
    if sys.version_info[:2] != (3, 11) or sys.implementation.name != "cpython":
        raise ValueError("AppGuard requires CPython 3.11 for build and execution")
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; select a new release directory")
    source = source.resolve()
    if not source.is_dir():
        raise ValueError("Source must be an existing directory")
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    unknown = set(config) - {"product_id", "include", "exclude"}
    if unknown:
        raise ValueError(f"Unsupported build configuration fields: {sorted(unknown)}")
    product = product_id(config.get("product_id"))
    include, exclude = config.get("include"), config.get("exclude", [])
    if not isinstance(include, list) or not include or not isinstance(exclude, list):
        raise ValueError("include must be a nonempty list and exclude must be a list")
    paths = collect_files(source, include, exclude)
    if not any(path.suffix == ".py" for path in paths):
        raise ValueError("No Python modules matched the include patterns")
    publisher = signer(key_path)
    code_key = read_key(code_key_path)
    for path in paths:
        if path.samefile(key_path) or path.samefile(code_key_path):
            raise ValueError("Publisher and code key files must not be included in the bundle")
        if path.suffix in {".pyc", ".pyo", ".pyw"}:
            raise ValueError(f"Unprotected Python file must be excluded: {path.relative_to(source)}")

    build_id = uuid.uuid4().hex
    bundle = output / "bundle"
    tree, modules = bundle / "tree", bundle / "modules"
    output.mkdir(parents=True)
    try:
        modules.mkdir(parents=True)
        tree.mkdir()
        entries = {}
        for path in paths:
            relative = path.relative_to(source).as_posix()
            target = tree / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix != ".py":
                shutil.copy2(path, target)
                continue
            code = compile(path.read_bytes(), "/app/" + relative, "exec", dont_inherit=True)
            entries[relative] = encrypt_entry(
                modules, relative, code, code_key,
                f"{product}:{build_id}:module:{relative}".encode(),
            )
            target.write_text(
                "# AppGuard protected module.\n"
                "from guard_runtime import execute_module as __appguard_load__\n"
                f"__appguard_load__({relative!r}, globals())\n",
                encoding="utf-8",
            )
        manifest = {
            "format": 2, "kind": "manifest", "product_id": product, "build_id": build_id,
            "python": "3.11", "layout": "modules-v1", "modules": entries,
            "code_key_sha256": hashlib.sha256(code_key).hexdigest(),
        }
        (bundle / "manifest.json").write_bytes(signed(manifest, publisher))
    except BaseException:
        shutil.rmtree(output)
        raise
    return {"product_id": product, "build_id": build_id, "modules": len(entries), "bundle": str(bundle)}
