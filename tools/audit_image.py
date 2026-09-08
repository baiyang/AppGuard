"""Inspect every Docker image layer for protected source and publisher secrets."""

import argparse
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--app-root", type=image_path, default="app", help="Application directory in the image (default: /app)")
    parser.add_argument("--bundle-root", type=image_path, default="opt/appguard/bundle", help="Bundle directory in the image")
    parser.add_argument("--protected-path", type=image_path, action="append", help="Image file or directory to check for protected code; repeatable, defaults to app root")
    args = parser.parse_args()
    protected_paths = args.protected_path or [args.app_root]
    private_roots = [args.app_root, args.bundle_root, *protected_paths]
    bundle_parent = str(PurePosixPath(args.bundle_root).parent)
    if bundle_parent != ".":
        private_roots.append(bundle_parent)
    image_id = subprocess.check_output(["docker", "image", "inspect", args.image, "--format", "{{.Id}}"], text=True).strip()
    problems = []
    protected = set()
    protected_functions = set()
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
                        if not entry.isfile():
                            continue
                        name = entry.name.removeprefix("./").lstrip("/")
                        if within(name, args.bundle_root + "/functions") and name.endswith(".agf"):
                            protected_functions.add(name)
                        owned = any(within(name, path) for path in protected_paths)
                        if owned and name.endswith((".pyc", ".pyo")):
                            problems.append("Unencrypted business bytecode: " + name)
                        if owned and name.endswith(".py"):
                            data = contents.extractfile(entry).read(256)
                            if not data.startswith(b"# AppGuard protected module.\n"):
                                problems.append("Plain business source: " + name)
                            protected.add(name)
                        if within(name, args.app_root) and (Path(name).name in {"pyproject.toml", "uv.lock", "poetry.lock", ".env"} or ".git" in PurePosixPath(name).parts):
                            problems.append("Private build input: " + name)
                        if any(within(name, root) for root in private_roots) and Path(name).name in {"issuer.key", "release.json", "deployment.key", "license.json", "bootstrap.key"}:
                            problems.append("Private license material: " + name)
                        if "/site-packages/appguard/" in name:
                            problems.append("Publisher tooling: " + name)
                        if name.endswith("/direct_url.json"):
                            payload = json.load(contents.extractfile(entry))
                            url = urlsplit(payload.get("url", ""))
                            if url.username is not None or url.password is not None:
                                problems.append("Credentials in distribution provenance: " + name)
    if not layer_count or not protected or not protected_functions:
        problems.append("No image layers, protected modules or protected functions inspected")
    result = {"image_id": image_id, "layers": layer_count, "protected_modules": len(protected),
              "protected_functions": len(protected_functions),
              "passed": not problems, "problems": problems}
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
