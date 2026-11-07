"""Dev test for tools/cutover.sh — exercises 4 paths against fixtures.

No real container is touched: STOP_CMD/START_CMD are injected as no-ops (or a
WAL-setting stub), the health endpoint is a local http.server, and the DBs are
small SQLite files. Each path runs in its own temp workdir and verifies the
observable outcome.

  1. happy path        swap good DB + healthy service  -> CUTOVER SUCCESS, WAL
  2. health fails       service never goes healthy      -> rollback restores DB
  3. pre-flight abort   source DB is corrupt            -> no change, no backup
  4. WAL gate           source not in WAL mode          -> check_wal fails, rollback
"""
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import http.server

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CUTOVER = os.path.join(HERE, "cutover.sh")
PY = sys.executable  # the interpreter running the tests (has sqlite3)

FAIL = []


def make_db(path, n_rows, wal=False, fk_violation=False):
    """A valid 2-table DB (parent `p`, child `c` with a FK to p)."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE p (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("CREATE TABLE c (id INTEGER PRIMARY KEY, pid INTEGER, "
                "FOREIGN KEY (pid) REFERENCES p(id))")
    for i in range(n_rows):
        con.execute("INSERT INTO p (id, v) VALUES (?, ?)", (i, f"row{i}"))
        con.execute("INSERT INTO c (id, pid) VALUES (?, ?)", (i, i))
    if fk_violation:
        # A child pointing at a parent that does not exist -> foreign_key_check fails.
        con.execute("INSERT INTO c (id, pid) VALUES (?, ?)", (n_rows, n_rows + 999))
    con.commit()
    if wal:
        con.execute("PRAGMA journal_mode=wal")
    con.close()
    if wal:
        # Re-open so the WAL header flag is flushed with no live -wal/-shm sidecar.
        c = sqlite3.connect(path)
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        c.close()


def sha(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def backup_file(backup_dir):
    """The cutover writes to BACKUP_DIR/<timestamp>/newboom.db; return it."""
    import glob
    found = glob.glob(f"{backup_dir}/*/newboom.db")
    return found[0] if found else None


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Health200(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def run_cutover(env, health_on=True, timeout=60):
    e = os.environ.copy()
    e.update(env)
    srv = None
    if health_on:
        import urllib.parse
        port = int(urllib.parse.urlparse(e["HEALTH_URL"]).port or 8000)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Health200)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        p = subprocess.run(["bash", CUTOVER], env=e, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    finally:
        if srv:
            srv.shutdown()


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def base_env(workdir, db, source, backup, health_url, stop=":", start=":", timeout="180"):
    return dict(
        DB_PATH=db,
        SOURCE_DB=source,
        BACKUP_DIR=backup,
        HEALTH_URL=health_url,
        HEALTH_TIMEOUT=timeout,
        STOP_CMD=stop,
        START_CMD=start,
        PY=PY,
        DRY_RUN="0",
    )


# ---- Path 1: happy path ---------------------------------------------------
def test_happy():
    print("\n[1] happy path: swap good DB + healthy service")
    wd = f"/tmp/nbr/cut_happy"; shutil.rmtree(wd, ignore_errors=True); os.makedirs(wd)
    live, good, backup = f"{wd}/live.db", f"{wd}/good.db", f"{wd}/backup"
    make_db(live, 5)
    make_db(good, 50, wal=True)  # different content + already WAL mode
    orig = sha(live)
    port = free_port()
    rc, out = run_cutover(base_env(wd, live, good, backup, f"http://127.0.0.1:{port}/healthz"))
    check("exit 0", rc == 0, f"rc={rc}")
    bf = backup_file(backup)
    check("backup created", bf is not None)
    check("backup == original live DB", bool(bf) and sha(bf) == orig)
    check("live DB swapped to good", sha(live) == sha(good))
    check("WAL mode active on live DB",
          sqlite3.connect(live).execute("PRAGMA journal_mode").fetchone()[0] == "wal")
    check("log says CUTOVER SUCCESS", "CUTOVER SUCCESS" in out)


# ---- Path 2: health check fails -> rollback --------------------------------
def test_health_fail_rollback():
    print("\n[2] health fails: service never healthy -> rollback restores DB")
    wd = f"/tmp/nbr/cut_health"; shutil.rmtree(wd, ignore_errors=True); os.makedirs(wd)
    live, good, backup = f"{wd}/live.db", f"{wd}/good.db", f"{wd}/backup"
    make_db(live, 5)
    make_db(good, 50, wal=True)
    orig = sha(live)
    port = free_port()  # NO server started on this port -> health check fails
    rc, out = run_cutover(base_env(wd, live, good, backup, f"http://127.0.0.1:{port}/healthz",
                                   timeout="3"), health_on=False, timeout=30)
    check("exit non-zero", rc != 0, f"rc={rc}")
    check("rollback ran", "rolling back" in out)
    check("live DB restored to original", sha(live) == orig)
    check("backup was created (rollback point)", backup_file(backup) is not None)


# ---- Path 3: pre-flight abort (corrupt source) -----------------------------
def test_preflight_abort():
    print("\n[3] pre-flight abort: corrupt source -> no change, no backup")
    wd = f"/tmp/nbr/cut_preflight"; shutil.rmtree(wd, ignore_errors=True); os.makedirs(wd)
    live, bad, backup = f"{wd}/live.db", f"{wd}/bad.db", f"{wd}/backup"
    make_db(live, 5)
    make_db(bad, 5, fk_violation=True)  # foreign_key_check will fail
    orig = sha(live)
    rc, out = run_cutover(base_env(wd, live, bad, backup, "http://127.0.0.1:1/healthz",
                                   timeout="3"), health_on=False, timeout=30)
    check("exit non-zero", rc != 0, f"rc={rc}")
    check("aborted before backup (no backup dir)",
          not os.path.exists(backup) or not os.listdir(backup))
    check("live DB unchanged", sha(live) == orig)
    check("no rollback (nothing to roll back)", "rolling back" not in out)


# ---- Path 4: WAL gate — source not in WAL mode -> rollback -----------------
def test_wal_gate():
    print("\n[4] WAL gate: source not in WAL mode -> check_wal fails -> rollback")
    wd = f"/tmp/nbr/cut_wal"; shutil.rmtree(wd, ignore_errors=True); os.makedirs(wd)
    live, good, backup = f"{wd}/live.db", f"{wd}/good.db", f"{wd}/backup"
    make_db(live, 5, wal=True)   # live is WAL
    make_db(good, 50, wal=False)  # source is rollback-journal mode
    orig = sha(live)
    port = free_port()
    rc, out = run_cutover(base_env(wd, live, good, backup, f"http://127.0.0.1:{port}/healthz"),
                          health_on=True, timeout=30)
    check("exit non-zero", rc != 0, f"rc={rc}")
    check("check_wal reported non-wal", "expected 'wal'" in out or "journal_mode is" in out)
    check("rollback restored live DB", sha(live) == orig)


if __name__ == "__main__":
    os.makedirs("/tmp/nbr", exist_ok=True)
    test_happy()
    test_health_fail_rollback()
    test_preflight_abort()
    test_wal_gate()
    print()
    if FAIL:
        print(f"RESULT: {len(FAIL)} FAILED -> {FAIL}")
        sys.exit(1)
    print("RESULT: all 4 cutover paths PASSED")
