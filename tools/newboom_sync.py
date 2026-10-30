#!/usr/bin/env python3
"""newboom-sync — transfer & import legacy ``boom_api`` PPI/Sleep data into newboom.

The legacy data on prod (``/root/boom/boom_api/repo``) is stored as one JSON file
per device capture. Each file's bytes are *exactly* a newboom upload request body:

    ppi   -> {"device","address","timezone","datas":[{time,hr,spo2,ppi}]}
    sleep -> {"device","address","timezone","datas":[{time,sleepOnsetTime,
              awakeTime,lightSleepPeriod,deepSleepPeriod,otherSleepPeriod,
              heartRateRest}]}

Pushing is therefore a *normalizing replay*: read the file, reshape it to the
current request contract, and POST it to ``{target}{api_prefix}/{ppi|sleep}``.

Two legacy-shape gaps must be bridged (both are flags, defaulted for this data):

  * ``activity`` -- new ``PPIUploadRequest`` requires an ``activity`` int per row
    (``ActivityState``, 0..7); it drives sleep-window detection in the compute
    pipeline. Legacy ppi rows carry none, so a default is injected (``--activity-
    default``; 3=RELAXED = "awake" so windows are not all treated as sleep).
  * ``spo2`` -- the source transitions from legacy percent (0-100, e.g. 93 ==
    93.0%%) to the new device-raw *x10* units (e.g. 862 == 86.2%%). A single
    global scale can't fit both, so ``--spo2-scale auto`` (default) detects the
    unit per file from its max spo2 (>150 -> already *x10, pass through; else
    percent -> multiply by 10). 0 ("not reported") is unaffected.

Subcommands:

  pull      rsync the prod repo -> a local staging dir (filtered, resumable)
  push      normalize + POST a local staging dir -> a newboom REST target
            (filtered, resumable; the local->server inverse of ``pull``)

Stdlib only; no new dependencies.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import http.client
import json
import os
import ssl
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

# --------------------------------------------------------------------------- #
# Defaults / env
# --------------------------------------------------------------------------- #


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


DEFAULTS = {
    "ssh": _env("NEWBOOM_SYNC_SSH", "root@47.100.30.18"),
    "repo": _env("NEWBOOM_SYNC_REPO", "/root/boom/boom_api/repo"),
    "staging": _env("NEWBOOM_SYNC_STAGING", "staging"),
    "target": _env("NEWBOOM_SYNC_TARGET", "http://localhost:8000"),
    "api_prefix": _env("NEWBOOM_SYNC_API_PREFIX", "/api/upload"),
    "activity_default": int(_env("NEWBOOM_SYNC_ACTIVITY_DEFAULT", "3")),
    "spo2_scale": _env("NEWBOOM_SYNC_SPO2_SCALE", "auto"),
    "concurrency": int(_env("NEWBOOM_SYNC_CONCURRENCY", "4")),
    "retry": int(_env("NEWBOOM_SYNC_RETRY", "3")),
    "http_timeout": int(_env("NEWBOOM_SYNC_HTTP_TIMEOUT", "30")),
}

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

_lock = threading.Lock()


def log(msg: str, *, quiet: bool = False) -> None:
    if quiet:
        return
    with _lock:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Classification & normalization
# --------------------------------------------------------------------------- #


def classify(name: str) -> str:
    """Classify a repo filename. ``bare`` = the legacy extensionless captures."""
    if name.endswith(".ppi"):
        return "ppi"
    if name.endswith(".sleep"):
        return "sleep"
    # Legacy captures without an extension are a bare sha256 hash.
    if (
        "." not in name
        and len(name) >= 16
        and all(c in "0123456789abcdef" for c in name)
    ):
        return "bare"
    return "unknown"


def load_body(path: Path) -> dict:
    with open(path, "rb") as f:
        raw = f.read()
    return json.loads(raw.decode("utf-8"))


def _spo2_scale_for(body: dict, mode: float | str) -> float:
    """Resolve the spo2 multiplier for one file.

    ``mode`` is ``"auto"`` or a float. The source transitions from legacy
    percent spo2 (0-100, e.g. 93 == 93.0%%) to the new device-raw *x10* units
    (e.g. 862 == 86.2%%). A single global scale can't fit both, so ``auto``
    inspects the file's own max spo2: >150 means it is already *x10 (pass
    through); otherwise it is percent, so multiply by 10. 0 (not reported)
    stays 0 under any scale.
    """
    if mode == "auto":
        mx = max((int(r.get("spo2") or 0) for r in body.get("datas", [])), default=0)
        return 1.0 if mx > 150 else 10.0
    return float(mode)


def normalize_ppi(
    body: dict, activity_default: int, spo2_mode: float | str, stats: dict
) -> dict:
    """Inject a missing ``activity`` and rescale legacy percent spo2.

    Mutates in place. ``activity`` is injected only where absent so any row that
    already carries it is preserved. The spo2 scale is resolved per file
    (``spo2_mode`` ``"auto"`` picks percent-vs-x10 by magnitude).
    """
    scale = _spo2_scale_for(body, spo2_mode)
    if scale != 1.0:
        stats["spo2_scale"] = scale
    for row in body.get("datas", []):
        if "activity" not in row:
            row["activity"] = activity_default
            stats["activity_injected"] += 1
        if scale != 1.0 and "spo2" in row and row["spo2"]:
            row["spo2"] = round(row["spo2"] * scale)
            stats["spo2_rescaled"] += 1
    return body


def normalize_sleep(body: dict) -> dict:
    """Sleep rows already match the contract; return unchanged."""
    return body


# --------------------------------------------------------------------------- #
# HTTP target (per-thread pooled connection)
# --------------------------------------------------------------------------- #


class HttpTarget:
    def __init__(self, base: str, prefix: str, timeout: int = 30) -> None:
        u = urlparse(base)
        self.scheme = u.scheme
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or (443 if self.scheme == "https" else 80)
        self.prefix = prefix.rstrip("/")
        self.timeout = timeout
        self._local = threading.local()

    def _conn(self) -> http.client.HTTPConnection:
        c = getattr(self._local, "c", None)
        if c is None:
            if self.scheme == "https":
                c = http.client.HTTPSConnection(
                    self.host,
                    self.port,
                    timeout=self.timeout,
                    context=ssl.create_default_context(),
                )
            else:
                c = http.client.HTTPConnection(
                    self.host, self.port, timeout=self.timeout
                )
            self._local.c = c
        return c

    def post(self, dtype: str, body: dict) -> tuple[int, dict]:
        """POST a request body; return (status, parsed_json). Resets the
        connection on transport failure so the next call reconnects."""
        conn = self._conn()
        try:
            conn.request(
                "POST",
                f"{self.prefix}/{dtype}",
                json.dumps(body),
                {"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            data = resp.read().decode("utf-8", "replace")
        except (http.client.HTTPException, ConnectionError, TimeoutError, OSError):
            self._local.c = None  # half-open / dead socket
            raise
        try:
            parsed = json.loads(data) if data else {}
        except json.JSONDecodeError:
            parsed = {"ok": False, "raw": data[:300]}
        return resp.status, parsed


# --------------------------------------------------------------------------- #
# State log (resume / idempotency)
# --------------------------------------------------------------------------- #


class StateLog:
    """Append-only JSONL of per-file import outcomes, keyed by content sha256.

    A file whose sha256 already has an ``ok`` line is skipped on rerun; a crash
    between POST and state-write is the only gap, and a rerun simply re-POSTs
    (the DB upserts by ``(device_id, time)``, so that is safe).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.done: set[str] = set()
        self.errors: dict[str, str] = {}
        if path.exists():
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("status") == "ok":
                        self.done.add(rec["sha256"])
                    else:
                        self.errors[rec["sha256"]] = rec.get("error", "?")

    def record(
        self, sha256: str, status: str, processed: int = 0, error: str = ""
    ) -> None:
        rec = {
            "sha256": sha256,
            "status": status,
            "processed": processed,
            "error": error,
            "ts": time.time(),
        }
        line = json.dumps(rec) + "\n"
        with _lock:
            with open(self.path, "a") as f:
                f.write(line)
            if status == "ok":
                self.done.add(sha256)
            elif status == "error":
                self.errors[sha256] = error

    def summary(self) -> dict:
        return {"ok": len(self.done), "errors": len(self.errors)}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #


