"""Check official PyPI artifact hashes and optionally install in a fresh environment."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def remote_files(name, version):
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise
    return {file["filename"]: file["digests"]["sha256"] for file in payload["urls"]}


def verify(expected, actual, allow_missing):
    extra = set(actual) - set(expected)
    if extra:
        raise ValueError(f"Unexpected PyPI files: {sorted(extra)}")
    for filename in expected.keys() & actual.keys():
        if expected[filename] != actual[filename]:
            raise ValueError(f"PyPI already contains different bytes for {filename}; do not reuse this version")
    missing = set(expected) - set(actual)
    if missing and not allow_missing:
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--wait", type=int, default=0, metavar="SECONDS")
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    if args.install and args.allow_missing:
        parser.error("--install requires all files to be published")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    name, version = project["name"], project["version"]
    filenames = {f"appguard_runtime-{version}-py3-none-any.whl", f"appguard_runtime-{version}.tar.gz"}
    files = {path.name: path for path in args.dist.iterdir() if path.is_file()}
    if set(files) != filenames:
        parser.error(f"Expected only the public wheel and sdist: {sorted(filenames)}")
    expected = {filename: hashlib.sha256(path.read_bytes()).hexdigest() for filename, path in files.items()}
    deadline = time.monotonic() + args.wait
    while True:
        try:
            actual = remote_files(name, version)
            if verify(expected, actual, args.allow_missing):
                break
        except (urllib.error.URLError, TimeoutError) as exc:
            if time.monotonic() >= deadline:
                raise
            print(f"Waiting for PyPI: {exc}", flush=True)
        if time.monotonic() >= deadline:
            parser.error("The release is not fully available on official PyPI")
        time.sleep(min(10, max(0, deadline - time.monotonic())))
    print(f"PyPI hashes match for {len(actual)} published files", flush=True)
    if args.install:
        with tempfile.TemporaryDirectory(prefix="appguard-pypi-") as directory:
            env_dir = Path(directory) / "venv"
            venv.EnvBuilder(with_pip=True).create(env_dir)
            python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("APPGUARD_", "PYTHONPATH", "PYTHONHOME", "PIP_"))}
            subprocess.run([str(python), "-m", "pip", "--isolated", "install", "--no-cache-dir",
                            "--index-url", "https://pypi.org/simple", f"{name}=={version}"],
                           cwd=directory, env=env, check=True, timeout=180)
            subprocess.run([str(python), "-I", "-c",
                            "import importlib.metadata, sys; "
                            "assert importlib.metadata.version('appguard-runtime') == sys.argv[1]", version],
                           cwd=directory, env=env, check=True, timeout=30)
            subprocess.run([str(python), "-I", "-m", "appguard", "--help"],
                           cwd=directory, env=env, check=True, timeout=30)
            subprocess.run([str(python), "-I", "-m", "appguard", "keygen", "--out", "issuer.key"],
                           cwd=directory, env=env, check=True, timeout=30)
        print(f"Official PyPI installation verified: {name}=={version}")


if __name__ == "__main__":
    main()
