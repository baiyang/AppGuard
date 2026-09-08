"""Produce encrypted modules, signed metadata and source-free launch stubs."""

import ast
import copy
import fnmatch
import hashlib
import json
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


def code_objects(code: CodeType) -> dict[str, CodeType]:
    result = {code.co_qualname: code}
    for value in code.co_consts:
        if isinstance(value, CodeType):
            result.update(code_objects(value))
    return result


class FunctionBodies(ast.NodeTransformer):
    """Keep signatures/decorators at import time; defer selected function bodies."""

    def __init__(self, relative: str, selected: set[str], originals: dict[str, CodeType]):
        self.relative = relative
        self.selected = selected
        self.originals = originals
        self.scope: list[str] = []
        self.bodies: dict[str, CodeType] = {}
        self.found: set[str] = set()

    def visit_ClassDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()
        return node

    def visit_FunctionDef(self, node):
        qualified = ".".join([*self.scope, node.name])
        if qualified not in self.selected:
            self.scope.extend([node.name, "<locals>"])
            self.generic_visit(node)
            del self.scope[-2:]
            return node
        if qualified in self.found:
            raise ValueError(f"Multiple definitions of protected function: {qualified}")
        self.found.add(qualified)
        original = self.originals[qualified]
        if original.co_freevars:
            raise ValueError(f"Protected closures are unsupported: {self.relative}:{qualified}")
        if original.co_flags & 0x200:
            raise ValueError(f"Protected async generators are unsupported: {self.relative}:{qualified}")
        function_id = f"{self.relative}:{qualified}"
        self.bodies[function_id] = original
        positional = [ast.Name(arg.arg, ast.Load()) for arg in [*node.args.posonlyargs, *node.args.args]]
        if node.args.vararg:
            positional.append(ast.Starred(ast.Name(node.args.vararg.arg, ast.Load()), ast.Load()))
        keys = [ast.Constant(arg.arg) for arg in node.args.kwonlyargs]
        values = [ast.Name(arg.arg, ast.Load()) for arg in node.args.kwonlyargs]
        if node.args.kwarg:
            keys.append(None)
            values.append(ast.Name(node.args.kwarg.arg, ast.Load()))
        call = ast.Call(ast.Name("__appguard_invoke__", ast.Load()), [
            ast.Constant(function_id), ast.Call(ast.Name("globals", ast.Load()), [], []),
            ast.Tuple(positional, ast.Load()), ast.Dict(keys, values),
        ], [])
        if isinstance(node, ast.AsyncFunctionDef):
            call = ast.Await(call)
        elif original.co_flags & 0x20:
            call = ast.YieldFrom(call)
        document = []
        if ast.get_docstring(node, clean=False) is not None:
            document = [node.body[0]]
        node.body = [*document, ast.copy_location(ast.Return(call), node)]
        return node

    visit_AsyncFunctionDef = visit_FunctionDef


def encrypt_entry(directory: Path, identifier: str, code: CodeType, key: bytes, aad: bytes, suffix: str) -> dict:
    nonce = os.urandom(12)
    encrypted = nonce + AESGCM(key).encrypt(nonce, marshal.dumps(code), aad)
    filename = hashlib.sha256(identifier.encode()).hexdigest() + suffix
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


def build(source: Path, config_path: Path, key_path: Path, output: Path) -> dict:
    if sys.version_info[:2] != (3, 11):
        raise ValueError("AppGuard v1 requires CPython 3.11 for build and execution")
    if output.exists():
        raise ValueError("Output already exists; select a new release directory")
    source = source.resolve()
    config = tomllib.loads(config_path.read_text())
    unknown = set(config) - {"product_id", "include", "exclude", "checkpoints", "protected_functions"}
    if unknown:
        raise ValueError(f"Unsupported build configuration fields: {sorted(unknown)}")
    include, exclude = config.get("include"), config.get("exclude", [])
    if not isinstance(include, list) or not include or not isinstance(exclude, list):
        raise ValueError("include must be a nonempty list and exclude must be a list")
    paths = collect_files(source, include, exclude)
    publisher = signer(key_path)
    content_key = os.urandom(32)
    bootstrap_key = os.urandom(32)
    build_id = uuid.uuid4().hex
    bundle = output / "bundle"
    tree = bundle / "tree"
    modules = bundle / "modules"
    functions = bundle / "functions"
    modules.mkdir(parents=True)
    functions.mkdir()
    entries = {}
    protected_entries = {}
    configured = config.get("checkpoints", {})
    protected = config.get("protected_functions", {})
    seen = set()
    for path in paths:
        relative = path.relative_to(source).as_posix()
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix != ".py":
            shutil.copy2(path, target)
            continue
        code = ast.parse(path.read_bytes(), filename="/app/" + relative)
        original = compile(copy.deepcopy(code), "/app/" + relative, "exec", dont_inherit=True)
        splitter = FunctionBodies(relative, set(protected.get(relative, [])), code_objects(original))
        code = splitter.visit(code)
        if splitter.selected - splitter.found:
            raise ValueError(f"Missing protected functions in {relative}: {sorted(splitter.selected - splitter.found)}")
        for identifier, body in splitter.bodies.items():
            protected_entries[identifier] = encrypt_entry(
                functions, identifier, body, content_key,
                f"{build_id}:function:{identifier}".encode(), ".agf",
            )
        checkpoints = Checkpoints(set(configured.get(relative, [])))
        code = checkpoints.visit(code)
        missing = checkpoints.functions - checkpoints.found
        if missing:
            raise ValueError(f"Missing checkpoints in {relative}: {sorted(missing)}")
        seen.add(relative)
        ast.fix_missing_locations(code)
        bootstrap = compile(code, "/app/" + relative, "exec", dont_inherit=True)
        entries[relative] = encrypt_entry(
            modules, relative, bootstrap, bootstrap_key,
            f"{build_id}:module:{relative}".encode(), ".agc",
        )
        target.write_text(
            "# AppGuard protected module.\n"
            "from guard_runtime import execute_module as __appguard_load__\n"
            f"__appguard_load__({relative!r}, globals())\n"
        )
    if (set(configured) | set(protected)) - seen:
        raise ValueError(f"Protection files were not included: {sorted((set(configured) | set(protected)) - seen)}")
    if not entries:
        raise ValueError("No Python modules matched the include patterns")
    if not protected_entries:
        raise ValueError("At least one protected function is required")
    manifest = {
        "format": 1, "product_id": config["product_id"], "build_id": build_id,
        "python": "3.11", "layout": "function-bodies-v1", "modules": entries,
        "functions": protected_entries,
        "bootstrap_key_sha256": hashlib.sha256(bootstrap_key).hexdigest(),
    }
    (bundle / "manifest.json").write_bytes(signed(manifest, publisher))
    (bundle / "publisher.pub").write_text(publisher.public_key().public_bytes_raw().hex() + "\n")
    private_write(bundle / "bootstrap.key", bootstrap_key.hex().encode() + b"\n")
    private_write(output / "release.json", canonical({
        "format": 1, "product_id": config["product_id"], "build_id": build_id,
        "content_key": content_key.hex(),
        "publisher_public": publisher.public_key().public_bytes_raw().hex(),
    }))
    return {"build_id": build_id, "modules": len(entries), "protected_functions": len(protected_entries), "bundle": str(bundle)}