def _post_one(
    path: Path, target: HttpTarget, dtype: str, body: dict, retries: int
) -> tuple[bool, int, str]:
    """POST a single file with backoff on transient/5xx failures.

    Returns (ok, processed, error). 4xx (e.g. 422 validation) is *not* retried.
    """
    last_err = ""
    for attempt in range(retries + 1):
        try:
            status, parsed = target.post(dtype, body)
        except Exception as e:  # noqa: BLE001
            last_err = f"transport: {e}"
        else:
            if status == 200 and parsed.get("ok"):
                return True, int(parsed.get("processed") or 0), ""
            if 500 <= status < 600:
                last_err = (
                    f"HTTP {status}: {parsed.get('message') or parsed.get('raw', '')}"
                )
            else:
                # 4xx: bad body, not transient.
                return (
                    False,
                    0,
                    f"HTTP {status}: {parsed.get('message') or parsed.get('raw', '')}",
                )
        if attempt < retries:
            time.sleep(min(2**attempt, 8))
    return False, 0, last_err or "unknown"


def cmd_push(args: argparse.Namespace) -> int:
    """Filter a local staging dir (the same filters as ``pull``) and POST the
    survivors, normalized, to a newboom REST target (``--target``).

    ``pull`` is server -> local (rsync); ``push`` is local -> server (REST),
    independent of where the server lives. The source is a local directory
    shared with ``pull`` via ``--staging``, so this uploads whatever ``pull``
    left behind.
    """
    src = Path(args.staging)
    if not src.is_dir():
        log(f"--staging not found: {src}", quiet=args.quiet)
        return 2

    target = HttpTarget(args.target, args.api_prefix, args.http_timeout)
    state = StateLog(Path(args.state) if args.state else src / ".import-state.jsonl")

    # Local filters mirror cmd_pull's server-side find, applied to the dir:
    #   --type        ppi / sleep / all (extension + bare gating)
    #   --mac         MAC prefix (filename starts with "<mac>.")
    #   --capture     exact capture date (filename's 2nd dot-segment)
    #   --since       mtime on/after a date (disk write time, not capture time)
    #   --limit       most-recent N by the embedded capture timestamp
    types = [t.strip() for t in args.type.split(",")] if args.type else ["ppi", "sleep"]
    include_all = types == ["all"]
    since_d = date.fromisoformat(args.since) if args.since else None
    since_ts = (
        datetime(since_d.year, since_d.month, since_d.day).timestamp() if since_d else None
    )
    capture = None
    if args.capture:
        cap = args.capture.replace("-", "")
        try:
            datetime.strptime(cap, "%Y%m%d")
        except ValueError:
            log(
                f"ERROR: --capture must be a date (got {args.capture!r})",
                quiet=args.quiet,
            )
            return 1
        capture = cap
    mac = args.mac or None

    files = sorted(
        p for p in src.rglob("*") if p.is_file() and not p.name.startswith(".")
    )
    log(f"found {len(files)} files in {src}", quiet=args.quiet)

    # Pre-filter by type/mac/capture/since, then skip already-done.
    pending: list[Path] = []
    skipped_bare = skipped_unknown = skipped_filter = skipped_done = 0
    for p in files:
        kind = classify(p.name)
        if kind == "unknown":
            skipped_unknown += 1
            continue
        if kind == "bare":
            if args.bare_files == "error":
                log(f"ERROR: bare file not allowed: {p.name}", quiet=args.quiet)
                return 1
            if args.bare_files == "skip":
                skipped_bare += 1
                continue
            kind = "ppi"  # fill: treat bare as ppi
        elif not include_all and kind not in types:
            skipped_filter += 1
            continue
        if mac and not p.name.startswith(mac + "."):
            skipped_filter += 1
            continue
        if capture:
            parts = p.name.split(".")
            if len(parts) < 2 or not parts[1].startswith(capture):
                skipped_filter += 1
                continue
        if since_ts is not None and p.stat().st_mtime < since_ts:
            skipped_filter += 1
            continue
        sha = sha256_file(p)
        if sha in state.done:
            skipped_done += 1
            continue
        pending.append(p)

    # --limit: most-recent N by embedded capture time (mirrors cmd_pull). Only
    # the non-all case; bare is excluded then, so no named/bare split is needed.
    if args.limit and args.limit > 0 and not include_all:
        pending.sort(key=lambda p: _file_ts(p.name) or datetime.min, reverse=True)
        pending = pending[: args.limit]

    log(
        f"to push: {len(pending)}  (skip bare={skipped_bare}, unknown={skipped_unknown}, "
        f"filtered={skipped_filter}, already-done={skipped_done})",
        quiet=args.quiet,
    )
    if args.dry_run:
        log(f"DRY-RUN: no POSTs. {len(pending)} files would be sent.", quiet=args.quiet)
        return 0

    ok = err = 0
    err_lock = threading.Lock()
    scale_tally: dict[float, int] = {}

    def work(p: Path) -> None:
        nonlocal ok, err
        kind = (
            "ppi"
            if p.name.endswith(".ppi")
            or (args.bare_files == "fill" and classify(p.name) == "bare")
            else "sleep"
        )
        try:
            body = load_body(p)
        except Exception as e:  # noqa: BLE001
            with err_lock:
                err += 1
            state.record(sha256_file(p), "error", 0, f"parse: {e}")
            return
        stats = {"activity_injected": 0, "spo2_rescaled": 0}
        if kind == "ppi":
            normalize_ppi(body, args.activity_default, args.spo2_scale, stats)
            with err_lock:
                scale_tally.setdefault(stats.get("spo2_scale", 1.0), 0)
                scale_tally[stats.get("spo2_scale", 1.0)] += 1
        ok_flag, processed, errmsg = _post_one(p, target, kind, body, args.retry)
        if ok_flag:
            with err_lock:
                ok += 1
            state.record(sha256_file(p), "ok", processed)
            if ok % 100 == 0:
                log(f"pushed {ok} ok / {err} err", quiet=args.quiet)
        else:
            with err_lock:
                err += 1
            state.record(sha256_file(p), "error", 0, errmsg)

    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        list(ex.map(work, pending))

    log(
        f"DONE: ok={ok} err={err} skip_bare={skipped_bare} "
        f"skip_unknown={skipped_unknown} skip_filtered={skipped_filter} "
        f"skip_done={skipped_done}",
        quiet=args.quiet,
    )
    if scale_tally:
        log(
            f"spo2 scale applied: "
            + ", ".join(f"{s:g}x={n}" for s, n in sorted(scale_tally.items())),
            quiet=args.quiet,
        )
    if err:
        log(f"{err} file(s) failed; see state log {state.path}", quiet=args.quiet)
    return 1 if err else 0


