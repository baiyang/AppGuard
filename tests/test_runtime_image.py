"""Test the encrypted examples/flask image with publisher inputs mounted only for tests.

Requires APPGUARD_TEST_RELEASE and APPGUARD_TEST_ISSUER; test keys are never
installed in the shipping image. Each subprocess receives a fresh license dir.
"""

import base64
import hashlib
import json
import os
import marshal
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from appguard.crypto import signed, signer, wrap_key
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


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


def test_missing_license_allows_startup_but_blocks_core_calls(deployment):
    run(deployment, "from web_app import app; assert 'appguard' in app.extensions; assert app.test_client().get('/_license/').status_code == 200")
    result = run(deployment, "from service import answer; answer(8)", check=False)
    assert result.returncode != 0
    assert "LICENSE_MISSING" in result.stderr


def test_valid_license_loads_encrypted_business_and_native_extension(deployment):
    store(deployment, license_bytes(deployment))
    result = run(deployment, "import guard_runtime; from service import answer; print(guard_runtime.__file__); print(answer(8))")
    assert ".so" in result.stdout
    assert result.stdout.splitlines()[-1] == "50"


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
    result = run(deployment, "from service import answer; answer(8)", check=False)
    assert result.returncode != 0
    assert "LICENSE_" in result.stderr


def test_loaded_function_rechecks_expiry_without_restart(deployment):
    store(deployment, license_bytes(deployment, expires=int(time.time()) + 4))
    result = run(deployment, "import time; from service import answer; time.sleep(4.2); answer(8)", check=False)
    assert result.returncode != 0
    assert "LICENSE_EXPIRED" in result.stderr


def test_tampered_module_fails_authenticated_load(deployment, tmp_path):
    store(deployment, license_bytes(deployment))
    bundle = tmp_path / "bundle"
    shutil.copytree(os.environ["APPGUARD_BUNDLE"], bundle)
    manifest = json.loads(base64.b64decode(json.loads((bundle / "manifest.json").read_text())["payload"]))
    path = bundle / "modules" / manifest["modules"]["service.py"]["file"]
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    deployment[0]["APPGUARD_BUNDLE"] = str(bundle)
    result = run(deployment, "import service", check=False)
    assert result.returncode != 0
    assert "MODULE_INVALID" in result.stderr


def test_activation_portal_and_renewal_preserve_good_license(deployment, tmp_path):
    candidate = tmp_path / "candidate.license"
    candidate.write_bytes(license_bytes(deployment))
    code = f'''
from werkzeug.test import Client
from werkzeug.wrappers import Response
from web_app import app
from appguard_flask import AppGuard
import guard_runtime
original = app.wsgi_app
AppGuard().init_app(app)
assert app.wsgi_app is original
client = Client(app, Response)
assert client.get('/anything', headers={{'Accept':'text/html'}}).status_code == 302
assert client.get('/api/answer').status_code == 403
assert client.get('/anything', headers={{'Accept':'*/*', 'Sec-Fetch-Dest':'document'}}).status_code == 302
assert client.post('/api/answer', json={{}}).status_code == 403
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
assert client.get('/api/answer?value=8').json == {{'answer': 50}}
assert client.post('/_license/activate', data={{'csrf':token,'license':'bad'}}).status_code == 400
assert guard_runtime.status()['valid']
'''
    run(deployment, code)


