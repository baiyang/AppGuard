# Changelog

## [Unreleased]

- Update the Flask Docker example and all installation examples to the published `0.0.2` release.

## [0.0.2] - 2026-09-10

- Add FastAPI registration through `appguard_fastapi.AppGuard` or native ASGI middleware,
  with the shared offline activation portal and explicit health-check exemptions.
- Include the FastAPI adapter in private runtime wheels and provide the optional
  `fastapi` dependency extra; verify installed FastAPI licensing during package checks.
- Simplify the Flask Docker example with in-image encryption and runtime builds,
  project-relative source layout, and standalone build and delivery verification scripts.
- Document the delivery architecture, CLI commands, Base64 license activation,
  and the boundaries of code protection and offline licensing.

## [0.0.1] - 2026-09-09

First public PyPI release of AppGuard, supporting CPython 3.11.

- Install the publisher toolkit with `pip install appguard-runtime==0.0.1`.
- Encrypt complete Python modules and sign reusable offline product licenses.
- Build a private native runtime for each product with `appguard build-runtime`.
- Integrate licensing with Flask or WSGI, including offline activation and renewal.
- Validate tests, public distributions, and Docker delivery before tagged releases.
- Publish to official PyPI through GitHub Actions Trusted Publishing.

The earlier repository-only version `0.3.0` was not a public PyPI release. The first
public distribution starts at `0.0.1`; encrypted bundle and license formats remain
at format 2.
