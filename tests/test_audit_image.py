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
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return buffer.getvalue()


def audit(monkeypatch, tmp_path, files, options=()):
    saved = archive_bytes({
        "manifest.json": json.dumps([{"Layers": ["layer.tar"]}]).encode(),
        "layer.tar": archive_bytes(files),
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
        "app/service.py": b"# AppGuard protected module.\n",
        "opt/appguard/bundle/functions/example.agf": b"encrypted",
    })
    assert code == 0
    assert report["passed"]
    assert report["protected_modules"] == report["protected_functions"] == 1


@pytest.mark.parametrize("name,data,problem", [
    ("app/domain/service.py", b"def answer(): pass\n", "Plain business source"),
    ("app/domain/service.pyc", b"bytecode", "Unencrypted business bytecode"),
    ("app/.git/config", b"config", "Private build input"),
    ("opt/appguard/bundle/bootstrap.key", b"key", "Private license material"),
    ("opt/appguard/issuer.key", b"key", "Private license material"),
])
def test_default_layout_rejects_unprotected_code_and_private_inputs(monkeypatch, tmp_path, name, data, problem):
    code, report = audit(monkeypatch, tmp_path, {
        "app/service.py": b"# AppGuard protected module.\n",
        "opt/appguard/bundle/functions/example.agf": b"encrypted",
        name: data,
    })
    assert code == 1
    assert any(item == f"{problem}: {name}" for item in report["problems"])


def test_custom_layout_limits_protection_to_selected_paths(monkeypatch, tmp_path):
    code, report = audit(monkeypatch, tmp_path, {
        "srv/product/core/service.py": b"# AppGuard protected module.\n",
        "srv/product/web.py": b"# AppGuard protected module.\n",
        "srv/product/core-extra/public.py": b"public = True\n",
        "srv/runtime/bundle/functions/example.agf": b"encrypted",
    }, ["--app-root", "/srv/product", "--bundle-root", "/srv/runtime/bundle",
        "--protected-path", "/srv/product/core", "--protected-path", "/srv/product/web.py"])
    assert code == 0
    assert report["protected_modules"] == 2


def test_image_without_protected_functions_fails(monkeypatch, tmp_path):
    code, report = audit(monkeypatch, tmp_path, {"app/service.py": b"# AppGuard protected module.\n"})
    assert code == 1
    assert not report["passed"]
