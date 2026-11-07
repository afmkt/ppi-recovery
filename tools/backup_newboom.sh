#!/usr/bin/env bash
#
# backup_newboom.sh — consistent, non-disruptive backup of the newboom SQLite DB.
#
# Runs on the HOST (the image has no cron). Uses SQLite's online backup API
# (via python3, which is always present) to make a crash-consistent copy of a
# live, WAL-mode database WITHOUT stopping the container or forcing a
# checkpoint. Keeps timestamped generations and verifies each with
# PRAGMA integrity_check, so a corrupt source never overwrites a good backup.
#
# Scheduled daily by the launchd agent com.newboom.dbbackup
# (~/Library/LaunchAgents/com.newboom.dbbackup.plist). Run it by hand any time:
#   ./tools/backup_newboom.sh
#
# Env: NEWBOOM_REPO / NEWBOOM_BACKUP_DIR / NEWBOOM_BACKUP_KEEP / PYTHON.
#
set -euo pipefail

# --- config ----------------------------------------------------------------
REPO="${NEWBOOM_REPO:-/Users/michael/code/python/newboom}"
DB="$REPO/data/newboom.db"
BKDIR="${NEWBOOM_BACKUP_DIR:-$REPO/data/backups}"
KEEP="${NEWBOOM_BACKUP_KEEP:-14}"          # daily cadence -> ~2 weeks
PYTHON="${PYTHON:-python3}"                # absolute path in the launchd agent
# ----------------------------------------------------------------------------

mkdir -p "$BKDIR"
stamp="$(date +%Y%m%d-%H%M%S)"
dest="$BKDIR/newboom.db.$stamp.bak"

if [[ ! -f "$DB" ]]; then
  echo "FATAL: source DB not found: $DB" >&2
  exit 1
fi

# Online backup + verify + count in one process. Crash-consistent even while the
# container writes (WAL); refuses to leave a bad backup behind (a corrupt source
# is detected and discarded, so a good backup is never overwritten).
"$PYTHON" - "$DB" "$dest" <<'PY'
import os
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
try:
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    s.backup(d)
    d.close()
    s.close()
except sqlite3.Error as e:  # online backup of a corrupt source can raise
    print(f"FATAL: online backup failed: {e}", file=sys.stderr)
    try:
        os.remove(dst)
    except OSError:
        pass
    sys.exit(1)

# Verify the copy before trusting it.
c = sqlite3.connect(dst)
row = c.execute("PRAGMA integrity_check").fetchone()[0]
c.close()
if str(row).strip().lower() != "ok":
    print(f"FATAL: backup failed integrity_check ({row!r}), discarded {dst}", file=sys.stderr)
    os.remove(dst)
    sys.exit(1)

try:
    c = sqlite3.connect(dst)
    n = c.execute("SELECT COUNT(*) FROM ppi").fetchone()[0]
    c.close()
except sqlite3.Error:
    n = "?"
print(f"backed up {src} -> {dst} (ppi={n})")
PY

# Prune to the KEEP most-recent generations. bash 3.2-safe (no GNU xargs -r).
while IFS= read -r old; do
  [ -n "$old" ] && rm -f "$old" || true
done < <(ls -1t "$BKDIR"/newboom.db.*.bak 2>/dev/null | tail -n +$((KEEP + 1)))
