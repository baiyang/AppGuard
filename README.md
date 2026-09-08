# AppGuard

AppGuard 0.1 is an offline licensing and encrypted-module delivery tool for
CPython 3.11. Publisher tooling lives in this repository; the customer receives
only a native loader, a WSGI activation host, encrypted modules and public assets.
It is an initial implementation, not a PyArmor-equivalent anti-reversing system.

## Components

- `appguard/`: publisher CLI, Ed25519 signing, AST checkpoints and AES-256-GCM packaging.
- `runtime/guard_runtime.pyx`: Cython-compiled `guard_runtime.so`, with a compiled-in
  publisher public key. It verifies signed metadata, unwraps deployment-specific
  content keys and evaluates decrypted code objects in memory.
- `appguard_host.py`: same-process WSGI activation page and request gate, with
  lazy business application initialization, CSRF protection and activation limits.
- `tests/`: publisher tests and native runtime tests against the deployed image.
- `tools/`: release hygiene utilities. Publisher signing tools are not in the runtime wheel.

## Trust and Cryptography

Each release has a random 256-bit content key and a unique build ID. Module AAD
binds ciphertext to its build ID and original relative path. A signed manifest
contains encrypted-module digests and the application entrypoint. Never change
the nonce/key rules or replace the cryptography library with custom primitives.

The customer deployment generates an X25519 identity. The issuer wraps the content
key using ephemeral X25519, HKDF-SHA256 (`appguard-wrap-v1`) and AES-256-GCM. An
Ed25519 signature covers all license fields, including this key envelope. A license
is bound to one product, build and deployment public key. Publisher keys and release
content keys must remain in the publisher's private storage and backups.

The runtime authenticates the envelope before reading its policy, rejects expired
or future licenses, and authenticates modules before unmarshalling. Native global
state retains content keys; the Python API does not return decrypted code or keys.
This does not prevent extraction by a hostile Python interpreter or host administrator.

## Publisher Workflow

Use Python 3.11 with `cryptography==45.0.4`. No separate GraphRAG environment is
needed; the medical-baike backend Python can run the publisher commands.

```sh
python -m appguard keygen --out .data/issuer.key
python -m appguard build --source /path/to/project --config /path/to/guard.toml \
  --issuer-key .data/issuer.key --out .data/releases/release-001
python -m appguard issue --release .data/releases/release-001/release.json \
  --issuer-key .data/issuer.key --request activation-request.json \
  --customer customer-001 --expires 2027-03-08T00:00:00Z --out customer-001.license
```

`build` refuses to overwrite a release or silently skip a configured checkpoint.
Only `release-001/bundle` is a customer build input. `release.json` contains the
content key and must never enter an image. The signing key is a raw private key
in a mode-0600 file; use restricted publisher storage or extend signing with your
KMS before operating a shared signing service.

`issue` also performs renewal: issue a new file for the same deployment and build
with a later expiry. Upgrading to a new build requires a new license for that build.
Keep release records and the issuer key backed up. `inspect` displays unverified
metadata only and must never be used as an authorization decision.

## Runtime and Renewal

The image compiles the runtime using `APPGUARD_PUBLIC_KEY` during wheel generation.
This is a public trust anchor, not a secret or a runtime environment override.

Runtime configuration:

| Variable | Default | Purpose |
|---|---|---|
| `APPGUARD_BUNDLE` | `/opt/appguard/bundle` | Signed manifest and ciphertexts |
| `APPGUARD_LICENSE_DIR` | `/var/lib/appguard` | Persistent identity, license and clock state |
| `APPGUARD_APP_ROOT` | `/app` | Runtime tree; v1 compiles diagnostic filenames for `/app` |
| `APPGUARD_SECURE_COOKIE` | `0` | Set `1` behind HTTPS for the activation cookie |

Serve `appguard_host:create_host()` with a WSGI server. The activation page always
remains available at `/_license/`, including after an expired-license restart.
Download the activation request, have the publisher issue a license, and upload
the file or paste its JSON/base64 form. Invalid candidates do not replace a valid
license. Valid candidates are authenticated, tested against a module and atomically
installed. Each process observes file changes and expiry at subsequent checks.

The host does not run business schema migrations. Operators must explicitly run
the product's initialization command after activation and before business use.
The license directory must be writable by the runtime UID, since it persists a
best-effort clock high-water mark as well as activation state.

## Protection Scope and Limitations

- The build injects native checks at explicitly configured function entries. It
  does not discover all business paths automatically. Each newly added worker or
  alternate entrypoint needs a coverage review. Ordinary source development is unchanged.
- Business modules are encrypted, but their stubs and public templates remain
  readable. Dependencies, indexes, prompts and already returned data are not protected
  automatically. Reimplementing functionality using delivered data is outside scope.
- Code objects and keys exist in process memory. Root, debuggers, monkeypatching
  lower-level dependencies, a modified interpreter or native binary patching can
  bypass or extract them. There is no bytecode VM, hardware attestation or anti-debugger.
- Offline clock rollback detection has a 120-second tolerance and an advisory
  persisted maximum time. Volume deletion, editing or whole-machine snapshot
  restoration can bypass it. An extracted content key has no cryptographic expiry.
- Copying both the deployment private key and its license clones the installation.
  No strong hardware binding, offline revocation or global concurrency enforcement exists.
- Already-running work is checked at configured entries and before forwarding
  each response chunk. Expiry closes the iterable when control returns, invoking
  existing cleanup. It does not forcibly interrupt blocked network I/O or arbitrary
  Python threads at an exact deadline. Idle browser tabs do not navigate themselves.
- Renewal does not guarantee monotonic license generations: an older, still-valid
  signed license can be reinstalled. Build-scoped licenses prevent cross-build use,
  not rollback to an older image and matching license.
- This initial runtime targets CPython 3.11. Native wheels must match OS and CPU;
  linux/amd64 is the verified target. ARM64 requires its own build and verification.
- Public Python signatures and annotations are preserved for Flask/Pydantic.
  Source inspection sees stubs, not original source; source-based tests and debugging
  tools need adaptation. The software is not a drop-in protection tool for other languages.

## Verification

```sh
PYTHONPATH=. python -m pytest tests/test_build.py -q
```

The medical-baike integration includes `backend/scripts/smoke_private_image.py`.
It mounts publisher inputs only into a disposable test container, installs pytest
there, tests license rejection/activation/native loading and runs existing business
tests against the encrypted modules. These test mounts are never customer deployment
instructions. External LLM/OCR calls are mocked; SQLite and generated KB fixtures
provide isolated data. A real customer-model end-to-end test is a separate gate.

`tools/verify_http.py` tests the real HTTP license boundary, temporarily installs
an eight-second license, optionally restarts a named test container, and restores
the supplied valid license. Run it only against an isolated test deployment.
`tools/audit_image.py --image <tag> --out <report.json>` checks all exported image
layers for plaintext business modules, publisher material and Git URL credentials;
its temporary image archive is removed after inspection.
