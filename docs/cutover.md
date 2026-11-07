# SQLite cutover with rollback

A safe, testable procedure to swap the live `newboom.db` (and redeploy the WAL
code) without data loss. Backs up the live DB before touching it, verifies the
replacement *before* the service starts, and **auto-rolls back** on any failure
after the backup.

Tooling:

- `tools/cutover.sh` — the procedure (stops → backs up → verifies → swaps →
  redeploys → health-checks → confirms WAL; rolls back on failure).
- `tools/cutover_test.py` — dev test, 4 paths (happy / health-fail rollback /
  corrupt-source abort / non-WAL rollback), all with injected no-op services so
  no real container is touched. Run it: `.venv/bin/python tools/cutover_test.py`.

## What `cutover.sh` does

1. **Pre-flight** — verify the source DB (`PRAGMA integrity_check` +
   `foreign_key_check`). A bad source aborts *before* anything changes.
2. **Stop** the service so nothing holds the DB.
3. **Back up** the live DB to `BACKUP_DIR/<timestamp>/newboom.db` — the rollback
   point. Sidecars (`-wal`/`-shm`) are copied if present.
4. **Swap** the source in atomically (copy to `.new`, then rename).
5. **Verify** the swapped DB (integrity + FK) *before* starting the service.
6. **Start** the service (`up -d --build`, which ships the new WAL code) and
   poll `/healthz` until the worker has persisted a cycle (200).
7. **Confirm** the live DB is in WAL mode.

Any failure after step 3 restores the live DB from the backup, restarts the
service, and exits non-zero.

### Config (all optional; prod defaults in brackets)

| Env | Default | Meaning |
|---|---|---|
| `DB_PATH` | `data/newboom.db` | live DB (host side of the `./data` bind mount) |
| `SOURCE_DB` | *(empty = keep live)* | DB to swap in |
| `BACKUP_DIR` | `data/_backup` | where the backup goes |
| `HEALTH_URL` | `http://127.0.0.1:8000/healthz` | health endpoint |
| `HEALTH_TIMEOUT` | `180` | seconds to wait for a 200 |
| `COMPOSE_FILE` / `SERVICE` | `compose.yml` / `newboom` | service control |
| `PY` | `.venv/bin/python` | interpreter for the DB checks |
| `STOP_CMD` / `START_CMD` | `docker compose … stop/up -d --build` | overrides (used by the dev test) |
| `DRY_RUN` | `0` | `1` = print the plan, change nothing |

## Runbook (recovery cutover)

This assumes the recovered DB exists and the new WAL code is in the working tree.

### 1. Prep — get the recovered DB to a stable spot

`/tmp` is ephemeral, so copy the recovered DB out first (to somewhere the
cutover can reach, on the host that runs it):

```sh
cp /tmp/nbr/newboom.final.db <stable>/newboom.recovered.db
```

Confirm the recovered DB is settled (no stale `-wal`/`-shm`, integrity ok, 0 FK
violations) and that the new code is what you want to ship (`git diff`; the
cutover's `--build` will deploy it):

```sh
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("<stable>/newboom.recovered.db")
print(c.execute("PRAGMA integrity_check").fetchone()[0])
print("fk violations:", len(c.execute("PRAGMA foreign_key_check").fetchall()))
print("ppi rows:", c.execute("SELECT count(*) FROM ppi").fetchone()[0])
PY
```

### 2. Dry run — prints the plan, changes nothing

```sh
SOURCE_DB=<stable>/newboom.recovered.db DRY_RUN=1 bash tools/cutover.sh
```

### 3. Cutover

```sh
SOURCE_DB=<stable>/newboom.recovered.db bash tools/cutover.sh
```

### 4. Verify

```sh
docker compose ps                       # newboom: Up (healthy)
docker compose logs newboom | tail      # a cycle completed, no "database is locked"
# the old (corrupt) DB is preserved at: data/_backup/<timestamp>/newboom.db
```

### 5. Manual rollback (only if the auto-rollback did not fire)

```sh
docker compose stop newboom
cp data/_backup/<timestamp>/newboom.db data/newboom.db
docker compose up -d
```

### 6. Cleanup

Once the new system has been healthy for a while, drop the old backup:

```sh
rm -rf data/_backup/<timestamp>
```

## Notes

- `START_CMD`'s default is `docker compose up -d --build` — the `--build` ships
  the new WAL code. Omit `--build` for a DB-only swap when the image is current.
- `DB_PATH` defaults to `data/newboom.db`, the host side of the `./data` mount;
  the container sees the same file at `/app/data/newboom.db`.
- `HEALTH_TIMEOUT` is 180s; the worker's stale window is 30s (`WORKER_STALE_S`),
  so a first healthy cycle lands well inside it.
- WAL requires a local, mmap-capable filesystem (not NFS/SMB).
- A plain restart cutover (keep the live DB, just redeploy the image) is
  `bash tools/cutover.sh` with no `SOURCE_DB`.