# --------------------------------------------------------------------------- #
# pull (rsync over ssh, filtered + resumable)
# --------------------------------------------------------------------------- #


def _file_ts(name: str) -> datetime | None:
    """Parse the capture timestamp embedded in a named file:
    ``{MAC}.{YYYYMMDD-HHMMSS}.{hash}.{ext}``. Bare files have no timestamp."""
    parts = name.split(".")
    if len(parts) < 2:
        return None
    try:
        return datetime.strptime(parts[1], "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def build_find_cmd(
    path: str,
    mac: str | None,
    capture: str | None,
    since: date | None,
    _type: str = "f",
    *exts: str,
) -> str:
    # Base find command
    cmd = f"find {path} -maxdepth 1 -type {_type}"
    names = []
    if exts:
        macs = [mac or "*" for _ in range(len(exts))]
        caps = [capture or "*" for _ in range(len(exts))]
        for m, d, e in zip(macs, caps, exts):
            names.append(f"{m}.{d}-*.{e}")
    else:
        if mac or capture:
            names.append(f"{mac or '*'}.{capture or '*'}-*")

    # Only add name filtering if names were actually provided
    if names:
        name_conditions = " -o ".join([f'-name "{n}"' for n in names])
        cmd += f" \\( {name_conditions} \\)"

    # Add date condition and formatting (GNU find specific)
    if since is not None:
        d = str(since)
        cmd += f" -newermt '{d}'"

    return cmd + " -printf '%P\\n'"


