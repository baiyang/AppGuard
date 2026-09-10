"""Backend API license middleware, activation portal and local install CLI."""

import argparse
import base64
import html
import json
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path

from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.wrappers import Request, Response

import guard_runtime as runtime


STATUS_LABELS = {
    "LICENSE_VALID": "授权有效",
    "LICENSE_MISSING": "尚未激活",
    "LICENSE_EXPIRED": "授权已到期",
    "LICENSE_NOT_YET_VALID": "授权尚未生效",
    "LICENSE_INVALID": "许可证无效",
    "LICENSE_WRONG_PRODUCT": "许可证与当前产品不匹配",
    "CLOCK_ROLLBACK": "系统时间异常",
    "CLOCK_STATE_INVALID": "时间校验记录异常",
    "LICENSE_STATE_UNWRITABLE": "授权状态无法保存",
    "BUNDLE_INVALID": "业务包校验失败",
}


def _validate_exempt_paths(exempt_paths):
    if isinstance(exempt_paths, str):
        raise ValueError("exempt_paths must contain exact absolute health-check paths")
    exempt_paths = tuple(exempt_paths)
    if any(
        not isinstance(path, str) or not path.startswith("/") or any(c in path for c in "?#*\x00")
        for path in exempt_paths
    ):
        raise ValueError("exempt_paths must contain exact absolute health-check paths")
    return frozenset(exempt_paths)


class LicenseMiddleware:
    def __init__(self, application, *, exempt_paths=()):
        self.application = application
        self.exempt_paths = _validate_exempt_paths(exempt_paths)
        self.lock = threading.RLock()
        self.attempts = {}
        self.product = runtime.product_info()

    def _response(self, payload, status=200):
        response = Response(json.dumps(payload, ensure_ascii=False), status=status, content_type="application/json")
        response.headers["Cache-Control"] = "no-store"
        return response

    def _page(self, error="", *, root_path=""):
        state = runtime.status()
        token = secrets.token_urlsafe(32)
        expiration = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(state["expires_at"] + 8 * 3600)) + "（北京时间）" if state["valid"] else "暂无有效授权"
        state_label = STATUS_LABELS.get(state["code"], "授权状态异常")
        product_fields = "".join(
            f"<dt>{label}</dt><dd>{html.escape(self.product[key])}</dd>"
            for label, key in [("产品标识", "product_id"), ("发布版本", "build_id")]
        )
        content = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>产品授权</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f6f8fa;color:#182323;font:15px system-ui,sans-serif;letter-spacing:0}}
main{{max-width:680px;margin:40px auto;padding:24px}}header{{border-bottom:3px solid #12766b;padding-bottom:18px}}
h1{{font-size:26px;margin:10px 0}}h2{{font-size:17px;margin:28px 0 10px}}p{{overflow-wrap:anywhere}}
pre,textarea{{width:100%;padding:12px;border:1px solid #bbc9c6;background:white;border-radius:4px;white-space:pre-wrap;overflow-wrap:anywhere}}
dl{{margin:0}}dt{{color:#596763;font-size:13px;margin-top:12px}}dd{{margin:4px 0 14px;overflow-wrap:anywhere;font:14px monospace}}
textarea{{min-height:150px;resize:vertical;font:13px monospace}}label{{display:block;margin:14px 0 6px}}
button{{background:#12766b;color:white;border:0;border-radius:4px;padding:12px 20px;font:inherit;cursor:pointer}}
input[type=file]{{max-width:100%;margin:8px 0 20px}}.error{{color:#ac233b}}a{{color:#12695f}}
@media(max-width:480px){{main{{margin:12px auto;padding:18px}}}}
</style><main><header>AppGuard<h1>产品授权</h1></header>
<p>授权状态：<strong>{state_label}</strong></p><p>到期时间：{expiration}</p>
<p class="error" role="alert">{html.escape(error)}</p>
<h2>产品信息</h2><dl>{product_fields}</dl>
<h2>更新授权</h2><form action="{html.escape(root_path)}/_license/activate" method="post" enctype="multipart/form-data">
<input type="hidden" name="csrf" value="{token}">
<label for="license">授权码</label><textarea id="license" name="license" spellcheck="false"></textarea>
<label for="file">许可证文件</label><input id="file" type="file" name="file" accept=".json,.license,.lic">
<div><button type="submit">激活授权</button></div></form></main></html>'''
        response = Response(content, content_type="text/html; charset=utf-8")
        response.set_cookie("appguard_csrf", token, httponly=True, samesite="Strict", path=root_path + "/_license/",
                            secure=os.environ.get("APPGUARD_SECURE_COOKIE", "0") == "1")
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"})
        return response

    def _portal(self, request, *, root_path=""):
        path = request.path
        if path in {"/_license", "/_license/"} and request.method in {"GET", "HEAD"}:
            return self._page(root_path=root_path)
        if path == "/_license/status" and request.method in {"GET", "HEAD"}:
            return self._response(runtime.status())
        if path != "/_license/activate" or request.method != "POST":
            return self._response({"error": "页面不存在"}, 404)
        request.max_content_length = 100_000
        try:
            token = request.form.get("csrf", "")
            cookie = request.cookies.get("appguard_csrf", "")
            if (not token or not cookie or not token.isascii() or not cookie.isascii()
                    or not secrets.compare_digest(token, cookie)):
                return self._response({"error": "授权表单已失效，请刷新页面后重试"}, 403)
            with self.lock:
                now = time.monotonic()
                if len(self.attempts) > 1024:
                    self.attempts = {k: v for k, v in self.attempts.items() if v and v[-1] > now - 60}
                attempts = self.attempts.setdefault(request.remote_addr, deque(maxlen=10))
                while attempts and attempts[0] < now - 60:
                    attempts.popleft()
                if len(attempts) >= 10:
                    return self._response({"error": "提交过于频繁，请稍后重试"}, 429)
                attempts.append(now)
            file = request.files.get("file")
            raw = file.read(65537) if file and file.filename else request.form.get("license", "").encode()
            if len(raw) > 65536:
                raise RequestEntityTooLarge()
            raw = raw.strip()
            if raw and not raw.startswith(b"{"):
                raw = base64.b64decode(raw, validate=True)
            runtime.install_license(raw)
        except RequestEntityTooLarge:
            return self._response({"error": "许可证文件过大"}, 413)
        except (ValueError, runtime.LicenseError):
            response = self._page("许可证验证失败，请确认授权码完整、未过期，且适用于当前产品。", root_path=root_path)
            response.status_code = 400
            return response
        return Response(status=303, headers={"Location": root_path + "/_license/", "Cache-Control": "no-store"})

    def __call__(self, environ, start_response):
        request = Request(environ)
        if request.path == "/_license" or request.path.startswith("/_license/"):
            return self._portal(request)(environ, start_response)
        if request.method in {"GET", "HEAD"} and request.path in self.exempt_paths:
            return self.application(environ, start_response)
        try:
            runtime.require_valid()
        except runtime.LicenseError as exc:
            response = self._response({"error": "当前产品未获得有效授权", "code": str(exc)}, 403)
            return response(environ, start_response)
        return self.application(environ, start_response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    install = sub.add_parser("install")
    install.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "install":
            with args.path.open("rb") as handle:
                state = runtime.install_license(handle.read(65537))
        else:
            state = runtime.status()
        print(json.dumps(state, ensure_ascii=False))
    except (OSError, ValueError, runtime.LicenseError) as exc:
        parser.exit(1, f"AppGuard: {exc}\n")


if __name__ == "__main__":
    main()
