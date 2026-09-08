"""Compile the customer runtime with a publisher trust anchor."""

import os

from Cython.Build import cythonize
from setuptools import Extension, setup

public_key = os.environ.get("APPGUARD_PUBLIC_KEY", "")
if len(public_key) != 64 or any(c not in "0123456789abcdef" for c in public_key):
    raise RuntimeError("APPGUARD_PUBLIC_KEY must be the publisher's 32-byte hex public key")
bootstrap_key = os.environ.get("APPGUARD_BOOTSTRAP_KEY", "")
if len(bootstrap_key) != 64 or any(c not in "0123456789abcdef" for c in bootstrap_key):
    raise RuntimeError("APPGUARD_BOOTSTRAP_KEY must be the release's 32-byte bootstrap key")

setup(
    py_modules=["appguard_host", "appguard_flask"],
    ext_modules=cythonize(
        [Extension("guard_runtime", ["runtime/guard_runtime.pyx"],
                   define_macros=[("APPGUARD_PUBLIC_KEY", '"' + public_key + '"'),
                                  ("APPGUARD_BOOTSTRAP_KEY", '"' + bootstrap_key + '"')])],
        compiler_directives={"language_level": 3, "embedsignature": False},
    ),
)
