# newboom SQLite corruption — recovery & hardening runbook

The database is a single SQLite file `data/newboom.db` (WAL mode, ~190 MB, ~1.2 M
`ppi` rows), mounted into the `newboom` container at `/app/data` via a bind mount
(`compose.yml`: `./data:/app/data`). The app is a FastAPI server + a background
worker that upserts per-second PPI and recomputes summaries every cycle.

Symptom of corruption (this incident, 2026-11-07): the server stays "healthy"
(`/healthz` → 200) but every query touching `ppi` fails with

```
sqlite3.DatabaseError: database disk image is malformed
```

because the **`ppi` PRIMARY-KEY auto-index** (`sqlite_autoindex_ppi_1`) — the
most frequently written b-tree — was damaged in its tail pages. All other tables
and the `ppi` *data* pages were intact.

---

## 1. Diagnose (never touch the live file in place)

Work on a copy so the running writer can't change the picture:

```bash
cd newboom
docker stop newboom                       # freeze the writer; WAL is checkpointed on clean stop
cp data/newboom.db /tmp/live.db           # copy first, never operate on the live file

sqlite3 /tmp/live.db "PRAGMA integrity_check;"   # list corrupt pages/trees
sqlite3 /tmp/live.db "SELECT rowid,time FROM ppi LIMIT 5;"   # is the data b-tree readable?
```

Read the `integrity_check` output: `Tree N` is the N-th b-tree; map it with
`SELECT rowid,type,name,tbl_name FROM sqlite_master ORDER BY rowid;`. If only one
tree (an index) is bad and the table's data pages are readable, damage is
localized and fully recoverable.

## 2. Recover with `.recover` (the canonical salvage tool)

`.recover` re-reads every page and rebuilds a valid database from everything
survivable — including partial rows inside damaged pages. It cannot `VACUUM` a
corrupt file, but it can dump and reload it:

```bash
sqlite3 /tmp/live.db ".recover" > /tmp/recovered.sql      # ~1-5 s for 190 MB
sqlite3 /tmp/recovered.db < /tmp/recovered.sql            # build the clean DB
sqlite3 /tmp/recovered.db "PRAGMA integrity_check;"       # must print: ok
```

Verify before trusting it: every table present with sane counts, the previously
failing query now runs, and the time range covers the full history.

## 3. Swap in and restart

Preserve the corrupt originals, then install the recovered file (the recovered
DB is a clean, compacted image — smaller than the corrupt one):

```bash
cd data
cp -p newboom.db newboom.db.corrupt-<stamp>-live.bak     # keep the evidence
cp -p /tmp/recovered.db newboom.db
sqlite3 newboom.db "PRAGMA integrity_check;"              # ok
cd .. && docker start newboom
docker logs --since 1m newboom | grep -i malformed        # expect nothing
curl -s "http://127.0.0.1:8000/devices/<MAC>/ppi?limit=5"  # returns rows
```

## 4. Fill any data holes

On auto-recovery the gate already re-ingests every ppi/sleep upload in
`data/uploads/` newer than the restored backup (see the gate bullet above), so the
ppi/sleep gap is closed automatically. Two things it does **not** cover:

- **db-only tables** — `conversation` / `message` / `questionnaireanswer` have no
  durable upload copy; they stay as-of the backup (up to a day, with the daily
  backup). Only those rows in the gap are lost.
- **`staging/*.ppi` device backlog** — not yet ingested; re-ingest it through the
  normal upsert path (idempotent on `(device_id, time)`).

## 5. Prevent recurrence / recover fast next time

Root-cause candidates for a corrupted tail b-tree:

1. **WAL on a non-mmap-capable FS.** `database.py` warns the DB "requires a local,
   mmap-capable filesystem (not NFS/SMB)." This runs on a **macOS Docker Desktop
   bind mount** (`./data`), whose virtualized FS is the prime suspect. Fix: back
   the data dir with a **Docker named volume** (stored in the Linux VM, mmap-safe)
   instead of a bind mount, or drop WAL (`journal_mode=DELETE`) at the cost of
   write concurrency.
