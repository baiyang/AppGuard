"""Check a TEST deployment's HTTP expiry and renewal, restoring its supplied license."""

import argparse
import json
import subprocess
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path

import requests

from appguard.crypto import signed, signer


class FormParser(HTMLParser):
    token = ""

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "input" and fields.get("name") == "csrf":
            self.token = fields.get("value", "")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--issuer-key", type=Path, required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--restore-license", type=Path, required=True)
    parser.add_argument("--restart-container")
    parser.add_argument("--startup-timeout", type=float, default=180)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    session = requests.Session()
    base = args.url.rstrip("/")
    results = {}

    def status():
        response = session.get(base + "/_license/status", timeout=10)
        response.raise_for_status()
        return response.json()

    def wait_ready():
        deadline = time.monotonic() + args.startup_timeout
        while True:
            try:
                return status()
            except requests.RequestException:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)

    def activate(raw):
        response = session.get(base + "/_license/", timeout=10)
        response.raise_for_status()
        assert "产品授权" in response.text
        form = FormParser()
        form.feed(response.text)
        assert form.token, "Activation page did not supply a CSRF token"
        response = session.post(base + "/_license/activate",
                                data={"csrf": form.token, "license": raw.decode()},
                                allow_redirects=False, timeout=10)
        assert response.status_code in (200, 303), response.status_code

    def assert_expired():
        assert status()["code"] == "LICENSE_EXPIRED"
        for method, path in (("GET", "/any-business-page"), ("GET", "/api/appguard-expiry-check"),
                             ("POST", "/jobs/appguard-expiry-check")):
            response = session.request(method, base + path, headers={"Accept": "text/html"},
                                       allow_redirects=False, timeout=10)
            assert response.status_code == 403, (path, response.status_code)
            assert response.json()["code"] == "LICENSE_EXPIRED"
            assert "Location" not in response.headers
        assert "授权已到期" in session.get(base + "/_license/", timeout=10).text

    restore = args.restore_license.read_bytes()
    issuer = signer(args.issuer_key)
    wait_ready()
    try:
        activate(restore)
        assert status()["valid"]
        expires = int(time.time()) + 8
        license = {"format": 2, "kind": "license", "license_id": uuid.uuid4().hex,
                   "customer": "HTTP expiry test", "product_id": args.product,
                   "issued_at": int(time.time()), "not_before": int(time.time()) - 60,
                   "expires_at": expires}
        activate(signed(license, issuer))
        assert status()["valid"]
        deadline = time.monotonic() + 25
        while status()["valid"] and time.monotonic() < deadline:
            time.sleep(0.25)
        assert_expired()
        results["expiry_blocks_all_business_requests"] = True
        if args.restart_container:
            subprocess.run(["docker", "restart", args.restart_container], check=True,
                           capture_output=True, timeout=45)
            wait_ready()
            assert_expired()
            results["expired_restart_keeps_activation_available"] = True
    finally:
        wait_ready()
        activate(restore)
        results["valid_license_restored"] = status()["valid"]
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2))
    print(json.dumps(results))


if __name__ == "__main__":
    main()
