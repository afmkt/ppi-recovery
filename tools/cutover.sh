#!/usr/bin/env bash
#
# cutover.sh — safe, testable SQLite cutover with automatic rollback.
#
# What it does
#   1. Pre-flight: verify the source DB (integrity + foreign keys) BEFORE touching
#      the live DB. A bad source aborts here — nothing is changed.
#   2. Stop the service so nothing holds the DB.
#   3. Back up the live DB (timestamped) — this is the rollback point.
#   4. Swap in the source DB atomically (copy to .new, then rename).
#   5. Verify the swapped DB (integrity + FK) BEFORE starting the service.
#   6. Start the service (builds the image) and poll /healthz until the worker
#      has persisted a cycle (200). A wedged/unhealthy service rolls back.
#   7. Confirm the live DB is in WAL mode.
#
#   Any failure AFTER the backup (verify, health, WAL) restores the live DB from
#   the backup and restarts the service, then exits non-zero.
#
# Everything is parameterised by env vars so the same script runs in production
# (against the real container) or in dev (against a test DB + a dummy service).
#
# Config (all optional; sensible prod defaults in brackets)
#   DB_PATH        live DB path                     [data/newboom.db]
#   SOURCE_DB      DB to swap in; empty = keep live []
#   BACKUP_DIR     where the backup goes            [data/_backup]
#   HEALTH_URL     health endpoint                  [http://127.0.0.1:8000/healthz]
#   HEALTH_TIMEOUT seconds to wait for a 200        [180]
#   COMPOSE_FILE   compose project file             [compose.yml]
#   SERVICE        compose service name             [newboom]
#   PY             python interpreter               [.venv/bin/python]
#   STOP_CMD       override stop command            [docker compose stop SERVICE]
#   START_CMD      override start command           [docker compose up -d --build SERVICE]
#   DRY_RUN        1 = print the plan, change nothing [0]
#
# Examples
#   # Recovery cutover: swap in the recovered DB and deploy the new WAL image.
#   # The script stops the service, backs up, swaps, redeploys, and health-checks.
#   SOURCE_DB=/path/to/recovered.db bash tools/cutover.sh
#
#   # Plain restart cutover (keep the live DB, just redeploy the new image)
#   bash tools/cutover.sh
#
#   # Dev test — see tools/cutover_test.py (4 paths, all with injected no-op services)
#   .venv/bin/python tools/cutover_test.py
#
set -euo pipefail

DB_PATH="${DB_PATH:-data/newboom.db}"
SOURCE_DB="${SOURCE_DB:-}"
BACKUP_DIR="${BACKUP_DIR:-data/_backup}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/healthz}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.yml}"
SERVICE="${SERVICE:-newboom}"
PY="${PY:-.venv/bin/python}"
DRY_RUN="${DRY_RUN:-0}"
STOP_CMD="${STOP_CMD:-docker compose -f ${COMPOSE_FILE} stop ${SERVICE}}"
START_CMD="${START_CMD:-docker compose -f ${COMPOSE_FILE} up -d --build ${SERVICE}}"

stamp() { date -u +%Y%m%dT%H%M%SZ; }
log() { printf '[cutover %s] %s\n' "$(stamp)" "$*"; }

# Run a Python snippet, passing the DB path as argv[1].
check_db() {
  "$PY" - "$1" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    sys.exit("integrity_check FAILED on " + sys.argv[1])
viol = con.execute("PRAGMA foreign_key_check").fetchall()
if viol:
    sys.exit(f"foreign_key_check FAILED on {sys.argv[1]}: {len(viol)} violation(s)")
con.close()
PY
}

# Confirm the live DB is in WAL mode.
check_wal() {
  "$PY" - "$1" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
mode = con.execute("PRAGMA journal_mode").fetchone()[0]
con.close()
if mode != "wal":
    sys.exit(f"journal_mode is '{mode}', expected 'wal'")
PY
}

