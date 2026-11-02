"""Mount the marimo dashboard (``src/dashboard/vis.py``) into the FastAPI app.

Marimo is served as a Starlette sub-application built via the public
``create_asgi_app`` API. Two subtleties:

* A Starlette ``Mount`` does **not** run the sub-app's own lifespan, so
  ``dashboard_lifespan`` enters the sub-app's runtime lifespan (the kernel and
  session manager) from the host app's lifespan.
* The sub-app is extracted from the built builder and mounted directly at
  ``/dashboard`` so the host's other routes (``/healthz``, ``/api/...``) are
  untouched.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from marimo._server.asgi import create_asgi_app
from starlette.applications import Starlette

# vis.py lives one level up from this package (src/dashboard/vis.py).
DASHBOARD_ROOT = str(Path(__file__).resolve().parents[1] / "dashboard" / "vis.py")

_dashboard_app: Starlette | None = None


def build_dashboard() -> Starlette:
    """Build (once) the standalone marimo dashboard sub-app, unmounted."""
    global _dashboard_app
    if _dashboard_app is None:
        # `build()` is typed to return `ASGIApp` (a bare callable), but it
        # actually returns the `Starlette` base app the builder wraps its
        # sub-apps in, which is what exposes `.routes`.
        built = cast(
            Starlette,
            create_asgi_app(quiet=True)
            .with_app(path="/dashboard", root=DASHBOARD_ROOT)
            .build(),
        )
        for route in built.routes:
            sub = getattr(route, "app", None)
            if isinstance(sub, Starlette):
                # The per-file sub-app's runtime lifespan reads a couple of
                # state attributes the standalone server sets on its parent app.
                # When mounted inside another app they aren't inherited, so set
                # them here: `quiet` (suppress console) and `server` (only the
                # standalone uvicorn server owns it; None skips its teardown).
                sub.state.quiet = True
                sub.state.server = None
                _dashboard_app = sub
                break
        if _dashboard_app is None:
            raise RuntimeError("marimo dashboard sub-app not found")
    return _dashboard_app


def mount_dashboard(app: FastAPI) -> Starlette:
    """Attach the dashboard at ``/dashboard`` (with a ``/`` -> ``/``/ redirect)."""
    sub = build_dashboard()
    app.mount("/dashboard", sub)
    app.add_route(
        "/dashboard",
        lambda _: RedirectResponse(url="/dashboard/", status_code=307),
    )
    return sub


@asynccontextmanager
async def dashboard_lifespan(app: FastAPI):
    """Enter the dashboard sub-app's runtime lifespan (kernel + sessions)."""
    sub = build_dashboard()
    async with sub.router.lifespan_context(sub):
        yield
