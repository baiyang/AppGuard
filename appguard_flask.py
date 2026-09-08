"""Explicit Flask extension. Does not create, import or run the business app."""

from appguard_host import LicenseMiddleware


class AppGuard:
    def __init__(self, app=None):
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        if "appguard" in app.extensions:
            return
        middleware = LicenseMiddleware(app.wsgi_app)
        app.wsgi_app = middleware
        app.extensions["appguard"] = self
