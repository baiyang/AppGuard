"""Exercise activation, real-time expiry, restart and renewal on a TEST deployment.

This temporarily installs a short-lived license and optionally restarts the named
test container. The supplied valid license is restored in a finally block.
"""

import argparse
import json
import subprocess
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path

import requests

from appguard.crypto import signed, signer, wrap_key


class FormParser(HTMLParser):
    token = ""

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "input" and fields.get("name") == "csrf":
            self.token = fields.get("value", "")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--issuer-key", type=Path, required=True)
    parser.add_argument("--restore-license", type=Path, required=True)
    parser.add_argument("--restart-container")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    session = requests.Session()
    base = args.url.rstrip("/")
    results = {}

    def status():
        response = session.get(base + "/_license/status", timeout=10)
        response.raise_for_status()
        return response.json()

    def activate(raw):
        response = session.get(base + "/_license/", timeout=10)
        response.raise_for_status()
        assert "产品授权" in response.text
        form = FormParser()
        form.feed(response.text)
        response = session.post(base + "/_license/activate",
                                data={"csrf": form.token, "license": raw.decode()},
                                allow_redirects=False, timeout=10)
        assert response.status_code == 303, response.status_code

    restore = args.restore_license.read_bytes()
    release = json.loads(args.release.read_text())
    request = session.get(base + "/_license/request", timeout=10).json()
    assert (request["build_id"], request["product_id"]) == (release["build_id"], release["product_id"])
    try:
        activate(restore)
        assert status()["valid"]
        expires = int(time.time()) + 8
        license = {"format": 1, "license_id": uuid.uuid4().hex, "customer": "HTTP expiry test",
                   "product_id": release["product_id"], "build_id": release["build_id"],
                   "deployment_public": request["deployment_public"], "issued_at": int(time.time()),
                   "not_before": int(time.time()) - 60, "expires_at": expires,
                   "wrapped_key": wrap_key(bytes.fromhex(release["content_key"]), request["deployment_public"], release["build_id"])}
        activate(signed(license, signer(args.issuer_key)))
        assert status()["valid"]
        deadline = time.monotonic() + 25
        while status()["valid"] and time.monotonic() < deadline:
            time.sleep(0.25)
        assert status()["code"] == "LICENSE_EXPIRED"
        response = session.get(base + "/any-business-page", headers={"Accept": "text/html"}, allow_redirects=False, timeout=10)
        assert response.status_code == 302 and response.headers["Location"] == "/_license/"
        response = session.post(base + "/api/query", json={}, timeout=10)
        assert response.status_code == 403 and response.json()["code"] == "LICENSE_EXPIRED"
        assert "授权已到期" in session.get(base + "/_license/", timeout=10).text
        results["expiry_blocks_pages_and_api"] = True
        if args.restart_container:
            subprocess.run(["docker", "restart", args.restart_container], check=True, capture_output=True, timeout=45)
            deadline = time.monotonic() + 30
            while True:
                try:
                    state = status()
                    break
                except requests.RequestException:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.5)
            assert state["code"] == "LICENSE_EXPIRED"
            assert session.get(base + "/_license/", timeout=10).status_code == 200
            results["expired_restart_keeps_activation_available"] = True
    finally:
        activate(restore)
        results["valid_license_restored"] = status()["valid"]
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2))
    print(json.dumps(results))


if __name__ == "__main__":
    main()
