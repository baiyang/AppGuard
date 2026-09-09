"""Compile a customer runtime using build sources bundled in the public wheel."""

import os
import subprocess
import sys
import tempfile
from importlib import resources
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .crypto import private_write, read_key


def build_runtime(public_key_path: Path, code_key_path: Path, output: Path,
                  *, isolation: bool = True) -> dict:
    if sys.version_info[:2] != (3, 11) or sys.implementation.name != "cpython":
        raise ValueError("AppGuard requires CPython 3.11 for build and execution")
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; select a new runtime directory")
    public_key = read_key(public_key_path).hex()
    code_key = read_key(code_key_path).hex()
    try:
        release_version = version("appguard-runtime")
    except PackageNotFoundError:
        raise RuntimeError("Install the appguard-runtime publisher package before building a runtime") from None
    template = resources.files("appguard").joinpath("_runtime")
    with tempfile.TemporaryDirectory(prefix="appguard-runtime-") as temporary:
        project = Path(temporary)
        for name in ("pyproject.toml", "setup.py", "guard_runtime.pyx", "LICENSE",
                     "appguard_host.py", "appguard_flask.py"):
            (project / name).write_bytes(template.joinpath(name).read_bytes())
        command = [sys.executable, "-m", "build", "--wheel", "--outdir", str(project / "dist")]
        if not isolation:
            command.append("--no-isolation")
        env = dict(os.environ, APPGUARD_PUBLIC_KEY=public_key, APPGUARD_CODE_KEY=code_key,
                   APPGUARD_VERSION=release_version)
        # Compiler commands contain key macros, so only emit redacted diagnostics on failure.
        try:
            result = subprocess.run(command, cwd=project, env=env, capture_output=True,
                                    text=True, timeout=600)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Runtime build timed out after 600 seconds") from None
        if result.returncode:
            detail = (result.stdout + result.stderr)[-8000:]
            for value in (public_key, code_key):
                detail = detail.replace(value, "[redacted key]")
            raise RuntimeError("Runtime build failed. Install a C compiler and CPython 3.11 headers.\n" + detail)
        wheels = list((project / "dist").glob("appguard_product_runtime-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("Runtime build did not produce exactly one product wheel")
        output.mkdir(parents=True, mode=0o700)
        wheel = output / wheels[0].name
        try:
            private_write(wheel, wheels[0].read_bytes())
        except BaseException:
            wheel.unlink(missing_ok=True)
            output.rmdir()
            raise
    return {"wheel": str(wheel), "distribution": "appguard-product-runtime"}
