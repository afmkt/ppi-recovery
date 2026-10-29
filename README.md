# newboom

FastAPI ingestion server (Python / uv). See `compose.yml` for the local
run recipe.

## Docker

The image is built on `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`
(matching `compose.yml`), runs as a non-root user, and serves on `0.0.0.0:8000`.
The SQLite DB and uploads default to `/app/data` (a volume); override with
`DATABASE_URL` / `UPLOAD_DIR`.

```bash
# local, native arch (Apple Silicon -> arm64, x64 -> amd64)
docker build -t newboom .

# run
docker run -p 8000:8000 -v newboom-data:/app/data newboom
```

## CI / release

`.github/workflows/release-docker.yml` builds a **multi-arch** image
(linux/amd64 + linux/arm64, each native, merged into one manifest) and pushes to:

- **GitHub Container Registry** — `ghcr.io/afmkt/ppi-recovery`
  (authenticates with the built-in `GITHUB_TOKEN`; the package is created
  **private** on first push — make it public once at
  `github.com/users/afmkt/packages/container/ppi-recovery/settings`, or
  `gh package edit ppi-recovery --visibility public`).
- **Aliyun ACR** — `crpi-zuhwpd6fwca3b0fc.cn-shanghai.personal.cr.aliyuncs.com/afmkt/ppi-recovery`
  (needs repo secrets `ALIYUN_USERNAME` / `ALIYUN_PASSWORD`).

Triggers: version tags (`v*` → also tags the version) and `workflow_dispatch`
(`latest` + short-SHA only). The amd64 image is smoke-tested against
`/openapi.json` **before** user-visible tags are attached.
