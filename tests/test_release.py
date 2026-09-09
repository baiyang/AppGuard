"""Release guards reject tag mistakes and conflicting public artifacts."""

import hashlib
import importlib.util
import json
import sys
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_release = load_tool("check_release")
verify_pypi = load_tool("verify_pypi")


@pytest.fixture
def release_project(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "appguard-runtime"\nversion = "0.0.1"\n'
    )
    (tmp_path / "CHANGELOG.md").write_text("# Changes\n\n## [0.0.1] - 2026-09-09\n")
    monkeypatch.setattr(check_release, "ROOT", tmp_path)
    monkeypatch.setattr(verify_pypi, "ROOT", tmp_path)
    return tmp_path


def test_release_accepts_matching_tag(release_project, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_release.py", "--ref", "refs/tags/v0.0.1"])
    check_release.main()
    assert "Validated appguard-runtime 0.0.1" in capsys.readouterr().out


@pytest.mark.parametrize("ref", [
    "refs/heads/main", "refs/heads/v0.0.1", "refs/tags/0.0.1",
    "refs/tags/v0.0.2", "refs/tags/v0.0.1rc1", "refs/tags/v0.0.1\n", "",
])
def test_release_rejects_mismatched_or_branch_ref(release_project, monkeypatch, ref):
    monkeypatch.setattr(sys, "argv", ["check_release.py", "--ref", ref])
    with pytest.raises(SystemExit) as error:
        check_release.main()
    assert error.value.code == 2


@pytest.mark.parametrize("name,version", [
    ("appguard-product-runtime", "0.0.1"),
    ("appguard-runtime", "00.0.1"),
    ("appguard-runtime", "0.0.1rc1"),
    ("appguard-runtime", "0.0.1+local"),
])
def test_release_rejects_private_name_and_nonstable_version(
    release_project, monkeypatch, name, version
):
    (release_project / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
    )
    monkeypatch.setattr(sys, "argv", ["check_release.py"])
    with pytest.raises(SystemExit) as error:
        check_release.main()
    assert error.value.code == 2


def test_release_requires_current_changelog_entry(release_project, monkeypatch):
    (release_project / "CHANGELOG.md").write_text("## [0.0.10]\n")
    monkeypatch.setattr(sys, "argv", ["check_release.py"])
    with pytest.raises(SystemExit) as error:
        check_release.main()
    assert error.value.code == 2


@pytest.mark.parametrize("allow_missing", [False, True])
def test_pypi_rejects_hash_conflict_even_during_retry(allow_missing):
    with pytest.raises(ValueError, match="different bytes"):
        verify_pypi.verify({"package.whl": "expected"}, {"package.whl": "changed"}, allow_missing)


@pytest.mark.parametrize("allow_missing", [False, True])
def test_pypi_rejects_unexpected_file_even_during_retry(allow_missing):
    with pytest.raises(ValueError, match="Unexpected PyPI files"):
        verify_pypi.verify({"package.whl": "expected"}, {"private.whl": "private"}, allow_missing)


@pytest.mark.parametrize("actual", [{}, {"package.whl": "wheel"}])
def test_pypi_partial_upload_is_allowed_only_before_publishing(actual):
    expected = {"package.whl": "wheel", "package.tar.gz": "sdist"}
    assert verify_pypi.verify(expected, actual, allow_missing=True)
    assert not verify_pypi.verify(expected, actual, allow_missing=False)


def test_pypi_accepts_complete_matching_release():
    expected = {"package.whl": "wheel", "package.tar.gz": "sdist"}
    assert verify_pypi.verify(expected, dict(expected), allow_missing=False)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_pypi_http_error_is_not_treated_as_missing(monkeypatch, status):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("https://pypi.org/", status, "failure", {}, None)

    monkeypatch.setattr(verify_pypi.urllib.request, "urlopen", fail)
    with pytest.raises(urllib.error.HTTPError):
        verify_pypi.remote_files("appguard-runtime", "0.0.1")


def test_pypi_404_permits_first_publication(monkeypatch):
    def missing(*args, **kwargs):
        raise urllib.error.HTTPError("https://pypi.org/", 404, "not found", {}, None)

    monkeypatch.setattr(verify_pypi.urllib.request, "urlopen", missing)
    assert verify_pypi.remote_files("appguard-runtime", "0.0.1") == {}


def test_pypi_uses_official_version_endpoint_and_sha256(monkeypatch):
    def response(url, timeout):
        assert url == "https://pypi.org/pypi/appguard-runtime/0.0.1/json"
        assert timeout > 0
        return BytesIO(json.dumps({"urls": [
            {"filename": "package.whl", "digests": {"sha256": "verified"}},
        ]}).encode())

    monkeypatch.setattr(verify_pypi.urllib.request, "urlopen", response)
    assert verify_pypi.remote_files("appguard-runtime", "0.0.1") == {"package.whl": "verified"}


@pytest.fixture
def release_files(release_project):
    dist = release_project / "dist"
    dist.mkdir()
    files = {
        "appguard_runtime-0.0.1-py3-none-any.whl": b"wheel contents",
        "appguard_runtime-0.0.1.tar.gz": b"sdist contents",
    }
    for filename, content in files.items():
        (dist / filename).write_bytes(content)
    return dist, {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}


def test_pypi_cli_hashes_both_local_artifacts(release_files, monkeypatch, capsys):
    dist, expected = release_files
    monkeypatch.setattr(sys, "argv", ["verify_pypi.py", "--dist", str(dist)])
    monkeypatch.setattr(verify_pypi, "remote_files", lambda name, version: expected)
    verify_pypi.main()
    assert "PyPI hashes match for 2 published files" in capsys.readouterr().out


def test_pypi_cli_rejects_private_artifact_before_network(release_files, monkeypatch):
    dist, _ = release_files
    (dist / "appguard_product_runtime-0.0.1.whl").write_bytes(b"private")
    monkeypatch.setattr(sys, "argv", ["verify_pypi.py", "--dist", str(dist), "--allow-missing"])

    def unexpected_call(*args):
        pytest.fail("Invalid artifact sets must fail before querying PyPI")

    monkeypatch.setattr(verify_pypi, "remote_files", unexpected_call)
    with pytest.raises(SystemExit) as error:
        verify_pypi.main()
    assert error.value.code == 2


def test_pypi_cli_waits_for_partial_publication(release_files, monkeypatch):
    dist, expected = release_files
    answers = iter([{}, {next(iter(expected)): next(iter(expected.values()))}, expected])
    clock = [0]
    monkeypatch.setattr(sys, "argv", ["verify_pypi.py", "--dist", str(dist), "--wait", "30"])
    monkeypatch.setattr(verify_pypi, "remote_files", lambda name, version: next(answers))
    monkeypatch.setattr(verify_pypi.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(verify_pypi.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    verify_pypi.main()
    assert clock[0] == 20


def test_pypi_cli_fails_when_publication_remains_partial(release_files, monkeypatch):
    dist, _ = release_files
    clock = [0]
    monkeypatch.setattr(sys, "argv", ["verify_pypi.py", "--dist", str(dist), "--wait", "15"])
    monkeypatch.setattr(verify_pypi, "remote_files", lambda name, version: {})
    monkeypatch.setattr(verify_pypi.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(verify_pypi.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    with pytest.raises(SystemExit) as error:
        verify_pypi.main()
    assert error.value.code == 2
    assert clock[0] == 15


def test_pypi_cli_rejects_install_with_partial_files(release_project, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["verify_pypi.py", "--allow-missing", "--install"])
    with pytest.raises(SystemExit) as error:
        verify_pypi.main()
    assert error.value.code == 2
