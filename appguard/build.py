"""Produce encrypted modules, signed metadata and source-free launch stubs."""

import ast
import fnmatch
import hashlib
import json
import marshal
import os
import shutil
import sys
import tomllib
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .crypto import canonical, private_write, signed, signer


class Checkpoints(ast.NodeTransformer):
    def __init__(self, functions: set[str]):
        self.functions = functions
        self.found: set[str] = set()

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        if node.name in self.functions:
            self.found.add(node.name)
            check = ast.Expr(ast.Call(ast.Name("__appguard_check__", ast.Load()), [], []))
            offset = int(bool(node.body and isinstance(node.body[0], ast.Expr)
                              and isinstance(node.body[0].value, ast.Constant)
                              and isinstance(node.body[0].value.value, str)))
            node.body.insert(offset, ast.copy_location(check, node))
        return node

    visit_AsyncFunctionDef = visit_FunctionDef


def build(source: Path, config_path: Path, key_path: Path, output: Path) -> dict:
    if sys.version_info[:2] != (3, 11):
        raise ValueError("AppGuard v1 requires CPython 3.11 for build and execution")
    if output.exists():
        raise ValueError("Output already exists; select a new release directory")
    source = source.resolve()
    config = tomllib.loads(config_path.read_text())
    publisher = signer(key_path)
    content_key = os.urandom(32)
    build_id = uuid.uuid4().hex
    bundle = output / "bundle"
    tree = bundle / "tree"
    modules = bundle / "modules"
    modules.mkdir(parents=True)
    entries = {}
    configured = config.get("checkpoints", {})
    seen = set()
    for pattern in config["include"]:
        for path in sorted(source.glob(pattern)):
            if path.suffix != ".py" or path.is_symlink():
                continue
            relative = path.relative_to(source).as_posix()
            if relative in entries:
                continue
            if any(fnmatch.fnmatch(relative, p) for p in config.get("exclude", [])):
                continue
            code = ast.parse(path.read_bytes(), filename="/app/" + relative)
            checkpoints = Checkpoints(set(configured.get(relative, [])))
            code = checkpoints.visit(code)
            missing = checkpoints.functions - checkpoints.found
            if missing:
                raise ValueError(f"Missing checkpoints in {relative}: {sorted(missing)}")
            seen.add(relative)
            ast.fix_missing_locations(code)
            data = marshal.dumps(compile(code, "/app/" + relative, "exec", dont_inherit=True))
            nonce = os.urandom(12)
            aad = f"{build_id}:{relative}".encode()
            encrypted = nonce + AESGCM(content_key).encrypt(nonce, data, aad)
            filename = hashlib.sha256(relative.encode()).hexdigest() + ".agc"
            (modules / filename).write_bytes(encrypted)
            target = tree / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "# AppGuard protected module.\n"
                "from guard_runtime import execute_module as __appguard_load__\n"
                f"__appguard_load__({relative!r}, globals())\n"
            )
            entries[relative] = {"file": filename, "sha256": hashlib.sha256(encrypted).hexdigest()}
    if set(configured) - seen:
        raise ValueError(f"Checkpoint files were not included: {sorted(set(configured) - seen)}")
    if not entries:
        raise ValueError("No Python modules matched the include patterns")
    for pattern in config.get("resources", []):
        for resource in source.glob(pattern):
            paths = resource.rglob("*") if resource.is_dir() else [resource]
            for path in paths:
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(source)
                if path.suffix in {".py", ".pyc", ".pyo", ".key"} or ".env" in path.name:
                    raise ValueError(f"Source or secret-like resource rejected: {relative}")
                if "__pycache__" in relative.parts:
                    continue
                target = tree / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
    manifest = {
        "format": 1, "product_id": config["product_id"], "build_id": build_id,
        "python": "3.11", "application": config["application"],
        "python_path": config.get("python_path", "backend"), "modules": entries,
    }
    (bundle / "manifest.json").write_bytes(signed(manifest, publisher))
    (bundle / "publisher.pub").write_text(publisher.public_key().public_bytes_raw().hex() + "\n")
    private_write(output / "release.json", canonical({
        "format": 1, "product_id": config["product_id"], "build_id": build_id,
        "content_key": content_key.hex(),
        "publisher_public": publisher.public_key().public_bytes_raw().hex(),
    }))
    return {"build_id": build_id, "modules": len(entries), "bundle": str(bundle)}
