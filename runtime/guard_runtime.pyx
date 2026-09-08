# cython: language_level=3
"""Native license gate and authenticated encrypted module loader.

The issuer trust anchor is compiled in. Decrypted bytecode and content keys
remain process-local; this is not protection against a hostile interpreter.
"""

import base64
import hashlib
import json
import marshal
import os
import sys
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from libc.time cimport time as c_time

cdef extern from *:
    const char *APPGUARD_PUBLIC_KEY

cdef extern from "Python.h":
    object PyEval_EvalCode(object code, object globals, object locals)

cdef object _manifest = None
cdef object _bundle = None
cdef object _cached_license = None
cdef object _cached_stamp = None
cdef object _content_key = None
cdef object _checkpoint = None
cdef long long _max_seen = 0
cdef long long _saved_seen = 0


class LicenseError(RuntimeError):
    """A stable machine-readable license failure code."""


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


cdef object _verify(bytes raw):
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Envelope too large")
    envelope = json.loads(raw, object_pairs_hook=_unique_pairs)
    if set(envelope) != {"payload", "signature"}:
        raise ValueError("Invalid envelope")
    payload = _decode(envelope["payload"])
    public = Ed25519PublicKey.from_public_bytes(bytes.fromhex((<bytes>APPGUARD_PUBLIC_KEY).decode()))
    public.verify(_decode(envelope["signature"]), payload)
    data = json.loads(payload, object_pairs_hook=_unique_pairs)
    if not isinstance(data, dict) or data.get("format") != 1:
        raise ValueError("Unsupported format")
    return data


cdef object _license_dir():
    return Path(os.environ.get("APPGUARD_LICENSE_DIR", "/var/lib/appguard"))


cdef object _load_manifest():
    global _manifest, _bundle
    if _manifest is None:
        _bundle = Path(os.environ.get("APPGUARD_BUNDLE", "/opt/appguard/bundle"))
        try:
            candidate = _verify((_bundle / "manifest.json").read_bytes())
            if candidate["python"] != f"{sys.version_info.major}.{sys.version_info.minor}":
                raise ValueError("Wrong Python ABI")
            if not isinstance(candidate["modules"], dict):
                raise ValueError("Invalid module table")
            _manifest = candidate
        except Exception as exc:
            raise LicenseError("BUNDLE_INVALID") from exc
    return _manifest


cdef object _device():
    path = _license_dir() / "deployment.key"
    try:
        return X25519PrivateKey.from_private_bytes(path.read_bytes())
    except Exception as exc:
        raise LicenseError("DEPLOYMENT_KEY_MISSING") from exc


cdef long long _now() except -1:
    global _max_seen, _saved_seen
    cdef long long current = <long long>c_time(NULL)
    path = _license_dir() / "last-seen"
    if not _max_seen:
        try:
            _max_seen = int(path.read_text())
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


cdef tuple _validate(bytes raw):
    manifest = _load_manifest()
    try:
        license = _verify(raw)
        for field in ("not_before", "expires_at", "issued_at"):
            if type(license[field]) is not int:
                raise ValueError("Invalid timestamp")
        if license["not_before"] >= license["expires_at"]:
            raise ValueError("Invalid validity interval")
        if license["product_id"] != manifest["product_id"] or license["build_id"] != manifest["build_id"]:
            raise LicenseError("LICENSE_WRONG_PRODUCT_OR_BUILD")
        device = _device()
        if device.public_key().public_bytes_raw().hex() != license["deployment_public"]:
            raise LicenseError("LICENSE_WRONG_DEPLOYMENT")
        current = _now()
        if current < license["not_before"]:
            raise LicenseError("LICENSE_NOT_YET_VALID")
        if current >= license["expires_at"]:
            raise LicenseError("LICENSE_EXPIRED")
        wrapped = license["wrapped_key"]
        public = X25519PublicKey.from_public_bytes(bytes.fromhex(wrapped["ephemeral_public"]))
        wrapping_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                            info=b"appguard-wrap-v1").derive(device.exchange(public))
        content_key = AESGCM(wrapping_key).decrypt(
            _decode(wrapped["nonce"]), _decode(wrapped["ciphertext"]), manifest["build_id"].encode())
        if len(content_key) != 32:
            raise ValueError("Invalid content key")
        return license, content_key
    except LicenseError:
        raise
    except Exception as exc:
        raise LicenseError("LICENSE_INVALID") from exc


cdef object _require():
    global _cached_license, _cached_stamp, _content_key
    path = _license_dir() / "license.json"
    try:
        stat = path.stat()
        if stat.st_size > 65536:
            raise LicenseError("LICENSE_INVALID")
        stamp = (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if stamp != _cached_stamp:
            license, key = _validate(path.read_bytes())
            _cached_license, _content_key, _cached_stamp = license, key, stamp
        if _now() >= _cached_license["expires_at"]:
            raise LicenseError("LICENSE_EXPIRED")
        return _cached_license
    except FileNotFoundError as exc:
        _cached_license = _cached_stamp = _content_key = None
        raise LicenseError("LICENSE_MISSING") from exc


def require_valid():
    """Native checkpoint called by transformed business functions."""
    _require()


_checkpoint = require_valid


def execute_module(str relative, dict namespace):
    """Authenticate and execute a module without writing decrypted code to disk."""
    _require()
    try:
        entry = _load_manifest()["modules"][relative]
        filename = entry["file"]
        if Path(filename).name != filename or not filename.endswith(".agc"):
            raise ValueError("Invalid module path")
        blob = (_bundle / "modules" / filename).read_bytes()
        if hashlib.sha256(blob).hexdigest() != entry["sha256"]:
            raise ValueError("Module digest mismatch")
        aad = f"{_manifest['build_id']}:{relative}".encode()
        data = AESGCM(_content_key).decrypt(blob[:12], blob[12:], aad)
        code = marshal.loads(data)
    except Exception as exc:
        raise LicenseError("MODULE_INVALID") from exc
    namespace["__appguard_check__"] = _checkpoint
    PyEval_EvalCode(code, namespace, namespace)


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


def enrollment():
    """Create a stable deployment identity; never return its private key."""
    manifest = _load_manifest()
    path = _license_dir() / "deployment.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".deployment-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(X25519PrivateKey.generate().private_bytes_raw())
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    return {"product_id": manifest["product_id"], "build_id": manifest["build_id"],
            "deployment_public": _device().public_key().public_bytes_raw().hex()}


def install_license(bytes raw):
    global _cached_stamp
    if len(raw) > 65536:
        raise LicenseError("LICENSE_INVALID")
    license, key = _validate(raw)
    # Verify the release key before replacing a working license.
    manifest = _load_manifest()
    relative = next(iter(manifest["modules"]))
    entry = manifest["modules"][relative]
    blob = (_bundle / "modules" / entry["file"]).read_bytes()
    try:
        AESGCM(key).decrypt(blob[:12], blob[12:], f"{manifest['build_id']}:{relative}".encode())
    except Exception as exc:
        raise LicenseError("LICENSE_WRONG_CONTENT_KEY") from exc
    _atomic_write(_license_dir() / "license.json", raw)
    _cached_stamp = None
    return status()


def status():
    try:
        license = _require()
        return {"valid": True, "code": "LICENSE_VALID", "customer": license["customer"],
                "expires_at": license["expires_at"], "license_id": license["license_id"]}
    except LicenseError as exc:
        return {"valid": False, "code": str(exc)}


def application_spec():
    """Only signed bundle metadata controls the default business entrypoint."""
    manifest = _load_manifest()
    return {"application": manifest["application"], "python_path": manifest["python_path"]}
