# cython: language_level=3
"""Authenticated module loading and independent offline product licensing.

The public trust anchor and product code key are compiled in. This prevents
plain source delivery, not extraction by a hostile host or interpreter.
"""

import base64
import hashlib
import json
import marshal
import os
import sys
import tempfile
from pathlib import Path
from types import CodeType

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from libc.time cimport time as c_time

cdef extern from *:
    const char *APPGUARD_PUBLIC_KEY
    const char *APPGUARD_CODE_KEY

cdef extern from "Python.h":
    object PyEval_EvalCode(object code, object globals, object locals)

cdef object _manifest = None
cdef object _bundle = None
cdef object _cached_license = None
cdef object _cached_stamp = None
cdef object _clock_dir = None
cdef long long _max_seen = 0
cdef long long _saved_seen = 0


class LicenseError(RuntimeError):
    """A stable machine-readable license or bundle failure code."""


cdef bytes _decode(object value):
    if not isinstance(value, str):
        raise ValueError("Invalid encoded field")
    return base64.b64decode(value, validate=True)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


cdef object _verify(bytes raw, str kind):
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Envelope too large")
    envelope = json.loads(raw, object_pairs_hook=_unique_pairs)
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
        raise ValueError("Invalid envelope")
    payload = _decode(envelope["payload"])
    public = Ed25519PublicKey.from_public_bytes(bytes.fromhex((<bytes>APPGUARD_PUBLIC_KEY).decode()))
    public.verify(_decode(envelope["signature"]), payload)
    data = json.loads(payload, object_pairs_hook=_unique_pairs)
    if (not isinstance(data, dict) or type(data.get("format")) is not int
            or data["format"] != 2 or data.get("kind") != kind):
        raise ValueError("Unsupported signed document")
    return data


cdef object _license_dir():
    return Path(os.environ.get("APPGUARD_LICENSE_DIR", "/var/lib/appguard"))


cdef bint _valid_text(object value, int limit):
    return (isinstance(value, str) and bool(value) and value == value.strip() and len(value) <= limit
            and not any(ord(character) < 32 or ord(character) == 127 for character in value))


cdef object _load_manifest():
    global _manifest, _bundle
    if _manifest is None:
        _bundle = Path(os.environ.get("APPGUARD_BUNDLE", "/opt/appguard/bundle"))
        try:
            candidate = _verify((_bundle / "manifest.json").read_bytes(), "manifest")
            if candidate["python"] != f"{sys.version_info.major}.{sys.version_info.minor}":
                raise ValueError("Wrong Python ABI")
            if not _valid_text(candidate["product_id"], 128) or not _valid_text(candidate["build_id"], 128):
                raise ValueError("Invalid product or build identifier")
            if not isinstance(candidate["modules"], dict) or not candidate["modules"]:
                raise ValueError("Invalid module table")
            if candidate["layout"] != "modules-v1":
                raise ValueError("Unsupported protection layout")
            key = bytes.fromhex((<bytes>APPGUARD_CODE_KEY).decode())
            if hashlib.sha256(key).hexdigest() != candidate["code_key_sha256"]:
                raise ValueError("Runtime does not match the product code key")
            _manifest = candidate
        except Exception as exc:
            raise LicenseError("BUNDLE_INVALID") from exc
    return _manifest


cdef void _atomic_write(object path, bytes data) except *:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".appguard-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


cdef long long _now() except -1:
    global _max_seen, _saved_seen, _clock_dir
    cdef long long current = <long long>c_time(NULL)
    directory = _license_dir()
    if _clock_dir != directory:
        _clock_dir = directory
        _max_seen = _saved_seen = 0
    path = directory / "last-seen"
    if not _max_seen:
        try:
            observed = int(path.read_text())
            if not 0 <= observed <= 253402300799:
                raise ValueError("Invalid clock state")
            _max_seen = observed
        except FileNotFoundError:
            _max_seen = current
        except Exception as exc:
            raise LicenseError("CLOCK_STATE_INVALID") from exc
    if current + 120 < _max_seen:
        raise LicenseError("CLOCK_ROLLBACK")
    _max_seen = max(_max_seen, current)
    if _max_seen > _saved_seen + 30:
        # Advisory rollback detection; an administrator can restore the whole volume.
        try:
            _atomic_write(path, str(_max_seen).encode())
        except OSError as exc:
            raise LicenseError("LICENSE_STATE_UNWRITABLE") from exc
        _saved_seen = _max_seen
    return _max_seen


