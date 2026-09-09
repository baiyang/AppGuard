"""Runtime compiler inputs and failure handling must preserve product secrets."""

import subprocess

import pytest

from appguard.runtime import build_runtime


def test_runtime_build_rejects_bad_keys_before_creating_output(tmp_path):
    public, code = tmp_path / "issuer.pub", tmp_path / "code.key"
    public.write_text("00" * 32)
    code.write_text("invalid")
    output = tmp_path / "runtime"
    with pytest.raises(ValueError, match="32-byte hexadecimal"):
        build_runtime(public, code, output)
    assert not output.exists()


def test_runtime_build_does_not_overwrite_existing_output(tmp_path):
    output = tmp_path / "runtime"
    output.mkdir()
    with pytest.raises(ValueError, match="Output already exists"):
        build_runtime(tmp_path / "missing.pub", tmp_path / "missing.key", output)
    assert output.is_dir()


def test_compiler_failure_redacts_key_macros_and_removes_temporary_sources(tmp_path, monkeypatch):
    public, code = tmp_path / "issuer.pub", tmp_path / "code.key"
    public.write_text("ab" * 32)
    code.write_text("cd" * 32)
    compiled = []

    def fail(command, **options):
        compiled.append(options["cwd"])
        return subprocess.CompletedProcess(command, 1,
                                           options["env"]["APPGUARD_PUBLIC_KEY"],
                                           options["env"]["APPGUARD_CODE_KEY"])

    monkeypatch.setattr(subprocess, "run", fail)
    output = tmp_path / "runtime"
    with pytest.raises(RuntimeError) as raised:
        build_runtime(public, code, output)
    assert public.read_text() not in str(raised.value)
    assert code.read_text() not in str(raised.value)
    assert "[redacted key]" in str(raised.value)
    assert not output.exists()
    assert not compiled[0].exists()
