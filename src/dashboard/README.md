Accessed at **`/dashboard/`** when the API runs (`uvicorn newboom.api:app` /
`newboom` console script) — the marimo sub-app is mounted into the FastAPI app by
`newboom.dashboard`, alongside the API and the ingestion worker. It reads
`data/newboom.db` read-only, so it works both live and from a historical DB.

1. Ingestion Health & Pipeline Throughput
2. Aggregation Pipeline Monitoring
3. Data Quality & Statistical Sanity
