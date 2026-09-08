"""Run inside the encrypted image with publisher tooling mounted for tests only.

Requires APPGUARD_TEST_RELEASE and APPGUARD_TEST_ISSUER; test keys are never
installed in the shipping image. Each subprocess receives a fresh license dir.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from appguard.crypto import signed, signer, wrap_key


pytestmark = pytest.mark.skipif(not os.environ.get("APPGUARD_TEST_RELEASE"), reason="Requires encrypted image")


@pytest.fixture
def deployment(tmp_path):
    env = dict(os.environ, APPGUARD_LICENSE_DIR=str(tmp_path / "license"))
    result = subprocess.run([sys.executable, "-m", "appguard_host", "enroll"], env=env, capture_output=True, text=True, check=True)
    request = json.loads(result.stdout)
    release = json.loads(Path(os.environ["APPGUARD_TEST_RELEASE"]).read_text())
    issuer = signer(Path(os.environ["APPGUARD_TEST_ISSUER"]))
    return env, request, release, issuer


def license_bytes(deployment, expires=None, **overrides):
    _, request, release, issuer = deployment
    now = int(time.time())
    payload = {"format": 1, "license_id": uuid.uuid4().hex, "customer": "Image test",
               "product_id": release["product_id"], "build_id": release["build_id"],
               "deployment_public": request["deployment_public"], "issued_at": now,
               "not_before": now - 3600, "expires_at": expires or now + 600,
               "wrapped_key": wrap_key(bytes.fromhex(release["content_key"]), request["deployment_public"], release["build_id"])}
    payload.update(overrides)
    return signed(payload, issuer)


def run(deployment, code, check=True):
    result = subprocess.run([sys.executable, "-c", code], env=deployment[0], capture_output=True, text=True)
    if check and result.returncode:
        pytest.fail(result.stderr[-10000:])
    return result


def store(deployment, data):
    path = Path(deployment[0]["APPGUARD_LICENSE_DIR"]) / "license.json"
    path.write_bytes(data)
    return path


def test_missing_license_blocks_direct_business_import(deployment):
    result = run(deployment, "import app", check=False)
    assert result.returncode != 0
    assert "LICENSE_MISSING" in result.stderr


def test_valid_license_loads_encrypted_business_and_native_extension(deployment):
    store(deployment, license_bytes(deployment))
    result = run(deployment, "import guard_runtime; from app.utils.kb_ids import validate_kb_id; print(guard_runtime.__file__); print(validate_kb_id('graphrag/example'))")
    assert ".so" in result.stdout
    assert "graphrag/example" in result.stdout


@pytest.mark.parametrize("kind", ["expired", "future", "wrong_build", "wrong_device", "tampered"])
def test_invalid_licenses_are_rejected(deployment, kind):
    options = {
        "expired": {"expires_at": int(time.time()) - 10},
        "future": {"not_before": int(time.time()) + 120},
        "wrong_build": {"build_id": "wrong"},
        "wrong_device": {"deployment_public": "00" * 32},
    }
    data = license_bytes(deployment, **options.get(kind, {}))
    if kind == "tampered":
        envelope = json.loads(data)
        payload = json.loads(base64.b64decode(envelope["payload"]))
        payload["expires_at"] += 100000
        envelope["payload"] = base64.b64encode(json.dumps(payload).encode()).decode()
        data = json.dumps(envelope).encode()
    store(deployment, data)
    result = run(deployment, "import app", check=False)
    assert result.returncode != 0
    assert "LICENSE_" in result.stderr


def test_loaded_function_rechecks_expiry_without_restart(deployment):
    store(deployment, license_bytes(deployment, expires=int(time.time()) + 4))
    result = run(deployment, "import time; from app import create_app; import guard_runtime; time.sleep(4.2); create_app()", check=False)
    assert result.returncode != 0
    assert "LICENSE_EXPIRED" in result.stderr


def test_tampered_module_fails_authenticated_load(deployment, tmp_path):
    store(deployment, license_bytes(deployment))
    bundle = tmp_path / "bundle"
    shutil.copytree(os.environ["APPGUARD_BUNDLE"], bundle)
    manifest = json.loads(base64.b64decode(json.loads((bundle / "manifest.json").read_text())["payload"]))
    path = bundle / "modules" / manifest["modules"]["backend/app/__init__.py"]["file"]
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    deployment[0]["APPGUARD_BUNDLE"] = str(bundle)
    result = run(deployment, "import app", check=False)
    assert result.returncode != 0
    assert "MODULE_INVALID" in result.stderr


def test_activation_portal_and_renewal_preserve_good_license(deployment, tmp_path):
    candidate = tmp_path / "candidate.license"
    candidate.write_bytes(license_bytes(deployment))
    code = f'''
from werkzeug.test import Client
from werkzeug.wrappers import Response
from appguard_host import create_host
import guard_runtime
client = Client(create_host(), Response)
assert client.get('/anything', headers={{'Accept':'text/html'}}).status_code == 302
assert client.get('/api/query').status_code == 403
assert client.get('/anything', headers={{'Accept':'*/*', 'Sec-Fetch-Dest':'document'}}).status_code == 302
assert client.post('/api/query', json={{}}).status_code == 403
page = client.get('/_license/')
assert page.status_code == 200
assert '产品授权' in page.get_data(as_text=True)
assert '尚未激活' in page.get_data(as_text=True)
assert 'Product activation' not in page.get_data(as_text=True)
assert client.post('/_license/activate', data={{'license':'bad'}}).status_code == 403
token = client.get_cookie('appguard_csrf', path='/_license/').value
data = open({str(candidate)!r}).read()
assert client.post('/_license/activate', data={{'csrf':token,'license':data}}).status_code == 303
assert guard_runtime.status()['valid']
assert client.get('/openapi/openapi.json').status_code == 200
assert client.post('/_license/activate', data={{'csrf':token,'license':'bad'}}).status_code == 400
assert guard_runtime.status()['valid']
'''
    run(deployment, code)


def test_encrypted_query_script_help(deployment):
    store(deployment, license_bytes(deployment))
    result = subprocess.run([sys.executable, "/app/backend/scripts/query_graphrag3_block_kb.py", "--help"], env=deployment[0], capture_output=True, text=True, check=True)
    assert "--query" in result.stdout


def test_response_cleanup_before_first_chunk_and_on_expiry(deployment):
    store(deployment, license_bytes(deployment))
    run(deployment, '''
from appguard_host import LicensedIterable
import guard_runtime
from pathlib import Path
import os
class Source:
    closed = False
    def __iter__(self): return iter([b'chunk'])
    def close(self): self.closed = True
source = Source()
LicensedIterable(source).close()
assert source.closed
source = Source()
stream = LicensedIterable(source)
Path(os.environ['APPGUARD_LICENSE_DIR'], 'license.json').unlink()
try:
    next(stream)
except guard_runtime.LicenseError:
    pass
else:
    raise AssertionError('Missing license should stop an existing response')
assert source.closed
''')


def test_replacing_public_check_does_not_bypass_native_load(deployment):
    result = run(deployment, "import guard_runtime; guard_runtime.require_valid = lambda: None; import app", check=False)
    assert result.returncode != 0
    assert "LICENSE_MISSING" in result.stderr