2. **Unclean stop during a write** (SIGKILL / host crash / disk full) with
   `synchronous=NORMAL`. A clean `docker stop` checkpoints the WAL (the worker
   runs `engine.dispose()`), so always stop the container gracefully.
3. **No integrity check at startup.** `create_db_and_tables()` only
   `create_all` + migrates; a corrupt DB starts silently and fails on first query.

Recommended hardening:

- **`synchronous=FULL` — APPLIED (2026-11-07).** The connect listener in
  `database.py` now sets `PRAGMA synchronous=FULL` (fsync after every commit),
  the strongest crash-safety level. Cost: write throughput. Kept WAL mode
  (`journal_mode=wal`). The image was rebuilt and the container recreated with it.
- **Startup integrity gate — APPLIED (2026-11-07).** `create_db_and_tables()` now
  runs `ensure_db_integrity()` first: it `PRAGMA quick_check`s the db on boot and,
  if corrupt, atomically restores the newest valid `*.bak` from `BACKUP_DIR`
  (default `<db_dir>/backups`), re-verifies, then **re-ingests every ppi/sleep
  upload in `data/uploads/` newer than that backup** (`reingest_uploads_after`,
  idempotent upsert on `(device_id, time)`) so the gap between the backup and the
  corruption is closed, and only then creates tables. No valid backup → it raises
  and refuses to start (never serves bad data). A malformed upload file is logged
  and skipped, never aborting the recovery of the rest. Env: `DB_INTEGRITY_CHECK`
  (off to disable), `INTEGRITY_CHECK_MODE` (`quick` default / `full`),
  `BACKUP_DIR`. Covered by `tests/test_db_integrity.py` and proven in the running
  image (a corrupted copy auto-restored to `ok` with all rows intact).
- **Bind mount — KEPT by decision.** Production runs on a Linux host, where a
  bind mount to a local path is mmap-capable and safe; the corruption was a
  macOS‑Docker‑Desktop (virtualized‑FS) artifact, mitigated locally by
  `synchronous=FULL`. No named volume.
- **Daily backup — APPLIED (2026-11-07).** `tools/backup_newboom.sh` runs
  **every day at 03:00** via the launchd agent `com.newboom.dbbackup`
  (`~/Library/LaunchAgents/com.newboom.dbbackup.plist`, template in `tools/`).
  It does an online `.backup` (non-disruptive, no stop/checkpoint), verifies each
  copy with `integrity_check`, and a corrupt source is discarded so a good backup
  is never overwritten. Keeps 14 generations (~2 weeks). Log:
  `data/backups/backup.log`. Manage: `launchctl kickstart -k gui/$(id -u)/com.newboom.dbbackup`
  (run now), `launchctl bootout gui/$(id -u)/com.newboom.dbbackup` (stop).
  The startup gate restores from these, so a good recent backup is what makes
  auto-recovery lossless.
- **Graceful stop** (already done by the worker) and watch disk space so the
  volume never fills mid-write.

## Files after the 2026-11-07 incident

```
data/newboom.db                                  recovered, live, integrity ok,
                                                 journal_mode=wal, synchronous=FULL
data/backups/newboom.db.<stamp>.bak              daily online backups (14 kept);
                                                 newest is the recover-from point
data/backups/backup.log                          launchd agent output
data/newboom.db.corrupt-<stamp>.bak             corrupt snapshots, kept as evidence
tools/backup_newboom.sh                          daily backup helper (non-disruptive)
tools/com.newboom.dbbackup.plist                 launchd agent (installed to
~/Library/LaunchAgents/com.newboom.dbbackup.plist; runs daily 03:00)
docs/db-recovery-runbook.md                      this file
```
