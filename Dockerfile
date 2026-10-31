# newboom — FastAPI ingestion server.
#
# Built on the same uv image the local compose.yml uses. The release workflow
# (.github/workflows/release-docker.yml) runs one NATIVE build per arch
# (linux/amd64 on a x64 runner, linux/arm64 on an arm runner) and merges them
# into a single multi-arch manifest — so this Dockerfile never pins a platform;
# `docker build .` on Apple Silicon yields a native arm64 image, on x64 a native
# amd64 image.
#
# Base is the uv image from compose.yml. A tag pin is fine for this small app;
# freeze the base by digest (M7-style) only if silent toolchain drift matters:
#   https://github.com/astral-sh/uv/pkgs/container/uv -> python3.12-bookworm-slim
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
# COPY-mode links keep the venv hermetic across layers instead of hardlinking
# build artifacts that outlive the layer they were created in.
ENV UV_LINK_MODE=copy

# ── 1. Dependency closure only (cached) ──────────────────────────────────
# Install deps from the lockfile WITHOUT building the project, so this layer
# rebuilds only when pyproject.toml / uv.lock change, not on every source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ── 2. Project sources ───────────────────────────────────────────────────
# Only the newboom package installs now (deps are cached from step 1).
COPY src/ ./src/
COPY README.md ./
RUN uv sync --frozen --no-dev

# ── 3. Runtime ───────────────────────────────────────────────────────────
# DB and uploads live under /app/data (a volume). Absolute env defaults make the
# image behave the same regardless of how the caller mounts it. load_dotenv() in
# the app is a no-op in the image (no .env is copied), so these win.
ENV DATABASE_URL=sqlite:////app/data/newboom.db \
    UPLOAD_DIR=/app/data/uploads

# Non-root: the venv + sources stay root-owned but read-only at runtime; only
# /app/data is writable and is chowned to the app user so the first (empty)
# volume inherits UID 10001 ownership. The chown lives in this cached layer so
# it only re-runs when the project layer changes.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --shell /usr/sbin/nologin --no-create-home app \
    && mkdir -p /app/data \
    && chown -R app:app /app

VOLUME ["/app/data"]

# Image-level healthcheck so a plain `docker run` detects a wedged worker the
# way compose does. /healthz reflects the background worker: 200 while a cycle
# has succeeded within its stale window, 503 otherwise — so a worker that can't
# persist (e.g. a read-only database) flips the container to "unhealthy" instead
# of looping forever. Stdlib only: no curl needed.
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python3 -c "import http.client,sys; c=http.client.HTTPConnection('127.0.0.1',8000,timeout=4); c.request('GET','/healthz'); sys.exit(0 if c.getresponse().status==200 else 1)" || exit 1

USER app

# Run the pre-built venv's uvicorn directly. The environment is fully built at
# image time (uv sync --frozen --no-dev), so invoking the binary skips uv's
# runtime sync — no building / downloading / uninstalling when the container
# starts. (This is what `uv run` would otherwise re-do on every start.)
CMD ["/app/.venv/bin/uvicorn", "newboom.api:app", "--host", "0.0.0.0", "--port", "8000"]
