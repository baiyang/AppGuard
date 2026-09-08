"""Publisher CLI: python -m appguard {keygen,build,issue,inspect}."""

import argparse
import base64
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .build import build
from .crypto import private_write, signed, signer, wrap_key


def timestamp(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Use an explicit timezone, e.g. 2027-03-08T00:00:00Z")
    return int(parsed.timestamp())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    keys = sub.add_parser("keygen")
    keys.add_argument("--out", type=Path, required=True)
    package = sub.add_parser("build")
    for name in ("source", "config", "issuer-key", "out"):
        package.add_argument("--" + name, type=Path, required=True)
    issue = sub.add_parser("issue")
    for name in ("release", "issuer-key", "request", "out"):
        issue.add_argument("--" + name, type=Path, required=True)
    issue.add_argument("--customer", required=True)
    issue.add_argument("--expires", required=True, type=timestamp)
    issue.add_argument("--not-before", type=timestamp)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "keygen":
            key = Ed25519PrivateKey.generate()
            private_write(args.out, key.private_bytes_raw().hex().encode() + b"\n")
            public_path = args.out.with_suffix(".pub")
            private_write(public_path, key.public_key().public_bytes_raw().hex().encode() + b"\n")
            print(json.dumps({"private_key": str(args.out), "public_key": str(public_path)}))
        elif args.command == "build":
            print(json.dumps(build(args.source, args.config, args.issuer_key, args.out)))
        elif args.command == "issue":
            release = json.loads(args.release.read_text())
            request = json.loads(args.request.read_text())
            key = signer(args.issuer_key)
            if key.public_key().public_bytes_raw().hex() != release["publisher_public"]:
                raise ValueError("Publisher key does not match the release")
            if (request["product_id"], request["build_id"]) != (release["product_id"], release["build_id"]):
                raise ValueError("Activation request does not match this release")
            public = request["deployment_public"]
            if len(bytes.fromhex(public)) != 32:
                raise ValueError("Invalid deployment public key")
            now = int(time.time())
            start = args.not_before if args.not_before is not None else min(now, args.expires - 1)
            if start >= args.expires:
                raise ValueError("not-before must precede expires")
            payload = {
                "format": 1, "license_id": uuid.uuid4().hex,
                "product_id": release["product_id"], "build_id": release["build_id"],
                "customer": args.customer, "deployment_public": public,
                "issued_at": now, "not_before": start, "expires_at": args.expires,
                "wrapped_key": wrap_key(bytes.fromhex(release["content_key"]), public, release["build_id"]),
            }
            private_write(args.out, signed(payload, key))
            print(json.dumps({"license": str(args.out), "license_id": payload["license_id"],
                              "expires_at": datetime.fromtimestamp(args.expires, timezone.utc).isoformat()}))
        else:
            envelope = json.loads(args.path.read_text())
            payload = json.loads(base64.b64decode(envelope["payload"], validate=True))
            payload.pop("wrapped_key", None)
            payload.pop("modules", None)
            print(json.dumps({"verified": False, "metadata": payload}, indent=2))
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, f"AppGuard: {exc}\n")


if __name__ == "__main__":
    main()
