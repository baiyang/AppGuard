"""Compile a reusable runtime with a publisher public key and product code key."""

import os
import re

from Cython.Build import cythonize
from setuptools import Extension, setup

version = os.environ.get("APPGUARD_VERSION", "")
if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
    raise RuntimeError("APPGUARD_VERSION must be the publisher toolkit's release version")
public_key = os.environ.get("APPGUARD_PUBLIC_KEY", "")
if len(public_key) != 64 or any(c not in "0123456789abcdef" for c in public_key):
    raise RuntimeError("APPGUARD_PUBLIC_KEY must be the publisher's 32-byte hex public key")
code_key = os.environ.get("APPGUARD_CODE_KEY", "")
if len(code_key) != 64 or any(c not in "0123456789abcdef" for c in code_key):
    raise RuntimeError("APPGUARD_CODE_KEY must be the product's 32-byte hex code key")

setup(
    version=version,
    py_modules=["appguard_host", "appguard_flask"],
    ext_modules=cythonize(
        [Extension("guard_runtime", ["guard_runtime.pyx"],
                   define_macros=[("APPGUARD_PUBLIC_KEY", '"' + public_key + '"'),
                                  ("APPGUARD_CODE_KEY", '"' + code_key + '"')])],
        compiler_directives={"language_level": 3, "embedsignature": False},
    ),
)
