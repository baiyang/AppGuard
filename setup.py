"""Compile the customer runtime with a publisher trust anchor."""

import os

from Cython.Build import cythonize
from setuptools import Extension, setup

public_key = os.environ.get("APPGUARD_PUBLIC_KEY", "")
if len(public_key) != 64 or any(c not in "0123456789abcdef" for c in public_key):
    raise RuntimeError("APPGUARD_PUBLIC_KEY must be the publisher's 32-byte hex public key")

setup(
    py_modules=["appguard_host"],
    ext_modules=cythonize(
        [Extension("guard_runtime", ["runtime/guard_runtime.pyx"],
                   define_macros=[("APPGUARD_PUBLIC_KEY", '"' + public_key + '"')])],
        compiler_directives={"language_level": 3, "embedsignature": False},
    ),
)
