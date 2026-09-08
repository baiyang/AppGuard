# AppGuard

AppGuard 0.2 is an offline licensing and encrypted-function delivery tool for
CPython 3.11. Publisher tooling lives in this repository; the customer receives
only a native loader, a Flask plugin, encrypted modules/functions and public assets.
It is an initial implementation, not a PyArmor-equivalent anti-reversing system.

## Components

- `appguard/`: publisher CLI, Ed25519 signing, AST function extraction and AES-256-GCM packaging.
- `runtime/guard_runtime.pyx`: Cython-compiled `guard_runtime.so`, with a compiled-in
  publisher public key. It verifies signed metadata, unwraps deployment-specific
  content keys and evaluates decrypted code objects in memory.
- `appguard_flask.py`: explicit `AppGuard().init_app(app)` registration. The business
  creates and runs its own Flask application with its existing startup command.
- `appguard_host.py`: WSGI activation middleware, CSRF protection, activation
  limits and enrollment/status CLI. It no longer creates or imports the business app.
- `tests/`: publisher tests and native runtime tests against the deployed image.
- `tools/`: release hygiene utilities. Publisher signing tools are not in the runtime wheel.

## Trust and Cryptography

Each release has separate random 256-bit bootstrap and licensed-function keys,
and a unique build ID. AAD binds ciphertext to its build ID, kind and identity.
A signed manifest contains module/function digests and the bootstrap key digest. Never change
the nonce/key rules or replace the cryptography library with custom primitives.

The builder extracts functions listed in `[protected_functions]` into `.agf` files.
The module retains declarations, decorators, signatures and forwarding wrappers;
these startup structures are encrypted as `.agc` files with the bootstrap key.
The bootstrap key is compiled into the native runtime, allowing ordinary imports,
Flask construction and explicit database initialization even without a license.
It is not a licensing secret: an administrator can recover startup/helper code.
The original bodies of selected functions are absent from the bootstrap payload.
The licensed-function key is available only through a valid deployment license.

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

`build` refuses to overwrite a release, omit configured functions, or ship a
bootstrap-only application with no protected functions. `release-001/bundle` is
an image build input, not the customer deliverable. Its `bootstrap.key` is used
only during native compilation and must not be copied into the final filesystem.
`release.json` contains the licensed-function key and must never enter an image.
The signing key is a raw private key
in a mode-0600 file; use restricted publisher storage or extend signing with your
KMS before operating a shared signing service.

### File Selection

`include` is the single selection list for the delivery tree. It accepts source-relative
files, directories and glob patterns. A selected directory is visited recursively,
including when selected through a glob such as `backend/*`. Overlapping selections
are deduplicated. Selected `.py` files use the existing code protection pipeline;
all other files are copied unchanged at their original relative paths, retaining
file permissions. Symbolic links are skipped and directories matched by `exclude`
are not traversed. Empty directories are not included.

```toml
product_id = "example"
include = ["backend/"]
exclude = [
    "backend/.venv*",
    "backend/tests/",
    "**/__pycache__/",
    "**/.env*",
    "**/*.pyc",
]

[protected_functions]
"backend/service.py" = ["answer"]
```

Exclusions apply to every file type. A directory can be excluded as `backend/tests`,
`backend/tests/` or `backend/tests/**`; its descendants stay excluded even if another
include pattern names them directly. Patterns match source-relative POSIX paths
with case-sensitive `fnmatch` semantics. Add product-specific secret, dependency
lock, build-only and cache files to `exclude` before including an entire source tree.
Unknown top-level configuration fields are rejected. No build report is generated.

`issue` also performs renewal: issue a new file for the same deployment and build
with a later expiry. Upgrading to a new build requires a new license for that build.
Keep release records and the issuer key backed up. `inspect` displays unverified
metadata only and must never be used as an authorization decision.

## Runtime and Renewal

The image compiles the runtime using `APPGUARD_PUBLIC_KEY` and
`APPGUARD_BOOTSTRAP_KEY` during wheel generation. These are not runtime overrides;
the licensed-function key is never compiled into the runtime.

Register the plugin after creating/configuring the application:

```python
from appguard_flask import AppGuard

AppGuard().init_app(app)
```

Registration is idempotent and wraps the existing WSGI middleware, preserving
ProxyFix and OpenTelemetry. No background HTTP server or startup interception is
installed. Continue using `python backend/web_app.py`, `gunicorn web_app:app`, or
the product's existing command. Automatic `.pth` instrumentation is not included.

Runtime configuration:

| Variable | Default | Purpose |
|---|---|---|
| `APPGUARD_BUNDLE` | `/opt/appguard/bundle` | Signed manifest and ciphertexts |
| `APPGUARD_LICENSE_DIR` | `/var/lib/appguard` | Persistent identity, license and clock state |
| `APPGUARD_SECURE_COOKIE` | `0` | Set `1` behind HTTPS for the activation cookie |

The plugin serves the Chinese activation page at `/_license/` before invoking
Flask request hooks, including after an expired-license restart.
Download the activation request, have the publisher issue a license, and upload
the file or paste its JSON/base64 form. Invalid candidates do not replace a valid
license. Valid candidates are authenticated, tested against a protected function and atomically
installed. Each process observes file changes and expiry at subsequent checks.

The plugin does not run business schema migrations. Operators must explicitly run
the product's initialization command; it can now run before activation. Application
configuration, dependencies and pre-start commands must still work: a database
initialization failure before the HTTP server starts also prevents the portal.
The license directory must be writable by the runtime UID, since it persists a
best-effort clock high-water mark as well as activation state.

## Protection Scope and Limitations

- Native execution decrypts selected function bodies only after licensing checks.
  Configured `[checkpoints]` add checks without deferring a function's body, for
  example a request hook that also blocks access if the plugin is removed. It
  does not discover all business paths automatically. Each newly added worker or
  alternate entrypoint needs a coverage review. Modules/functions needed for startup
  must remain in the bootstrap group. Protecting a function called at import time
  can still prevent unlicensed startup. Source development only adds plugin registration.
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
- Already-running work is checked at function entry/return, generator resumptions
  and before forwarding each response chunk. Coroutines use normal task cancellation
  and finish asynchronous cleanup before their result is checked. Expiry closes the iterable when control returns, invoking
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
- Protected closures (including zero-argument `super()` methods) and async generators
  are rejected explicitly. Sync functions, ordinary methods, generators and coroutines
  are supported. The bootstrap uses reserved `__appguard_*` globals for dispatch.
- The v0.2 function-body layout requires a newly built image and license. Existing
  v0.1 images continue to use their own runtime and license; they are not upgraded in place.

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