def cmd_pull(args: argparse.Namespace) -> int:
    """List the repo once over ssh, filter, rsync the survivors.

    The filters ``find`` can express -- MAC prefix, exact capture date, extension
    type, and mtime-newer (``--since``) -- are pushed to the server as a single
    find command, so the list we pull back is already narrow. The only thing
    left to do locally is the most-recent-N sort (``--limit``): it needs the
    filename's embedded capture time ordered, which find can't do.
    """
    staging = Path(args.staging)
    staging.mkdir(parents=True, exist_ok=True)
    list_path = staging / ".pull-filelist.txt"

    types = [t.strip() for t in args.type.split(",")] if args.type else ["ppi", "sleep"]
    include_all = types == ["all"]
    since_d = date.fromisoformat(args.since) if args.since else None

    # --capture is the filename's own capture date (exact day). Accept either
    # YYYYMMDD or YYYY-MM-DD; find's glob needs the on-disk 8-digit segment.
    capture = None
    if args.capture:
        cap = args.capture.replace("-", "")
        try:
            datetime.strptime(cap, "%Y%m%d")
        except ValueError:
            log(
                f"ERROR: --capture must be a date (got {args.capture!r})",
                quiet=args.quiet,
            )
            return 1
        capture = cap

    # --type drives the extension globs; "all" pushes no extension filter.
    exts = [t for t in types if t in ("ppi", "sleep")] if not include_all else []

    # build_find_cmd's 5th positional is the find -type value ("f"); pass it
    # explicitly so MAC / capture / since / exts land in the filter slots.
    list_cmd = build_find_cmd(
        args.repo, args.mac or None, capture, since_d, "f", *exts
    )
    log(f'listing: ssh {args.ssh} "{list_cmd}"', quiet=args.quiet)
    res = subprocess.run(
        ["ssh", args.ssh, list_cmd], capture_output=True, text=True, check=False
    )
    if res.returncode != 0:
        log(f"listing failed: {res.stderr.strip()[:500]}", quiet=args.quiet)
        return 1
    all_names = [ln for ln in res.stdout.splitlines() if ln]
    log(
        f"server returned {len(all_names)} file(s); filtering locally", quiet=args.quiet
    )

    candidates: list[tuple[str, str]] = []
    for n in all_names:
        kind = classify(n)
        if kind == "unknown":
            continue
        if include_all:
            if kind == "bare" and args.bare_files == "error":
                log(f"ERROR: bare file not allowed: {n}", quiet=args.quiet)
                return 1
        elif kind not in types:
            continue
        candidates.append((n, kind))

    if args.limit and args.limit > 0 and not include_all:
        named = [c for c in candidates if c[1] != "bare"]
        bare = [c for c in candidates if c[1] == "bare"]
        named.sort(key=lambda c: _file_ts(c[0]) or datetime.min, reverse=True)
        candidates = named[: args.limit] + bare

    names = [c[0] for c in candidates]
    list_path.write_text("\n".join(names) + ("\n" if names else ""))
    log(f"matched {len(names)} file(s) -> {list_path}", quiet=args.quiet)
    if not names:
        return 0

    # rsync the matched basenames. -a + --partial = resumable.
    rsync = [
        "rsync",
        "-avz",
        "--progress",
        "--partial",
        "--files-from",
        str(list_path),
        f"{args.ssh}:{args.repo}/",
        f"{staging}/",
    ]
    if args.dry_run:
        rsync.insert(1, "-n")
    log(f"rsync: {' '.join(rsync)}", quiet=args.quiet)
    return subprocess.run(rsync, check=False).returncode


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="newboom_sync",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--quiet", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    # pull
    q = sub.add_parser("pull", help="rsync prod repo -> local staging")
    q.add_argument("--ssh", default=DEFAULTS["ssh"])
    q.add_argument("--repo", default=DEFAULTS["repo"])
    q.add_argument("--staging", default=DEFAULTS["staging"])
    q.add_argument(
        "--type",
        default="ppi,sleep",
        help="comma list: ppi / sleep / all (default excludes bare)",
    )
    q.add_argument(
        "--mac", default="", help="MAC filter, e.g. D6:02:32:72:6A:81"
    )
    q.add_argument(
        "--capture",
        default="",
        help="exact capture date YYYYMMDD (matches the filename's own timestamp)",
    )
    q.add_argument(
        "--since",
        default="",
        help="only files whose mtime is after YYYY-MM-DD (disk write time, not capture time)",
    )
    q.add_argument("--limit", type=int, default=0, help="most-recent N files only")
    q.add_argument(
        "--bare-files",
        dest="bare_files",
        choices=["skip", "fill", "error"],
        default="skip",
        help="only with --type all: skip / fill-as-ppi / error on bare",
    )
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_pull)

    # push (local -> REST target; reuses pull's filters + the POST pipeline)
    u = sub.add_parser(
        "push", help="normalize + POST a local staging dir -> a newboom REST target"
    )
    u.add_argument(
        "--staging",
        default=DEFAULTS["staging"],
        help="local dir to push from (shared with pull's staging)",
    )
    u.add_argument(
        "--target",
        default=DEFAULTS["target"],
        help="newboom REST base URL (default http://localhost:8000)",
    )
    u.add_argument("--api-prefix", dest="api_prefix", default=DEFAULTS["api_prefix"])
    u.add_argument(
        "--type",
        default="ppi,sleep",
        help="comma list: ppi / sleep / all (default excludes bare)",
    )
    u.add_argument("--mac", default="", help="MAC filter, e.g. D6:02:32:72:6A:81")
    u.add_argument(
        "--capture",
        default="",
        help="exact capture date YYYYMMDD (matches the filename's own timestamp)",
    )
    u.add_argument(
        "--since",
        default="",
        help="only files whose mtime is after YYYY-MM-DD (disk write time, not capture time)",
    )
    u.add_argument("--limit", type=int, default=0, help="most-recent N files only")
    u.add_argument(
        "--bare-files",
        dest="bare_files",
        choices=["skip", "fill", "error"],
        default="skip",
        help="skip / fill-as-ppi / error on bare (hash-only, no extension)",
    )
    u.add_argument(
        "--activity-default",
        dest="activity_default",
        type=int,
        default=DEFAULTS["activity_default"],
    )
    u.add_argument(
        "--spo2-scale",
        dest="spo2_scale",
        default="auto",
        help="spo2 units: auto (detect per file) | 1 (pass-through) | 10",
    )
    u.add_argument("--concurrency", type=int, default=DEFAULTS["concurrency"])
    u.add_argument("--retry", type=int, default=DEFAULTS["retry"])
    u.add_argument(
        "--http-timeout",
        dest="http_timeout",
        type=int,
        default=DEFAULTS["http_timeout"],
    )
    u.add_argument(
        "--state", default="", help="state-log path (default: <staging>/.import-state.jsonl)"
    )
    u.add_argument("--dry-run", action="store_true")
    u.set_defaults(func=cmd_push)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.quiet = getattr(args, "quiet", False)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
