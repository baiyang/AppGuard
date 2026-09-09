import io
import json
import tarfile

import pytest

from tools import audit_image


def archive_bytes(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            if isinstance(data, tuple):
                entry.type, entry.linkname = data
                archive.addfile(entry)
                continue
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return buffer.getvalue()


def stub(identifier="service.py"):
    return (
        "# AppGuard protected module.\n"
        "from guard_runtime import execute_module as __appguard_load__\n"
        f"__appguard_load__({identifier!r}, globals())\n"
    ).encode()


def audit(monkeypatch, tmp_path, files, options=(), earlier_layers=()):
    layers = [*earlier_layers, files]
    names = [f"layer-{index}.tar" for index in range(len(layers))]
    saved = archive_bytes({
        "manifest.json": json.dumps([{"Layers": names}]).encode(),
        **{name: archive_bytes(layer) for name, layer in zip(names, layers)},
    })

    def save_image(command, **kwargs):
        destination = command[command.index("-o") + 1]
        audit_image.Path(destination).write_bytes(saved)

    monkeypatch.setattr(audit_image.subprocess, "check_output", lambda *a, **kw: "sha256:test\n")
    monkeypatch.setattr(audit_image.subprocess, "run", save_image)
    report = tmp_path / "audit.json"
    monkeypatch.setattr("sys.argv", ["audit_image.py", "--image", "example", "--out", str(report), *options])
    code = audit_image.main()
    return code, json.loads(report.read_text())


def test_default_layout_accepts_generic_application(monkeypatch, tmp_path):
    code, report = audit(monkeypatch, tmp_path, {
        "app/service.py": stub(),
        "opt/appguard/bundle/modules/example.agc": b"encrypted",
    })
    assert code == 0
    assert report["passed"]
    assert report["protected_modules"] == report["encrypted_modules"] == 1


@pytest.mark.parametrize("name,data,problem", [
    ("app/domain/service.py", b"def answer(): pass\n", "Plain business source"),
    ("app/domain/service.pyw", b"def answer(): pass\n", "Plain business source"),
    ("app/domain/service.pyc", b"bytecode", "Unencrypted business bytecode"),
    ("app/domain/__pycache__/service.cpython-311.pyc", b"bytecode", "Unencrypted business bytecode"),
    ("app/domain/service.c", b"generated C", "Native build source"),
    ("app/domain/service.pyx", b"Cython source", "Native build source"),
    ("app/.git/config", b"config", "Private build input"),
    ("app/.env.production", b"config", "Private build input"),
    ("app/service.py", stub() + b"secret = 123\n", "Plain business source"),
    ("app/service.py", stub() + b"# secret = 123\n", "Plain business source"),
    ("app/service.py", b"# AppGuard protected module.\n", "Plain business source"),
    ("app/service.py", stub("../outside.py"), "Plain business source"),
    ("opt/appguard/bundle/functions/old.agf", b"encrypted", "Obsolete protected function"),
    ("opt/appguard/bundle/bootstrap.key", b"key", "Private license material"),
    ("opt/appguard/issuer.key", b"key", "Private license material"),
    ("tmp/code.key", b"key", "Private license material"),
    ("tmp/license.json", b"license", "Private license material"),
    ("tmp/release.json", b"release", "Private license material"),
    ("usr/local/lib/python3.11/site-packages/appguard/crypto.py", b"source", "Publisher tooling"),
])
def test_default_layout_rejects_unprotected_code_and_private_inputs(monkeypatch, tmp_path, name, data, problem):
    code, report = audit(monkeypatch, tmp_path, {
        "app/service.py": stub(),
        "opt/appguard/bundle/modules/example.agc": b"encrypted",
        name: data,
    })
    assert code == 1
    assert any(item == f"{problem}: {name}" for item in report["problems"])


def test_custom_layout_limits_protection_to_selected_paths(monkeypatch, tmp_path):
    code, report = audit(monkeypatch, tmp_path, {
        "srv/product/core/service.py": stub("core/service.py"),
        "srv/product/web.py": stub("web.py"),
        "srv/product/core-extra/public.py": b"public = True\n",
        "srv/runtime/bundle/modules/example.agc": b"encrypted",
    }, ["--app-root", "/srv/product", "--bundle-root", "/srv/runtime/bundle",
        "--protected-path", "/srv/product/core", "--protected-path", "/srv/product/web.py"])
    assert code == 0
    assert report["protected_modules"] == 2


@pytest.mark.parametrize("name,problem", [
    ("srv/product/web.pyc", "Unencrypted business bytecode"),
    ("srv/product/__pycache__/web.cpython-311.pyc", "Unencrypted business bytecode"),
    ("srv/product/web.c", "Native build source"),
])
def test_file_selection_also_checks_generated_artifacts(monkeypatch, tmp_path, name, problem):
    code, report = audit(monkeypatch, tmp_path, {
        "srv/product/web.py": stub("web.py"),
        "opt/appguard/bundle/modules/example.agc": b"encrypted",
        name: b"unprotected code",
    }, ["--app-root", "/srv/product", "--protected-path", "/srv/product/web.py"])
    assert code == 1
    assert f"{problem}: {name}" in report["problems"]


def test_image_without_encrypted_modules_fails(monkeypatch, tmp_path):
    code, report = audit(monkeypatch, tmp_path, {"app/service.py": stub()})
    assert code == 1
    assert not report["passed"]


@pytest.mark.parametrize("link_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_links_cannot_hide_protected_code_or_secret_files(monkeypatch, tmp_path, link_type):
    code, report = audit(monkeypatch, tmp_path, {
        "app/service.py": stub(),
        "opt/appguard/bundle/modules/example.agc": b"encrypted",
        "app/other.py": (link_type, "outside/source.py"),
        "tmp/code.key": (link_type, "outside/value"),
    })
    assert code == 1
    assert "Linked protected file: app/other.py" in report["problems"]
    assert "Private license material: tmp/code.key" in report["problems"]


@pytest.mark.parametrize("name,data,whiteout,problem", [
    ("app/service.py", b"secret = 123\n", "app/.wh.service.py", "Plain business source"),
    ("tmp/code.key", b"key", "tmp/.wh.code.key", "Private license material"),
])
def test_deleted_lower_layer_secrets_and_source_still_fail(monkeypatch, tmp_path, name, data, whiteout, problem):
    code, report = audit(monkeypatch, tmp_path, {
        "app/service.py": stub(),
        "opt/appguard/bundle/modules/example.agc": b"encrypted",
        whiteout: b"",
    }, earlier_layers=[{name: data}])
    assert code == 1
    assert report["layers"] == 2
    assert f"{problem}: {name}" in report["problems"]


def test_distribution_url_credentials_fail(monkeypatch, tmp_path):
    name = "usr/local/lib/python3.11/site-packages/example.dist-info/direct_url.json"
    code, report = audit(monkeypatch, tmp_path, {
        "app/service.py": stub(),
        "opt/appguard/bundle/modules/example.agc": b"encrypted",
        name: json.dumps({"url": "https://user:password@example.test/project.git"}).encode(),
    })
    assert code == 1
    assert f"Credentials in distribution provenance: {name}" in report["problems"]