def test_encrypted_cli_help_and_execution(deployment):
    store(deployment, license_bytes(deployment))
    result = subprocess.run([sys.executable, "/app/cli.py", "--help"], env=deployment[0], capture_output=True, text=True, check=True)
    assert "--value" in result.stdout
    result = subprocess.run([sys.executable, "/app/cli.py", "--value", "8"], env=deployment[0], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "50"


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


def test_replacing_public_check_does_not_bypass_native_execution(deployment):
    result = run(deployment, "import guard_runtime; guard_runtime.require_valid = lambda: None; guard_runtime.invoke_function = lambda *a: None; from service import answer; answer(8)", check=False)
    assert result.returncode != 0
    assert "LICENSE_MISSING" in result.stderr


def test_removing_flask_plugin_still_blocks_business_requests(deployment):
    run(deployment, '''
from web_app import app
app.wsgi_app = app.wsgi_app.application
client = app.test_client()
assert client.get('/api/answer?value=8').status_code >= 400
''')


def test_protected_function_ciphertext_is_verified(deployment, tmp_path):
    store(deployment, license_bytes(deployment))
    bundle = tmp_path / "bundle"
    shutil.copytree(os.environ["APPGUARD_BUNDLE"], bundle)
    manifest = json.loads(base64.b64decode(json.loads((bundle / "manifest.json").read_text())["payload"]))
    identifier = "service.py:answer"
    path = bundle / "functions" / manifest["functions"][identifier]["file"]
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    deployment[0]["APPGUARD_BUNDLE"] = str(bundle)
    result = run(deployment, "from service import answer; answer(8)", check=False)
    assert result.returncode != 0
    assert "MODULE_INVALID" in result.stderr


def test_native_generator_and_async_protocols(deployment, tmp_path):
    store(deployment, license_bytes(deployment))
    bundle = tmp_path / "bundle"
    shutil.copytree(os.environ["APPGUARD_BUNDLE"], bundle)
    manifest = json.loads(base64.b64decode(json.loads((bundle / "manifest.json").read_text())["payload"]))
    program = compile('''
def stream():
    try:
        value = yield "first"
        yield value
    except ValueError:
        yield "caught"
    finally:
        events.append("closed")
    return 17
async def calculate():
    await asyncio.sleep(0)
    return 23
async def cancelled():
    try:
        await asyncio.sleep(60)
    finally:
        events.append("cancelled")
async def expiring():
    try:
        await asyncio.sleep(0)
    finally:
        await asyncio.sleep(0)
        events.append("expired")
    return 99
''', "test_protocols", "exec")
    for code in program.co_consts:
        if not hasattr(code, "co_code"):
            continue
        identifier = "protocol:" + code.co_name
        nonce = os.urandom(12)
        blob = nonce + AESGCM(bytes.fromhex(deployment[2]["content_key"])).encrypt(
            nonce, marshal.dumps(code), f"{manifest['build_id']}:function:{identifier}".encode())
        filename = hashlib.sha256(identifier.encode()).hexdigest() + ".agf"
        (bundle / "functions" / filename).write_bytes(blob)
        manifest["functions"][identifier] = {"file": filename, "sha256": hashlib.sha256(blob).hexdigest()}
    (bundle / "manifest.json").write_bytes(signed(manifest, deployment[3]))
    deployment[0]["APPGUARD_BUNDLE"] = str(bundle)
    run(deployment, '''
import asyncio
import guard_runtime
import os
from pathlib import Path
events = []
namespace = {'events': events, 'asyncio': asyncio}
def invoke(name): return guard_runtime.invoke_function('protocol:' + name, namespace, (), {})
stream = invoke('stream')
assert next(stream) == 'first'
assert stream.send('second') == 'second'
assert stream.throw(ValueError()) == 'caught'
try:
    next(stream)
except StopIteration as complete:
    assert complete.value == 17
assert events == ['closed']
async def dispatch(name): return await invoke(name)
assert asyncio.run(dispatch('calculate')) == 23
async def cancel():
    task = asyncio.create_task(dispatch('cancelled'))
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError('Cancellation was lost')
asyncio.run(cancel())
assert events == ['closed', 'cancelled']
async def expire():
    task = asyncio.create_task(dispatch('expiring'))
    await asyncio.sleep(0)
    Path(os.environ['APPGUARD_LICENSE_DIR'], 'license.json').unlink()
    try:
        await task
    except guard_runtime.LicenseError:
        pass
    else:
        raise AssertionError('Expired coroutine result was returned')
asyncio.run(expire())
assert events == ['closed', 'cancelled', 'expired']
''')
