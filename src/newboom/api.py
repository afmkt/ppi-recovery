from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from newboom.boom import router as boom_router
from newboom.schema import ApiResponse
from newboom.upload import router as upload_router
from newboom.worker import lifespan, worker_health

# --- FastAPI App Initialization ---
app = FastAPI(title="Device Ingestion API", version="1.0.0", lifespan=lifespan)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    res = ApiResponse(ok=False, message=str(exc))
    return JSONResponse(
        status_code=500,
        content=res.model_dump(),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # If detail is already a string, use it as the message
    msg = exc.detail if isinstance(exc.detail, str) else str(exc.detail)

    response_data = ApiResponse(ok=False, message=msg)
    return JSONResponse(
        status_code=exc.status_code,
        content=response_data.model_dump(),
    )


# Enable CORS for your Vite React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Database schema creation moved to worker.lifespan (a custom lifespan=
# replaces Starlette's default startup, so @app.on_event handlers never ran).

app.include_router(upload_router, prefix="/api/upload", tags=["Upload"])
app.include_router(boom_router)


@app.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    """Worker liveness + health.

    Returns 200 while a background worker cycle has succeeded within the stale
    window and 503 (carrying the last error) otherwise, so a persistently
    failing worker surfaces via the container health check instead of looping
    forever while silently failing to persist. Deliberately out of the public
    OpenAPI schema (``include_in_schema=False``).
    """
    healthy = worker_health.is_healthy()
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"ok": healthy, "worker": worker_health.snapshot()},
    )


def main() -> None:
    """Console-script entry point: launch the FastAPI app with uvicorn.

    The application object lives on this module so the installed console
    script (``newboom``) and the top-level ``main.py`` route through here.
    Host and port come from the ``HOST`` / ``PORT`` environment variables
    (defaults ``0.0.0.0`` : ``8000``).
    """
    import os

    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
