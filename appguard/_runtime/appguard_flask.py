"""Explicit Flask extension. Does not create, import or run the business app."""

from appguard_host import LicenseMiddleware


class AppGuard:
    def __init__(self, app=None, *, exempt_paths=()):
        self.exempt_paths = exempt_paths
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        if "appguard" in app.extensions:
            return
        middleware = LicenseMiddleware(app.wsgi_app, exempt_paths=self.exempt_paths)
        app.wsgi_app = middleware
        app.extensions["appguard"] = self
