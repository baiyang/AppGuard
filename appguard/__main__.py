"""Publisher CLI: python -m appguard {keygen,code-keygen,build,issue,inspect}."""

import argparse
import base64
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .build import build
from .crypto import private_write, product_id, signed, signer


def timestamp(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("missing timezone")
        result = int(parsed.timestamp())
        if not 0 <= result <= 253402300799:
            raise ValueError("outside supported timestamp range")
        return result
    except (ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError("Use a date after 1970 with an explicit timezone, e.g. 2027-03-08T00:00:00Z") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    keys = sub.add_parser("keygen", help="Generate an Ed25519 issuer key pair")
    keys.add_argument("--out", type=Path, required=True)
    code_keys = sub.add_parser("code-keygen", help="Generate a product AES-256 code key")
    code_keys.add_argument("--out", type=Path, required=True)
    package = sub.add_parser("build", help="Encrypt Python modules using a product code key")
    for name in ("source", "config", "issuer-key", "code-key", "out"):
        package.add_argument("--" + name, type=Path, required=True)
    issue = sub.add_parser("issue", help="Issue a license valid across builds of one product")
    for name in ("issuer-key", "out"):
        issue.add_argument("--" + name, type=Path, required=True)
    issue.add_argument("--product", required=True)
    issue.add_argument("--customer", required=True)
    issue.add_argument("--expires", required=True, type=timestamp)
    issue.add_argument("--not-before", type=timestamp)
    inspect = sub.add_parser("inspect", help="Display metadata without verifying its signature")
    inspect.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "keygen":
            public_path = args.out.with_suffix(".pub")
            if public_path == args.out:
                raise ValueError("Private key output must not use the .pub extension")
            if any(path.exists() or path.is_symlink() for path in (args.out, public_path)):
                raise ValueError("Key output already exists")
            key = Ed25519PrivateKey.generate()
            private_write(args.out, key.private_bytes_raw().hex().encode() + b"\n")
            try:
                private_write(public_path, key.public_key().public_bytes_raw().hex().encode() + b"\n")
            except OSError:
                args.out.unlink()
                raise
            print(json.dumps({"private_key": str(args.out), "public_key": str(public_path)}))
        elif args.command == "code-keygen":
            private_write(args.out, os.urandom(32).hex().encode() + b"\n")
            print(json.dumps({"code_key": str(args.out)}))
        elif args.command == "build":
            print(json.dumps(build(args.source, args.config, args.issuer_key, args.out, args.code_key)))
        elif args.command == "issue":
            product = product_id(args.product)
            if (not args.customer or args.customer != args.customer.strip() or len(args.customer) > 512
                    or any(ord(character) < 32 or ord(character) == 127 for character in args.customer)):
                raise ValueError("customer must contain 1 to 512 characters without surrounding whitespace or control characters")
            key = signer(args.issuer_key)
            now = int(time.time())
            start = args.not_before if args.not_before is not None else now
            if start >= args.expires:
                raise ValueError("not-before must precede expires")
            if args.expires <= now:
                raise ValueError("expires must be in the future")
            payload = {
                "format": 2, "kind": "license", "license_id": uuid.uuid4().hex,
                "product_id": product, "customer": args.customer,
                "issued_at": now, "not_before": start, "expires_at": args.expires,
            }
            private_write(args.out, signed(payload, key))
            print(json.dumps({"license": str(args.out), "license_id": payload["license_id"],
                              "expires_at": datetime.fromtimestamp(args.expires, timezone.utc).isoformat()}))
        else:
            envelope = json.loads(args.path.read_text(encoding="utf-8"))
            if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), str):
                raise ValueError("Expected a signed envelope with a base64 payload")
            payload = json.loads(base64.b64decode(envelope["payload"], validate=True))
            if not isinstance(payload, dict):
                raise ValueError("Expected an object payload")
            payload.pop("modules", None)
            print(json.dumps({"verified": False, "metadata": payload}, indent=2))
    except (ValueError, OSError, KeyError, SyntaxError) as exc:
        parser.exit(1, f"AppGuard: {exc}\n")


if __name__ == "__main__":
    main()
