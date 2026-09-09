"""Build, audit and exercise an isolated Flask delivery with ephemeral test keys."""

import argparse
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from tools.verify_http import FormParser


ROOT = Path(__file__).resolve().parents[1]


def run(*command, timeout=60, capture=False):
    return subprocess.run(
        [str(part) for part in command], cwd=ROOT, check=True,
        text=True, capture_output=capture, timeout=timeout,
    )


def wait_ready(session, url, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            response = session.get(url + "/_license/status", timeout=5)
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def activate(session, url, license_path):
    response = session.get(url + "/_license/", timeout=10)
    response.raise_for_status()
    form = FormParser()
    form.feed(response.text)
    if not form.token:
        raise AssertionError("Activation page did not supply a CSRF token")
    response = session.post(
        url + "/_license/activate",
        data={"csrf": form.token, "license": license_path.read_text()},
        allow_redirects=False, timeout=10,
    )
    if response.status_code not in (200, 303):
        raise AssertionError(f"Activation failed: HTTP {response.status_code}")


def assert_answer(session, url):
    response = session.get(url + "/api/answer", params={"value": 8}, timeout=10)
    response.raise_for_status()
    if response.json() != {"answer": 50}:
        raise AssertionError(f"Unexpected protected response: {response.text}")


def verify(args, result, container):
    run("docker", "info", "--format", "{{.ServerVersion}}", timeout=20)
    run("docker", "buildx", "version", timeout=20)
    with tempfile.TemporaryDirectory(prefix="appguard-delivery-") as directory:
        temp = Path(directory)
        issuer, public, code = temp / "issuer.key", temp / "issuer.pub", temp / "code.key"
        release, license_path = temp / "release", temp / "customer.license"
        example = ROOT / "examples" / "flask"
        config = example / "guard.toml"
        product = tomllib.loads(config.read_text())["product_id"]
        cli = (sys.executable, "-m", "appguard")
        run(*cli, "keygen", "--out", issuer)
        run(*cli, "code-keygen", "--out", code)
        run(*cli, "build", "--source", example, "--config", config,
            "--issuer-key", issuer, "--code-key", code, "--out", release)
        expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        run(*cli, "issue", "--product", product, "--issuer-key", issuer,
            "--customer", "CI integration test", "--expires", expires,
            "--out", license_path)
        run(
            "docker", "buildx", "build", "--load", "--platform", args.platform,
            "--file", example / "Dockerfile", "--tag", args.image,
            "--build-context", f"application={example}",
            "--build-context", f"release={release / 'bundle'}",
            "--secret", f"id=publisher_public,src={public}",
            "--secret", f"id=code_key,src={code}",
            "--build-arg", "APPGUARD_PUBLIC_KEY_SHA256=" + hashlib.sha256(public.read_bytes()).hexdigest(),
            "--build-arg", "APPGUARD_CODE_KEY_SHA256=" + hashlib.sha256(code.read_bytes()).hexdigest(),
            ROOT, timeout=args.build_timeout,
        )
        audit = temp / "image-audit.json"
        run(sys.executable, "-m", "tools.audit_image", "--image", args.image,
            "--out", audit, timeout=300)
        result["image_audit"] = json.loads(audit.read_text())
        # An implicit Docker host port can change on restart; choose it once here.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        run("docker", "run", "--detach", "--name", container,
            "--platform", args.platform, "--publish", f"127.0.0.1:{port}:8000",
            args.image)
        inspection = json.loads(run("docker", "inspect", container, capture=True).stdout)[0]
        binding = inspection["NetworkSettings"]["Ports"]["8000/tcp"][0]
        if binding["HostIp"] != "127.0.0.1":
            raise AssertionError("Test container port must be bound to loopback")
        url = "http://127.0.0.1:" + binding["HostPort"]
        with requests.Session() as session:
            session.trust_env = False
            status = wait_ready(session, url, args.startup_timeout)
            if status["valid"] or status["code"] != "LICENSE_MISSING":
                raise AssertionError(f"Expected a fresh unlicensed container: {status}")
            response = session.get(url + "/api/answer?value=8", timeout=10)
            if response.status_code != 403 or response.json()["code"] != "LICENSE_MISSING":
                raise AssertionError("Missing license did not block the business endpoint")
            result["missing_license_blocks_business"] = True
            activate(session, url, license_path)
            assert_answer(session, url)
            result["valid_license_enables_protected_modules"] = True
            http_report = temp / "http-verification.json"
            run(sys.executable, "-m", "tools.verify_http", "--url", url,
                "--issuer-key", issuer, "--product", product,
                "--restore-license", license_path, "--restart-container", container,
                "--startup-timeout", args.startup_timeout, "--out", http_report,
                timeout=3 * args.startup_timeout + 120)
            result["http_verification"] = json.loads(http_report.read_text())
            if not all(result["http_verification"].values()):
                raise AssertionError("HTTP expiry and renewal verification failed")
            assert_answer(session, url)
            result["renewal_restores_business_without_restart"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="appguard-ci:local")
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--out", type=Path, default=Path(".data/delivery-verification.json"))
    parser.add_argument("--build-timeout", type=float, default=900)
    parser.add_argument("--startup-timeout", type=float, default=60)
    args = parser.parse_args()
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 11):
        parser.error("The Flask delivery requires CPython 3.11")
    if args.build_timeout <= 0 or args.startup_timeout <= 0:
        parser.error("Timeouts must be positive")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    container = "appguard-ci-" + uuid.uuid4().hex
    result = {"image": args.image, "platform": args.platform, "passed": False}
    try:
        verify(args, result, container)
        result["passed"] = True
    except (OSError, ValueError, KeyError, AssertionError, subprocess.SubprocessError,
            requests.RequestException) as exc:
        result["error"] = str(exc)
        try:
            logs = run("docker", "logs", "--tail", "100", container, capture=True, timeout=20)
            print(logs.stdout + logs.stderr, file=sys.stderr)
        except (OSError, subprocess.SubprocessError):
            pass
    finally:
        try:
            cleanup = subprocess.run(["docker", "rm", "--force", "--volumes", container],
                                     check=False, capture_output=True, text=True, timeout=30)
            if cleanup.returncode and result["passed"]:
                result["cleanup_error"] = cleanup.stderr.strip()
                result["passed"] = False
        except (OSError, subprocess.SubprocessError) as exc:
            result["cleanup_error"] = str(exc)
            result["passed"] = False
        args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return int(not result["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
