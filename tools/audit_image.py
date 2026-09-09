"""Inspect every Docker image layer for protected source and publisher secrets."""

import argparse
import ast
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


def image_path(value):
    path = PurePosixPath(value)
    if ".." in path.parts or str(path).strip("/") in {"", "."}:
        raise argparse.ArgumentTypeError("Expected an image file or directory path")
    return str(path).lstrip("/")


def within(name, root):
    return name == root or name.startswith(root + "/")


def protected_path(name, root):
    if within(name, root):
        return True
    source, candidate = PurePosixPath(root), PurePosixPath(name)
    if source.suffix != ".py":
        return False
    if candidate.parent == source.parent and candidate.stem == source.stem:
        return True
    return (candidate.parent == source.parent / "__pycache__"
            and candidate.name.startswith(source.stem + "."))


def loader_stub(data):
    if not data.startswith(b"# AppGuard protected module.\n"):
        return False
    try:
        tree = ast.parse(data)
        identifier = tree.body[1].value.args[0].value
        if not isinstance(identifier, str) or not identifier.endswith(".py"):
            return False
        path = PurePosixPath(identifier)
        if path.is_absolute() or ".." in path.parts:
            return False
        expected = (
            "# AppGuard protected module.\n"
            "from guard_runtime import execute_module as __appguard_load__\n"
            f"__appguard_load__({identifier!r}, globals())\n"
        )
        return data == expected.encode("utf-8")
    except (SyntaxError, ValueError, IndexError, AttributeError, TypeError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--app-root", type=image_path, default="app", help="Application directory in the image (default: /app)")
    parser.add_argument("--bundle-root", type=image_path, default="opt/appguard/bundle", help="Bundle directory in the image")
    parser.add_argument("--protected-path", type=image_path, action="append", help="Image file or directory to check for protected code; repeatable, defaults to app root")
    args = parser.parse_args()
    protected_paths = args.protected_path or [args.app_root]
    image_id = subprocess.check_output(["docker", "image", "inspect", args.image, "--format", "{{.Id}}"], text=True).strip()
    problems = []
    protected = set()
    encrypted_modules = set()
    layer_count = 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="image-audit-", dir=args.out.parent) as temp:
        archive = Path(temp) / "image.tar"
        subprocess.run(["docker", "image", "save", "-o", str(archive), image_id], check=True)
        with tarfile.open(archive) as image:
            manifests = json.load(image.extractfile("manifest.json"))
            layers = dict.fromkeys(layer for manifest in manifests for layer in manifest["Layers"])
            for layer in layers:
                layer_count += 1
                with tarfile.open(fileobj=image.extractfile(layer), mode="r|*") as contents:
                    for entry in contents:
                        name = entry.name.removeprefix("./").lstrip("/")
                        owned = any(protected_path(name, path) for path in protected_paths)
                        if owned and (entry.issym() or entry.islnk()):
                            problems.append("Linked protected file: " + name)
                        basename = PurePosixPath(name).name
                        if not entry.isdir() and basename in {"issuer.key", "code.key", "release.json", "deployment.key", "license.json", "bootstrap.key"}:
                            problems.append("Private license material: " + name)
                        if not entry.isfile():
                            continue
                        if within(name, args.bundle_root + "/modules") and name.endswith(".agc"):
                            encrypted_modules.add(name)
                        if within(name, args.bundle_root) and name.endswith(".agf"):
                            problems.append("Obsolete protected function: " + name)
                        if owned and name.endswith((".pyc", ".pyo")):
                            problems.append("Unencrypted business bytecode: " + name)
                        if owned and name.endswith(".pyw"):
                            problems.append("Plain business source: " + name)
                        if owned and name.endswith(".py"):
                            data = contents.extractfile(entry).read(65537)
                            if len(data) > 65536 or not loader_stub(data):
                                problems.append("Plain business source: " + name)
                            else:
                                protected.add(name)
                        if owned and name.endswith((".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".pyx", ".pxd", ".pxi")):
                            problems.append("Native build source: " + name)
                        if within(name, args.app_root) and (basename in {"pyproject.toml", "uv.lock", "poetry.lock", ".env"} or basename.startswith(".env.") or ".git" in PurePosixPath(name).parts):
                            problems.append("Private build input: " + name)
                        if "/site-packages/appguard/" in name:
                            problems.append("Publisher tooling: " + name)
                        if name.endswith("/direct_url.json"):
                            payload = json.load(contents.extractfile(entry))
                            url = urlsplit(payload.get("url", ""))
                            if url.username is not None or url.password is not None:
                                problems.append("Credentials in distribution provenance: " + name)
    if not layer_count or not protected or not encrypted_modules:
        problems.append("No image layers, protected stubs or encrypted modules inspected")
    result = {"image_id": image_id, "layers": layer_count, "protected_modules": len(protected),
              "encrypted_modules": len(encrypted_modules),
              "passed": not problems, "problems": problems}
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
