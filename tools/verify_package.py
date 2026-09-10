"""Verify public artifacts and a real installed publisher-to-customer workflow."""

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import venv
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


TEMPLATE_FILES = {"pyproject.toml", "setup.py", "guard_runtime.pyx", "LICENSE",
                  "appguard_host.py", "appguard_flask.py", "appguard_fastapi.py"}
RUNTIME_MODULES = {"appguard_host.py", "appguard_flask.py", "appguard_fastapi.py"}


def audit_wheel(path: Path, *, private: bool = False) -> str:
    with ZipFile(path) as archive:
        names = archive.namelist()
        metadata_paths = [name for name in names if name.endswith(".dist-info/METADATA")]
        assert len(metadata_paths) == 1, "Expected exactly one distribution metadata file"
        metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
        expected_name = "appguard-product-runtime" if private else "appguard-runtime"
        assert metadata["Name"] == expected_name, metadata["Name"]
        assert metadata["License-Expression"] == "MIT"
        license_path = metadata_paths[0].replace("METADATA", "licenses/LICENSE")
        license_text = archive.read(license_path)
        assert b"MIT License" in license_text
        for name in names:
            parts = PurePosixPath(name)
            assert not parts.is_absolute() and ".." not in parts.parts, name
            assert parts.suffix not in {".key", ".pub", ".license", ".c", ".pyc", ".pyo"}, name
        if private:
            assert "Private :: Do Not Upload" in metadata.get_all("Classifier", [])
            assert RUNTIME_MODULES.issubset(names)
            assert "fastapi" in metadata.get_all("Provides-Extra", [])
            assert any(requirement.startswith("fastapi") and 'extra == "fastapi"' in requirement
                       for requirement in metadata.get_all("Requires-Dist", []))
            assert any(name.startswith("guard_runtime.") and name.endswith((".so", ".pyd")) for name in names)
            assert not any(name.endswith(".pyx") or name.startswith("appguard/") for name in names)
        else:
            assert path.name.endswith("-py3-none-any.whl"), path.name
            assert not any(name.endswith((".so", ".pyd", ".dll", ".dylib")) for name in names)
            assert {f"appguard/_runtime/{name}" for name in TEMPLATE_FILES}.issubset(names)
            assert archive.read("appguard/_runtime/LICENSE") == license_text
            assert not RUNTIME_MODULES.intersection(names)
            entrypoints = archive.read(metadata_paths[0].replace("METADATA", "entry_points.txt")).decode()
            assert "appguard = appguard.__main__:main" in entrypoints
        return metadata["Version"]


def audit_sdist(path: Path) -> None:
    with tarfile.open(path) as archive:
        names = set()
        for member in archive.getmembers():
            parts = PurePosixPath(member.name)
            assert not parts.is_absolute() and ".." not in parts.parts, member.name
            assert not member.issym() and not member.islnk(), member.name
            assert parts.suffix not in {".key", ".pub", ".license", ".c", ".pyc", ".pyo", ".so", ".pyd"}, member.name
            assert ".data" not in parts.parts, member.name
            names.add(PurePosixPath(*parts.parts[1:]).as_posix())
        assert {f"appguard/_runtime/{name}" for name in TEMPLATE_FILES}.issubset(names)
        assert {"README.md", "LICENSE", "CHANGELOG.md"}.issubset(names)


def run(command, cwd: Path, env: dict) -> str:
    result = subprocess.run(list(map(str, command)), cwd=cwd, env=env, text=True,
                            capture_output=True, timeout=900)
    if result.returncode:
        raise RuntimeError(f"Command failed: {command[:3]}\n{result.stdout[-4000:]}{result.stderr[-8000:]}")
    return result.stdout


def environment(path: Path) -> tuple[Path, Path]:
    venv.EnvBuilder(with_pip=True).create(path)
    scripts = path / ("Scripts" if os.name == "nt" else "bin")
    return scripts / ("python.exe" if os.name == "nt" else "python"), scripts / ("appguard.exe" if os.name == "nt" else "appguard")


