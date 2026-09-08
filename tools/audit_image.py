"""Inspect every Docker image layer for protected source and publisher secrets."""

import argparse
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
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
                        if name.startswith("opt/appguard/bundle/functions/") and name.endswith(".agf"):
                            protected_functions.add(name)
                        owned = name.startswith(("app/backend/app/", "app/backend/lib/", "app/backend/scripts/")) or name == "app/backend/web_app.py"
                        if owned and name.endswith((".pyc", ".pyo")):
                            problems.append("Unencrypted business bytecode: " + name)
                        if owned and name.endswith(".py"):
                            data = contents.extractfile(entry).read(256)
                            if not data.startswith(b"# AppGuard protected module.\n"):
                                problems.append("Plain business source: " + name)
                            protected.add(name)
                        if name in {"app/backend/pyproject.toml", "app/backend/uv.lock", "app/.env"} or name.startswith("app/.git/"):
                            problems.append("Private build input: " + name)
                        if name.startswith(("app/", "opt/appguard/")) and Path(name).name in {"issuer.key", "release.json", "deployment.key", "license.json", "bootstrap.key"}:
                            problems.append("Private license material: " + name)
                        if "/site-packages/appguard/" in name:
                            problems.append("Publisher tooling: " + name)
                        if name.endswith("/direct_url.json"):
                            payload = json.load(contents.extractfile(entry))
                            url = urlsplit(payload.get("url", ""))
                            if url.username is not None or url.password is not None:
                                problems.append("Credentials in distribution provenance: " + name)
    if not layer_count or not protected:
        problems.append("No image layers or protected modules inspected")
    result = {"image_id": image_id, "layers": layer_count, "protected_modules": len(protected),
              "protected_functions": len(protected_functions),
              "passed": not problems, "problems": problems}
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
