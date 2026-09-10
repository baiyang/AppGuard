"""Explicit FastAPI registration and ASGI license middleware."""

from io import BytesIO

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from werkzeug.wrappers import Request as WSGIRequest

import guard_runtime as runtime
from appguard_host import LicenseMiddleware as WSGILicenseMiddleware, _validate_exempt_paths


class LicenseMiddleware:
    """Check HTTP requests and WebSocket handshakes without buffering business traffic."""

    def __init__(self, app, *, exempt_paths=()):
        self.app = app
        self._host = WSGILicenseMiddleware(None, exempt_paths=exempt_paths)
        self.exempt_paths = self._host.exempt_paths

    async def _respond(self, response, scope, receive, send):
        # Preserve the shared portal's cookies, redirects and security headers.
        result = Response(response.get_data(), status_code=response.status_code)
        result.raw_headers = [(name.lower().encode("latin-1"), value.encode("latin-1"))
                              for name, value in response.get_wsgi_headers({})]
        if scope["method"] == "HEAD":
            result.body = b""
        try:
            await result(scope, receive, send)
        finally:
            response.close()

    async def _portal(self, scope, receive, send, path, root_path):
        body = bytearray()
        if path == "/_license/activate" and scope["method"] == "POST":
            try:
                async for chunk in Request(scope, receive).stream():
                    # Bound chunked uploads before passing them to the shared form parser.
                    if len(body) + len(chunk) > 100_000:
                        response = self._host._response({"error": "许可证文件过大"}, 413)
                        await self._respond(response, scope, receive, send)
                        return
                    body.extend(chunk)
            except ClientDisconnect:
                return
        headers = Headers(scope=scope)
        client = scope.get("client")
        # The portal needs only the request path, form, cookie and client address.
        # Use the actual buffered length, including when Transfer-Encoding is chunked.
        request = WSGIRequest({
            "REQUEST_METHOD": scope["method"],
            "PATH_INFO": path.encode("utf-8").decode("latin-1"),
            "CONTENT_TYPE": headers.get("content-type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "HTTP_COOKIE": "; ".join(headers.getlist("cookie")),
            "REMOTE_ADDR": client[0] if client else None,
            "wsgi.input": BytesIO(body),
        })
        try:
            response = await run_in_threadpool(self._host._portal, request, root_path=root_path)
        finally:
            request.close()
        await self._respond(response, scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        root_path = scope.get("root_path", "").rstrip("/")
        if root_path and (path == root_path or path.startswith(root_path + "/")):
            path = path[len(root_path):]
        if scope["type"] == "http":
            if path == "/_license" or path.startswith("/_license/"):
                await self._portal(scope, receive, send, path, root_path)
                return
            if scope["method"] in {"GET", "HEAD"} and path in self.exempt_paths:
                await self.app(scope, receive, send)
                return

        try:
            await run_in_threadpool(runtime.require_valid)
        except runtime.LicenseError as exc:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                response = self._host._response({"error": "当前产品未获得有效授权", "code": str(exc)}, 403)
                await self._respond(response, scope, receive, send)
            return
        await self.app(scope, receive, send)


class AppGuard:
    """Register once per app, after other middleware and before startup."""

    def __init__(self, app=None, *, exempt_paths=()):
        self.exempt_paths = _validate_exempt_paths(exempt_paths)
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        if hasattr(app.state, "appguard"):
            return
        app.add_middleware(LicenseMiddleware, exempt_paths=self.exempt_paths)
        app.state.appguard = self