# Poll the health endpoint until it returns 200 (or the timeout elapses).
wait_health() {
  local waited=0
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    if "$PY" - "$HEALTH_URL" <<'PY'
import sys, http.client, urllib.parse
url = sys.argv[1]
try:
    p = urllib.parse.urlparse(url)
    c = http.client.HTTPConnection(p.hostname or "127.0.0.1", p.port or 8000, timeout=5)
    c.request("GET", p.path or "/healthz")
    status = c.getresponse().status
except Exception:
    sys.exit(1)
sys.exit(0 if status == 200 else 1)
PY
    then
      log "health 200 after ${waited}s"
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  log "health check did NOT reach 200 within ${HEALTH_TIMEOUT}s"
  return 1
}

# ---- Pre-flight: a bad source must not touch the live DB -------------------
if [ -n "$SOURCE_DB" ]; then
  if [ ! -f "$SOURCE_DB" ]; then
    log "ABORT: SOURCE_DB not found: $SOURCE_DB"; exit 1
  fi
  log "Pre-flight: verifying SOURCE_DB integrity + FK"
  check_db "$SOURCE_DB" || { log "ABORT: SOURCE_DB failed verification"; exit 1; }
fi

if [ "$DRY_RUN" = 1 ]; then
  log "DRY_RUN: would stop '${SERVICE}', back up ${DB_PATH} -> ${BACKUP_DIR}/<ts>,"
  log "DRY_RUN: would ${SOURCE_DB:+swap in ${SOURCE_DB} }then start + health-check ${HEALTH_URL}"
  exit 0
fi

# ---- Rollback point --------------------------------------------------------
STAMP="$(stamp)"
BACKUP_PATH="${BACKUP_DIR}/${STAMP}"
ROLLBACK=1
trap '
  if [ "${ROLLBACK}" = 1 ]; then
    log "FAILURE after backup: rolling back to ${BACKUP_PATH}"
    ${STOP_CMD} || log "rollback: stop_service failed (check the service manually)"
    if [ -f "${BACKUP_PATH}/newboom.db" ]; then
      cp -f "${BACKUP_PATH}/newboom.db" "${DB_PATH}"
      log "rollback: live DB restored from backup"
    else
      log "rollback: no backup file to restore"
    fi
    ${START_CMD} || log "rollback: start_service failed (check the service manually)"
    log "ROLLBACK complete — service should be back on the previous DB"
  fi
' EXIT

# ---- 1. Stop the service so nothing holds the DB ---------------------------
log "Stopping service"
${STOP_CMD}

# ---- 2. Back up the live DB (the rollback point) --------------------------
log "Backing up live DB -> ${BACKUP_PATH}/newboom.db"
mkdir -p "$BACKUP_PATH" "$(dirname "$DB_PATH")"
cp -f "$DB_PATH" "${BACKUP_PATH}/newboom.db"
# Sidecars are transient (regenerated on open); back them up only if present.
for side in wal shm; do
  [ -f "${DB_PATH}-${side}" ] && cp -f "${DB_PATH}-${side}" "${BACKUP_PATH}/" || true
done
log "backup checksum: $(sha256sum "${BACKUP_PATH}/newboom.db" 2>/dev/null | cut -d' ' -f1 || shasum -a256 "${BACKUP_PATH}/newboom.db" 2>/dev/null | cut -d' ' -f1 || echo n/a)"

# ---- 3. Swap in the source DB atomically ----------------------------------
if [ -n "$SOURCE_DB" ]; then
  log "Swapping in SOURCE_DB (atomic rename)"
  cp -f "$SOURCE_DB" "${DB_PATH}.new"
  mv -f "${DB_PATH}.new" "$DB_PATH"
fi

# ---- 4. Verify the swapped DB BEFORE starting the service ------------------
log "Verifying swapped DB integrity + FK"
check_db "$DB_PATH" || exit 1   # -> rollback

# ---- 5. Start the service + wait for a healthy worker ----------------------
log "Starting service (builds image)"
${START_CMD}

log "Polling ${HEALTH_URL} (timeout ${HEALTH_TIMEOUT}s)"
wait_health || exit 1           # -> rollback

# ---- 6. Confirm WAL mode ---------------------------------------------------
log "Confirming WAL mode on live DB"
check_wal "$DB_PATH" || exit 1  # -> rollback

# ---- Success ---------------------------------------------------------------
ROLLBACK=0
log "CUTOVER SUCCESS"
log "Live backup at: ${BACKUP_PATH}/newboom.db"