cdef void _check_time(object license) except *:
    current = _now()
    if current < license["not_before"]:
        raise LicenseError("LICENSE_NOT_YET_VALID")
    if current >= license["expires_at"]:
        raise LicenseError("LICENSE_EXPIRED")


cdef object _validate(bytes raw):
    manifest = _load_manifest()
    try:
        if len(raw) > 65536:
            raise ValueError("License too large")
        license = _verify(raw, "license")
        if set(license) != {"format", "kind", "license_id", "product_id", "customer",
                           "issued_at", "not_before", "expires_at"}:
            raise ValueError("Invalid license fields")
        for field, limit in (("product_id", 128), ("license_id", 128), ("customer", 512)):
            if not _valid_text(license[field], limit):
                raise ValueError("Invalid license identifier")
        for field in ("not_before", "expires_at", "issued_at"):
            if type(license[field]) is not int or not 0 <= license[field] <= 253402300799:
                raise ValueError("Invalid timestamp")
        if license["not_before"] >= license["expires_at"]:
            raise ValueError("Invalid validity interval")
        if license["product_id"] != manifest["product_id"]:
            raise LicenseError("LICENSE_WRONG_PRODUCT")
        _check_time(license)
        return license
    except LicenseError:
        raise
    except Exception as exc:
        raise LicenseError("LICENSE_INVALID") from exc


cdef object _require():
    global _cached_license, _cached_stamp
    path = _license_dir() / "license.json"
    try:
        stat = path.stat()
        if stat.st_size > 65536:
            raise LicenseError("LICENSE_INVALID")
        stamp = (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        if stamp != _cached_stamp:
            candidate = _validate(path.read_bytes())
            _cached_license, _cached_stamp = candidate, stamp
        _check_time(_cached_license)
        return _cached_license
    except FileNotFoundError as exc:
        _cached_license = _cached_stamp = None
        raise LicenseError("LICENSE_MISSING") from exc
    except OSError as exc:
        raise LicenseError("LICENSE_INVALID") from exc


def require_valid():
    """Check the product license at the business API boundary."""
    _require()


def execute_module(str relative, dict namespace):
    """Authenticate and load a complete module independently of licensing."""
    manifest = _load_manifest()
    try:
        entry = manifest["modules"][relative]
        filename = entry["file"]
        if Path(filename).name != filename or not filename.endswith(".agc"):
            raise ValueError("Invalid module path")
        blob = (_bundle / "modules" / filename).read_bytes()
        if hashlib.sha256(blob).hexdigest() != entry["sha256"]:
            raise ValueError("Module digest mismatch")
        key = bytes.fromhex((<bytes>APPGUARD_CODE_KEY).decode())
        aad = f"{manifest['product_id']}:{manifest['build_id']}:module:{relative}".encode()
        code = marshal.loads(AESGCM(key).decrypt(blob[:12], blob[12:], aad))
        if not isinstance(code, CodeType):
            raise ValueError("Invalid module code")
    except Exception as exc:
        raise LicenseError("MODULE_INVALID") from exc
    PyEval_EvalCode(code, namespace, namespace)


def product_info():
    manifest = _load_manifest()
    return {key: manifest[key] for key in ("product_id", "build_id", "python")}


def install_license(bytes raw):
    global _cached_stamp
    _validate(raw)
    try:
        _atomic_write(_license_dir() / "license.json", raw)
    except OSError as exc:
        raise LicenseError("LICENSE_STATE_UNWRITABLE") from exc
    _cached_stamp = None
    return status()


def status():
    try:
        product_id = _load_manifest()["product_id"]
        license = _require()
        return {"valid": True, "code": "LICENSE_VALID", "product_id": product_id,
                "customer": license["customer"], "expires_at": license["expires_at"],
                "license_id": license["license_id"]}
    except LicenseError as exc:
        return {"valid": False, "code": str(exc),
                "product_id": _manifest["product_id"] if _manifest is not None else None}
