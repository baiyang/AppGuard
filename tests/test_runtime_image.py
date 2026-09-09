"""Compile real customer runtimes in temporary directories and test isolated processes."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from appguard.build import build
from appguard.crypto import signed


ROOT = Path(__file__).resolve().parents[1]
PRODUCT = "runtime-test-product"
SERVICE = '''
def answer(value):
    return value + 42

def outer(value):
    def inner(extra):
        return value + extra
    return inner

class Example:
    def work(self, value=2, /, *items, scale=3, **options):
        return (value + sum(items)) * scale + options.get("offset", 0)

def stream():
    try:
        value = yield "first"
        yield value
    except ValueError:
        yield "caught"
    finally:
        events.append("closed")
    return 17

async def calculate(value=4):
    return value * 2

async def async_stream():
    yield 3
    yield 5

events = []
'''
WEB_APP = '''
from flask import Flask, request
from appguard_flask import AppGuard
from service import answer

app = Flask(__name__)

@app.get("/")
def index():
    return {"status": "ok"}

@app.get("/api/answer")
def calculate():
    return {"answer": answer(request.args.get("value", 0, type=int))}

@app.post("/jobs/reindex")
def reindex():
    return {"answer": answer(0)}

AppGuard().init_app(app)
'''


@pytest.fixture(scope="session")
def publisher(tmp_path_factory):
    assert sys.version_info[:2] == (3, 11), "Run native integration tests with Python 3.11"
    directory = tmp_path_factory.mktemp("native-runtime")
    issuer = Ed25519PrivateKey.generate()
    issuer_path = directory / "issuer.key"
    issuer_path.write_text(issuer.private_bytes_raw().hex())
    source = directory / "source"
    source.mkdir()
    (source / "service.py").write_text(SERVICE)
    (source / "web_app.py").write_text(WEB_APP)
    config = directory / "guard.toml"
    config.write_text(f'product_id = "{PRODUCT}"\ninclude = ["*.py"]\n')
    return {"directory": directory, "issuer": issuer, "issuer_path": issuer_path,
            "source": source, "config": config}


def make_artifact(publisher, name, code_key=None, native=None):
    directory = publisher["directory"] / name
    directory.mkdir()
    code_key = code_key or os.urandom(32)
    code_key_path = directory / "code.key"
    code_key_path.write_text(code_key.hex())
    release = directory / "release"
    metadata = build(publisher["source"], publisher["config"], publisher["issuer_path"],
                     release, code_key_path)
    if native is None:
        compiler = directory / "compiler"
        compiler.mkdir()
        for filename in ("setup.py", "pyproject.toml", "appguard_host.py", "appguard_flask.py"):
            shutil.copy2(ROOT / filename, compiler / filename)
        shutil.copytree(ROOT / "runtime", compiler / "runtime", ignore=shutil.ignore_patterns("*.c", "*.so"))
        native = directory / "native"
        env = dict(os.environ,
                   APPGUARD_PUBLIC_KEY=publisher["issuer"].public_key().public_bytes_raw().hex(),
                   APPGUARD_CODE_KEY=code_key.hex())
        result = subprocess.run(
            [sys.executable, "setup.py", "build_ext", "--build-lib", str(native)],
            cwd=compiler, env=env, capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == 0, result.stdout[-5000:] + result.stderr[-10000:]
        assert list(native.glob("guard_runtime*")), "Native extension was not produced"
    return {"bundle": release / "bundle", "native": native,
            "code_key": code_key, "metadata": metadata}


@pytest.fixture(scope="session")
def artifact(publisher):
    return make_artifact(publisher, "first")


@pytest.fixture(scope="session")
def upgraded_artifact(publisher, artifact):
    return make_artifact(publisher, "upgrade", artifact["code_key"], artifact["native"])


@pytest.fixture(scope="session")
def rotated_artifact(publisher):
    return make_artifact(publisher, "rotated")


@pytest.fixture
def deployment(tmp_path, publisher, artifact):
    license_dir = tmp_path / "license"
    license_dir.mkdir()
    env = dict(os.environ,
               APPGUARD_LICENSE_DIR=str(license_dir),
               APPGUARD_BUNDLE=str(artifact["bundle"]),
               PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join(map(str, [artifact["native"], ROOT,
                                                   artifact["bundle"] / "tree"])))
    return {"env": env, "cwd": tmp_path, "publisher": publisher, "artifact": artifact,
            "license_dir": license_dir}


def license_payload(**overrides):
    now = int(time.time())
    payload = {"format": 2, "kind": "license", "license_id": uuid.uuid4().hex,
               "product_id": PRODUCT, "customer": "Runtime test", "issued_at": now,
               "not_before": now - 3600, "expires_at": now + 600}
    payload.update(overrides)
    return payload


def license_bytes(deployment, **overrides):
    return signed(license_payload(**overrides), deployment["publisher"]["issuer"])


def run(deployment, code, check=True):
    result = subprocess.run([sys.executable, "-c", code], env=deployment["env"],
                            cwd=deployment["cwd"], capture_output=True, text=True, timeout=30)
    if check and result.returncode:
        pytest.fail(result.stdout[-5000:] + result.stderr[-10000:])
    return result


def store(deployment, data):
    path = deployment["license_dir"] / "license.json"
    temporary = path.with_suffix(".new")
    temporary.write_bytes(data)
    temporary.replace(path)
    return path


def with_artifact(deployment, artifact):
    env = dict(deployment["env"], APPGUARD_BUNDLE=str(artifact["bundle"]),
               PYTHONPATH=os.pathsep.join(map(str, [artifact["native"], ROOT,
                                                   artifact["bundle"] / "tree"])))
    return dict(deployment, env=env, artifact=artifact)


def test_missing_license_keeps_startup_portal_and_direct_code_available(deployment):
    run(deployment, '''
from web_app import app
from service import answer
import guard_runtime
assert "appguard" in app.extensions
assert answer(8) == 50
assert app.test_client().get("/_license/").status_code == 200
assert not guard_runtime.status()["valid"]
assert guard_runtime.status()["code"] == "LICENSE_MISSING"
assert guard_runtime.product_info()["product_id"] == "runtime-test-product"
''')


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("path", ["/", "/api/answer", "/jobs/reindex", "/admin/dashboard", "/static/app.js"])
def test_all_business_requests_are_403_regardless_of_accept(deployment, method, path):
    run(deployment, f'''
from web_app import app
client = app.test_client()
for headers in ({{}}, {{"Accept": "text/html"}}, {{"Accept": "*/*", "Sec-Fetch-Dest": "document"}}):
    response = client.open({path!r}, method={method!r}, headers=headers)
    assert response.status_code == 403
    assert "Location" not in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    if {method!r} != "HEAD":
        assert response.json["code"] == "LICENSE_MISSING"
''')


def test_valid_license_allows_business_http_and_loads_native_module(deployment):
    store(deployment, license_bytes(deployment))
    run(deployment, '''
import guard_runtime
from web_app import app
assert guard_runtime.__file__.endswith((".so", ".pyd"))
assert guard_runtime.status()["valid"]
assert app.test_client().get("/api/answer?value=8").json == {"answer": 50}
assert app.test_client().post("/jobs/reindex").json == {"answer": 42}
assert app.test_client().get("/does-not-exist").status_code == 404
''')


@pytest.mark.parametrize(("overrides", "code"), [
    ({"expires_at": 1}, "LICENSE_EXPIRED"),
    ({"not_before": 4_000_000_000, "expires_at": 4_000_000_001}, "LICENSE_NOT_YET_VALID"),
    ({"product_id": "another-product"}, "LICENSE_WRONG_PRODUCT"),
])
def test_rejected_licenses_are_uniformly_blocked(deployment, overrides, code):
    if overrides.get("expires_at") == 1:
        overrides = dict(overrides, not_before=0, issued_at=0)
    store(deployment, license_bytes(deployment, **overrides))
    run(deployment, f'''
import guard_runtime
from web_app import app
assert not guard_runtime.status()["valid"]
assert guard_runtime.status()["code"] == {code!r}
response = app.test_client().get("/jobs/reindex", headers={{"Accept": "text/html"}})
assert response.status_code == 403
assert response.json["code"] == {code!r}
assert app.test_client().get("/_license/").status_code == 200
''')


@pytest.mark.parametrize("kind", ["tampered", "wrong_issuer", "old_format", "wrong_kind", "bool_time", "string_time",
                                  "float_time", "missing_field", "extra_field", "bad_interval",
                                  "empty_product", "empty_license_id", "json_array", "oversized"])
def test_malformed_or_forged_licenses_fail_closed(deployment, kind):
    payload = license_payload()
    if kind == "old_format":
        payload["format"] = 1
    elif kind == "wrong_kind":
        payload["kind"] = "manifest"
    elif kind == "bool_time":
        payload["issued_at"] = True
    elif kind == "string_time":
        payload["expires_at"] = str(payload["expires_at"])
    elif kind == "float_time":
        payload["not_before"] = float(payload["not_before"])
    elif kind == "missing_field":
        del payload["license_id"]
    elif kind == "extra_field":
        payload["build_id"] = "old-build-binding"
    elif kind == "bad_interval":
        payload["not_before"] = payload["expires_at"]
    elif kind == "empty_product":
        payload["product_id"] = ""
    elif kind == "empty_license_id":
        payload["license_id"] = ""
    elif kind == "json_array":
        payload = []
    raw = signed(payload, deployment["publisher"]["issuer"])
    if kind == "tampered":
        envelope = json.loads(raw)
        payload["expires_at"] += 100000
        envelope["payload"] = base64.b64encode(json.dumps(payload).encode()).decode()
        raw = json.dumps(envelope).encode()
    elif kind == "oversized":
        raw = b" " * 65537
    elif kind == "wrong_issuer":
        raw = signed(payload, Ed25519PrivateKey.generate())
    store(deployment, raw)
    run(deployment, '''
import guard_runtime
from web_app import app
assert not guard_runtime.status()["valid"]
assert guard_runtime.status()["code"] == "LICENSE_INVALID"
assert app.test_client().get("/").status_code == 403
''')


def test_duplicate_signed_json_fields_are_rejected(deployment):
    payload = json.dumps(license_payload()).encode()
    payload = payload[:-1] + b', "expires_at": 4000000000}'
    issuer = deployment["publisher"]["issuer"]
    raw = json.dumps({"payload": base64.b64encode(payload).decode(),
                      "signature": base64.b64encode(issuer.sign(payload)).decode()}).encode()
    store(deployment, raw)
    run(deployment, '''
import guard_runtime
assert not guard_runtime.status()["valid"]
assert guard_runtime.status()["code"] == "LICENSE_INVALID"
''')


@pytest.mark.parametrize(("clock_value", "code"), [
    ("-1", "CLOCK_STATE_INVALID"),
    ("not-a-timestamp", "CLOCK_STATE_INVALID"),
    (str(1 << 128), "CLOCK_STATE_INVALID"),
    ("future", "CLOCK_ROLLBACK"),
])
def test_invalid_clock_state_keeps_repeated_status_and_api_checks_closed(deployment, clock_value, code):
    store(deployment, license_bytes(deployment))
    if clock_value == "future":
        clock_value = str(int(time.time()) + 3600)
    (deployment["license_dir"] / "last-seen").write_text(clock_value)
    run(deployment, f'''
import guard_runtime
from web_app import app
client = app.test_client()
for _ in range(2):
    state = guard_runtime.status()
    assert not state["valid"]
    assert state["code"] == {code!r}
    response = client.get("/api/answer?value=8")
    assert response.status_code == 403
    assert response.json["code"] == {code!r}
''')


def test_activation_csrf_upload_and_bad_renewal_preserve_working_license(deployment):
    candidate = deployment["cwd"] / "candidate.license"
    candidate.write_bytes(license_bytes(deployment))
    run(deployment, f'''
import io
from pathlib import Path
import guard_runtime
from web_app import app
from appguard_flask import AppGuard
original = app.wsgi_app
AppGuard().init_app(app)
assert app.wsgi_app is original
client = app.test_client()
assert client.get("/_license/request").status_code == 404
assert client.get("/_license/status").json["code"] == "LICENSE_MISSING"
page = client.get("/_license/")
assert page.status_code == 200
assert "产品授权" in page.get_data(as_text=True)
assert client.post("/_license/activate", data={{"license": "bad"}}).status_code == 403
token = client.get_cookie("appguard_csrf", path="/_license/").value
raw = Path({str(candidate)!r}).read_bytes()
response = client.post("/_license/activate", data={{"csrf": token, "file": (io.BytesIO(raw), "customer.license")}})
assert response.status_code in (200, 303)
state = guard_runtime.status()
assert state["valid"]
assert client.get("/api/answer?value=8").json == {{"answer": 50}}
assert client.post("/_license/activate", data={{"csrf": "非ASCII", "license": raw.decode()}}).status_code == 403
assert guard_runtime.status() == state
client.set_cookie("appguard_csrf", "非ASCII", path="/_license/")
assert client.post("/_license/activate", data={{"csrf": token, "license": raw.decode()}}).status_code == 403
assert guard_runtime.status() == state
client.set_cookie("appguard_csrf", token, path="/_license/")
for invalid in ("bad", "{{", "[]", "null"):
    token = client.get_cookie("appguard_csrf", path="/_license/").value
    response = client.post("/_license/activate", data={{"csrf": token, "license": invalid}})
    assert response.status_code == 400
    assert guard_runtime.status() == state
token = client.get_cookie("appguard_csrf", path="/_license/").value
response = client.post("/_license/activate", data={{"csrf": token, "file": (io.BytesIO(b"x" * 100001), "huge.license")}})
assert response.status_code == 413
assert guard_runtime.status() == state
''')


@pytest.mark.parametrize(("overrides", "code"), [
    ({"product_id": "another-product"}, "LICENSE_WRONG_PRODUCT"),
    ({"not_before": 0, "issued_at": 0, "expires_at": 1}, "LICENSE_EXPIRED"),
    ({"not_before": 4_000_000_000, "expires_at": 4_000_000_001}, "LICENSE_NOT_YET_VALID"),
])
def test_signed_but_unusable_renewal_preserves_license_on_disk(deployment, overrides, code):
    store(deployment, license_bytes(deployment))
    candidate = deployment["cwd"] / "rejected.license"
    candidate.write_bytes(license_bytes(deployment, **overrides))
    run(deployment, f'''
import os
from pathlib import Path
import guard_runtime
from web_app import app
path = Path(os.environ["APPGUARD_LICENSE_DIR"], "license.json")
original = path.read_bytes()
state = guard_runtime.status()
assert state["valid"]
try:
    guard_runtime.install_license(Path({str(candidate)!r}).read_bytes())
except guard_runtime.LicenseError as exc:
    assert str(exc) == {code!r}
else:
    raise AssertionError("Invalid renewal replaced a working license")
assert path.read_bytes() == original
assert guard_runtime.status() == state
assert app.test_client().get("/api/answer?value=8").json == {{"answer": 50}}
''')


def test_cached_license_does_not_hide_replacement_with_invalid_file(deployment):
    store(deployment, license_bytes(deployment))
    run(deployment, '''
import os
from pathlib import Path
import guard_runtime
from web_app import app
path = Path(os.environ["APPGUARD_LICENSE_DIR"], "license.json")
original = path.read_bytes()
client = app.test_client()
assert client.get("/api/answer?value=8").json == {"answer": 50}
path.write_bytes(b"{}")
for _ in range(2):
    response = client.get("/api/answer?value=8")
    assert response.status_code == 403
    assert response.json["code"] == "LICENSE_INVALID"
    assert not guard_runtime.status()["valid"]
path.write_bytes(original)
assert client.get("/api/answer?value=8").json == {"answer": 50}
''')


def test_hot_expiry_blocks_next_request_but_existing_response_and_cli_continue(deployment):
    expires = int(time.time()) + 4
    store(deployment, license_bytes(deployment, expires_at=expires))
    run(deployment, f'''
import time
import guard_runtime
from werkzeug.test import EnvironBuilder
from appguard_host import LicenseMiddleware
from service import answer
from web_app import app
def business(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return iter([b"before", b"after"])
response = LicenseMiddleware(business)(EnvironBuilder(path="/").get_environ(), lambda *args: None)
iterator = iter(response)
assert next(iterator) == b"before"
assert guard_runtime.status()["valid"]
time.sleep(max(0, {expires} - time.time()) + 0.2)
assert next(iterator) == b"after"
assert answer(8) == 50
assert guard_runtime.status()["code"] == "LICENSE_EXPIRED"
client = app.test_client()
assert client.get("/", headers={{"Accept": "text/html"}}).status_code == 403
assert client.get("/_license/").status_code == 200
''')
    run(deployment, '''
from web_app import app
assert app.test_client().get("/_license/status").json["code"] == "LICENSE_EXPIRED"
assert app.test_client().get("/_license/").status_code == 200
''')


def test_renewal_is_seen_by_all_running_workers(deployment):
    first = license_payload()
    store(deployment, signed(first, deployment["publisher"]["issuer"]))
    program = '''
import guard_runtime
print(guard_runtime.status()["license_id"], flush=True)
input()
print(guard_runtime.status()["license_id"], flush=True)
'''
    workers = [subprocess.Popen([sys.executable, "-c", program], cwd=deployment["cwd"],
                                env=deployment["env"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
               for _ in range(2)]
    try:
        for worker in workers:
            assert worker.stdout.readline().strip() == first["license_id"]
        renewed = license_payload(expires_at=first["expires_at"] + 3600)
        store(deployment, signed(renewed, deployment["publisher"]["issuer"]))
        for worker in workers:
            stdout, stderr = worker.communicate("continue\n", timeout=10)
            assert worker.returncode == 0, stderr
            assert stdout.strip() == renewed["license_id"]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=10)


def test_product_license_survives_build_upgrade_and_code_key_rotation(deployment, upgraded_artifact, rotated_artifact):
    store(deployment, license_bytes(deployment))
    builds = set()
    for artifact in (deployment["artifact"], upgraded_artifact, rotated_artifact):
        result = run(with_artifact(deployment, artifact), '''
import guard_runtime
from web_app import app
assert guard_runtime.status()["valid"]
assert app.test_client().get("/api/answer?value=8").json == {"answer": 50}
print(guard_runtime.product_info()["build_id"])
''')
        builds.add(result.stdout.strip())
    assert len(builds) == 3


def test_whole_modules_keep_closures_methods_and_async_generator_semantics(deployment):
    run(deployment, '''
import asyncio
import inspect
import service
assert service.outer(10)(5) == 15
assert service.Example().work() == 6
assert service.Example().work(2, 4, scale=2, offset=1) == 13
assert inspect.isgeneratorfunction(service.stream)
stream = service.stream()
assert next(stream) == "first"
assert stream.send("second") == "second"
assert stream.throw(ValueError()) == "caught"
try:
    next(stream)
except StopIteration as complete:
    assert complete.value == 17
else:
    raise AssertionError("Generator did not stop")
assert service.events == ["closed"]
assert inspect.iscoroutinefunction(service.calculate)
assert asyncio.run(service.calculate()) == 8
assert inspect.isasyncgenfunction(service.async_stream)
async def collect():
    return [value async for value in service.async_stream()]
assert asyncio.run(collect()) == [3, 5]
''')


@pytest.mark.parametrize("kind", ["ciphertext", "authenticated_ciphertext", "manifest_signature", "old_manifest", "bad_layout"])
def test_tampered_bundle_fails_authenticated_load(deployment, kind):
    bundle = deployment["cwd"] / "tampered-bundle"
    shutil.copytree(deployment["artifact"]["bundle"], bundle)
    envelope = json.loads((bundle / "manifest.json").read_bytes())
    manifest = json.loads(base64.b64decode(envelope["payload"]))
    if kind in {"ciphertext", "authenticated_ciphertext"}:
        path = bundle / "modules" / manifest["modules"]["service.py"]["file"]
        blob = bytearray(path.read_bytes())
        blob[-1] ^= 1
        path.write_bytes(blob)
        if kind == "authenticated_ciphertext":
            manifest["modules"]["service.py"]["sha256"] = hashlib.sha256(blob).hexdigest()
            (bundle / "manifest.json").write_bytes(signed(manifest, deployment["publisher"]["issuer"]))
    elif kind == "manifest_signature":
        manifest["build_id"] = "forged"
        envelope["payload"] = base64.b64encode(json.dumps(manifest).encode()).decode()
        (bundle / "manifest.json").write_text(json.dumps(envelope))
    else:
        manifest["format" if kind == "old_manifest" else "layout"] = 1 if kind == "old_manifest" else "function-bodies-v1"
        (bundle / "manifest.json").write_bytes(signed(manifest, deployment["publisher"]["issuer"]))
    result = run(dict(deployment, env=dict(deployment["env"], APPGUARD_BUNDLE=str(bundle))),
                 "import service", check=False)
    assert result.returncode != 0
    assert ("MODULE_INVALID" if kind in {"ciphertext", "authenticated_ciphertext"} else "BUNDLE_INVALID") in result.stderr
    if kind == "authenticated_ciphertext":
        assert "InvalidTag" in result.stderr


def test_runtime_rejects_bundle_encrypted_for_another_code_key(deployment, rotated_artifact):
    result = run(dict(deployment, env=dict(deployment["env"], APPGUARD_BUNDLE=str(rotated_artifact["bundle"]))),
                 "import service", check=False)
    assert result.returncode != 0
    assert "BUNDLE_INVALID" in result.stderr


def test_explicit_health_exemptions_are_exact_get_and_head_only(deployment):
    run(deployment, '''
from appguard_host import LicenseMiddleware
from werkzeug.test import Client
from werkzeug.wrappers import Response
def health(environ, start_response):
    return Response("ok")(environ, start_response)
client = Client(LicenseMiddleware(health, exempt_paths=("/healthz",)), Response)
assert client.get("/healthz").status_code == 200
assert client.head("/healthz").status_code == 200
assert client.post("/healthz").status_code == 403
assert client.get("/healthz/details").status_code == 403
assert client.get("/healthz/").status_code == 403
''')


def test_removing_middleware_is_outside_the_authorization_boundary(deployment):
    run(deployment, '''
from web_app import app
app.wsgi_app = app.wsgi_app.application
assert app.test_client().get("/api/answer?value=8").json == {"answer": 50}
''')


def test_license_cli_installs_and_reports_status_without_enrollment(deployment):
    candidate = deployment["cwd"] / "candidate.license"
    candidate.write_bytes(license_bytes(deployment))
    for arguments in (["install", str(candidate)], ["status"]):
        result = subprocess.run([sys.executable, "-m", "appguard_host", *arguments],
                                cwd=deployment["cwd"], env=deployment["env"],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["valid"]
    assert {path.name for path in deployment["license_dir"].iterdir()} <= {"license.json", "last-seen"}
