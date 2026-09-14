from __future__ import annotations

from aef_terminal.ui import app as ui_app


def iter_app_routes(app=ui_app.app):
    for route in app.routes:
        if type(route).__name__ == "_IncludedRouter":
            yield from route.original_router.routes
            continue
        yield route


def app_route_paths(app=ui_app.app) -> list[str]:
    return [str(getattr(route, "path", "") or "") for route in iter_app_routes(app)]


def app_has_route(path: str, app=ui_app.app) -> bool:
    return path in app_route_paths(app)


def app_route_endpoint(path: str, method: str = "GET"):
    for route in iter_app_routes():
        if getattr(route, "path", "") != path:
            continue
        methods = getattr(route, "methods", None)
        if methods is None:
            if method.upper() == "GET":
                return route.endpoint
            continue
        if method.upper() in (methods or set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")