def verify(wheel: Path, sdist: Path) -> None:
    version = audit_wheel(wheel)
    audit_sdist(sdist)
    clean_env = {key: value for key, value in os.environ.items()
                 if key not in {"PYTHONPATH", "PYTHONHOME"} and not key.startswith("APPGUARD_")}
    clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="appguard-package-check-") as temporary:
        work = Path(temporary)
        publisher, cli = environment(work / "publisher")
        run([publisher, "-m", "pip", "install", "--disable-pip-version-check", wheel], work, clean_env)
        run([publisher, "-m", "pip", "check"], work, clean_env)
        run([cli, "--help"], work, clean_env)
        run([publisher, "-c", "import appguard; from importlib.metadata import version; "
             f"assert version('appguard-runtime') == {version!r}; "
             "assert 'site-packages' in appguard.__file__"], work, clean_env)

        # Rebuild the sdist without keys, then replace the installed wheel with that build.
        rebuilt = work / "rebuilt"
        run([publisher, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", rebuilt, sdist], work, clean_env)
        rebuilt_wheels = list(rebuilt.glob("*.whl"))
        assert len(rebuilt_wheels) == 1
        assert audit_wheel(rebuilt_wheels[0]) == version
        with ZipFile(wheel) as original, ZipFile(rebuilt_wheels[0]) as rebuilt_archive:
            original_sources = {name: original.read(name) for name in original.namelist() if name.startswith("appguard/")}
            rebuilt_sources = {name: rebuilt_archive.read(name) for name in rebuilt_archive.namelist() if name.startswith("appguard/")}
            assert original_sources == rebuilt_sources, "sdist and wheel contain different package sources"
        run([publisher, "-m", "pip", "install", "--force-reinstall", "--no-deps", rebuilt_wheels[0]], work, clean_env)

        issuer, public, code_key = work / "issuer.key", work / "issuer.pub", work / "code.key"
        run([cli, "keygen", "--out", issuer], work, clean_env)
        run([publisher, "-m", "appguard", "code-keygen", "--out", code_key], work, clean_env)
        source = work / "source"
        source.mkdir()
        (source / "service.py").write_text("def answer(value):\n    return value + 42\n", encoding="utf-8")
        (source / "web_app.py").write_text(
            "from flask import Flask\nfrom appguard_flask import AppGuard\nfrom service import answer\n"
            "app = Flask(__name__)\n@app.get('/answer')\ndef calculate():\n    return {'answer': answer(8)}\n"
            "AppGuard().init_app(app)\n", encoding="utf-8")
        (source / "fastapi_app.py").write_text(
            "from fastapi import FastAPI\nfrom appguard_fastapi import AppGuard\nfrom service import answer\n"
            "app = FastAPI()\n@app.get('/answer')\ndef calculate():\n    return {'answer': answer(8)}\n"
            "@app.get('/healthz')\ndef health():\n    return {'ok': True}\n"
            "AppGuard(app, exempt_paths=('/healthz',))\n", encoding="utf-8")
        config = work / "guard.toml"
        config.write_text('product_id = "package-check"\ninclude = ["*.py"]\n', encoding="utf-8")
        release = work / "release"
        run([cli, "build", "--source", source, "--config", config, "--issuer-key", issuer,
             "--code-key", code_key, "--out", release], work, clean_env)
        license_path = work / "customer.license"
        run([cli, "issue", "--product", "package-check", "--customer", "Package check",
             "--expires", "2099-01-01T00:00:00Z", "--issuer-key", issuer, "--out", license_path], work, clean_env)
        inspected = json.loads(run([cli, "inspect", license_path], work, clean_env))
        assert inspected["metadata"]["product_id"] == "package-check"
        runtime_result = json.loads(run([cli, "build-runtime", "--public-key", public,
                                        "--code-key", code_key, "--out", work / "runtime"], work, clean_env))
        native_wheel = Path(runtime_result["wheel"])
        assert audit_wheel(native_wheel, private=True) == version, "Public and runtime versions differ"

        customer, _ = environment(work / "customer")
        run([customer, "-m", "pip", "install", f"{native_wheel}[fastapi]",
             "Flask>=3.1,<4", "httpx>=0.27,<1"], work, clean_env)
        run([customer, "-m", "pip", "check"], work, clean_env)
        deployment_env = dict(clean_env, APPGUARD_BUNDLE=str(release / "bundle"),
                              APPGUARD_LICENSE_DIR=str(work / "license"),
                              PYTHONPATH=str(release / "bundle" / "tree"))
        run([customer, "-c", "import importlib.util; assert importlib.util.find_spec('appguard') is None; "
             "from web_app import app; client = app.test_client(); "
             "assert client.get('/answer').status_code == 403; "
             "assert client.get('/_license/').status_code == 200"], work, deployment_env)
        run([customer, "-c", "from fastapi.testclient import TestClient\nfrom fastapi_app import app\n"
             "with TestClient(app) as client:\n"
             "    for path in ('/answer', '/docs', '/openapi.json'):\n"
             "        assert client.get(path).status_code == 403\n"
             "    assert client.get('/_license/').status_code == 200\n"
             "    assert client.get('/healthz').status_code == 200\n"], work, deployment_env)
        run([customer, "-m", "appguard_host", "install", license_path], work, deployment_env)
        run([customer, "-c", "from web_app import app; response = app.test_client().get('/answer'); "
             "assert response.status_code == 200; assert response.get_json() == {'answer': 50}"], work, deployment_env)
        run([customer, "-c", "from fastapi.testclient import TestClient\nfrom fastapi_app import app\n"
             "with TestClient(app) as client:\n"
             "    response = client.get('/answer')\n"
             "    assert response.status_code == 200\n"
             "    assert response.json() == {'answer': 50}\n"], work, deployment_env)
    print(f"Verified appguard-runtime {version}: wheel, sdist rebuild, installed CLI, native build, "
          "and Flask/FastAPI customer licensing")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("sdist", type=Path)
    args = parser.parse_args()
    verify(args.wheel.resolve(), args.sdist.resolve())


if __name__ == "__main__":
    main()
