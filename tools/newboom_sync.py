#!/usr/bin/env python3
"""newboom-sync — transfer & import legacy ``boom_api`` PPI/Sleep data into newboom.

The legacy data on prod (``/root/boom/boom_api/repo``) is stored as one JSON file
per device capture. Each file's bytes are *exactly* a newboom upload request body:

    ppi   -> {"device","address","timezone","datas":[{time,hr,spo2,ppi,activity}]}
    sleep -> {"device","address","timezone","datas":[{time,sleepOnsetTime,
              awakeTime,lightSleepPeriod,deepSleepPeriod,otherSleepPeriod,
              heartRateRest}]}

The source files are the source of truth: pull is a faithful copy
(server -> local) and push is a faithful replay (local -> server). We never
rewrite a file between the two -- a capture that reaches the staging dir is the
capture that is POSTed, byte for byte. The data is produced in its final shape
by the upstream system; any old-shape -> new-shape bridging is the server's
responsibility, not this tool's.

Idempotency is server-side. A re-run of push first queries ``GET {prefix}/file``
for the content hashes the server has already saved and skips any local file whose
canonical hash matches -- there is no local state file to keep in sync. (The
server's DB upserts by ``(device_id, time)``, so a re-POST is safe regardless.)

Subcommands:

  pull      rsync the prod repo -> a local staging dir (filtered, resumable)
  push      POST a local staging dir verbatim -> a newboom REST target
            (filtered; the local->server inverse of ``pull``; skips files the
            server already has via the content-hash query above)

Stdlib only; no new dependencies.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import http.client
import json
import os
import re
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
    """Classify a repo filename by its capture extension."""
    if name.endswith(".ppi"):
        return "ppi"
    if name.endswith(".sleep"):
        return "sleep"
    return "unknown"


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

    def post(self, dtype: str, body: bytes) -> tuple[int, dict]:
        """POST the file's raw bytes verbatim; return (status, parsed_json).
        Resets the connection on transport failure so the next call reconnects."""
        conn = self._conn()
        try:
            conn.request(
                "POST",
                f"{self.prefix}/{dtype}",
                body,
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

    def get(self, path: str) -> tuple[int, dict]:
        """GET {prefix}/{path}; return (status, parsed_json). Resets the
        connection on transport failure so the next call reconnects."""
        conn = self._conn()
        try:
            conn.request("GET", f"{self.prefix}/{path}")
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
# Uploaded-content index (server-side idempotency)
# --------------------------------------------------------------------------- #


_NAME_HASH_RE = re.compile(r"([0-9a-f]{64})\.(?:ppi|sleep)\.json$")


def _content_hash(parsed: object) -> str:
    """Canonical content hash, identical to the one the server bakes into the
    saved filename: ``sha256(json.dumps(parsed, sort_keys=True))``. A local file
    is "already uploaded" when this hash is in the server's uploaded-file list."""
    return hashlib.sha256(json.dumps(parsed, sort_keys=True).encode()).hexdigest()


def _hash_from_filename(name: str) -> str | None:
    """Pull the 64-hex content hash out of a saved upload filename
    (``{device}-{YYYYMMDD-HHMMSS}-{hash}.{type}.json``); None if it doesn't match."""
    m = _NAME_HASH_RE.search(name)
    return m.group(1) if m else None


def fetch_uploaded_hashes(target: HttpTarget, retries: int) -> tuple[set[str], str]:
    """GET {prefix}/file and return (content_hashes, error).

    The server lists every body it has saved, each filename embedding the content
    hash we recompute on the client. On failure returns an empty set with a
    non-empty error, so the caller can fall back to "skip nothing" -- re-uploading
    is safe because the DB upserts by ``(device_id, time)``.
    """
    last_err = ""
    for attempt in range(retries + 1):
        try:
            status, parsed = target.get("file")
        except Exception as e:  # noqa: BLE001
            last_err = f"transport: {e}"
        else:
            if 200 <= status < 300 and parsed.get("ok"):
                names = parsed.get("data") or []
                if not isinstance(names, list):
                    last_err = f"/file data not a list: {type(names).__name__}"
                else:
                    return (
                        {
                            h
                            for n in names
                            if (h := _hash_from_filename(n)) is not None
                        },
                        "",
                    )
            last_err = f"HTTP {status}: {_describe_error(parsed)}"
        if attempt < retries:
            time.sleep(min(2 ** attempt, 8))
    return set(), last_err or "unknown"


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #


def _describe_error(parsed: dict) -> str:
    """Human-readable reason from an error body.

    FastAPI/validation errors arrive as ``{"detail": [{loc,msg,type}, ...]}``;
    newboom's own endpoints use ``message``/``raw``.  Without this a 422 logs as
    a blank ``HTTP 422:`` and the cause is lost.
    """
    detail = parsed.get("detail")
    if detail:
        items = detail if isinstance(detail, list) else [detail]
        parts: list[str] = []
        for item in items:
            if isinstance(item, dict):
                loc = ".".join(
                    str(x)
                    for x in item.get("loc", [])
                    if x not in ("body", "query", "path")
                )
                msg = item.get("msg", "")
                parts.append(f"{loc}: {msg}" if loc else str(msg))
            else:
                parts.append(str(item))
        return "; ".join(parts)
    return str(parsed.get("message") or parsed.get("raw") or "")


def _post_one(
    path: Path, target: HttpTarget, dtype: str, body: bytes, retries: int
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
                last_err = f"HTTP {status}: {_describe_error(parsed)}"
            else:
                # 4xx: bad body, not transient.
                return False, 0, f"HTTP {status}: {_describe_error(parsed)}"
        if attempt < retries:
            time.sleep(min(2**attempt, 8))
    return False, 0, last_err or "unknown"


def cmd_push(args: argparse.Namespace) -> int:
    """Filter a local staging dir (the same filters as ``pull``) and POST the
    survivors verbatim to a newboom REST target (``--target``).

    ``pull`` is server -> local (rsync); ``push`` is local -> server (REST),
    independent of where the server lives. The source is a local directory
    shared with ``pull`` via ``--staging``, so this uploads whatever ``pull``
    left behind, byte for byte -- no field is injected or rescaled.
    """
    src = Path(args.staging)
    if not src.is_dir():
        log(f"--staging not found: {src}", quiet=args.quiet)
        return 2

    target = HttpTarget(args.target, args.api_prefix, args.http_timeout)

    # Local filters mirror cmd_pull's server-side find, applied to the dir:
    #   --type        ppi / sleep / all (by extension)
    #   --mac         MAC prefix (filename starts with "<mac>.")
    #   --capture     exact capture date (filename's 2nd dot-segment)
    #   --since       mtime on/after a date (disk write time, not capture time)
    #   --limit       most-recent N by the embedded capture timestamp
    types = [t.strip() for t in args.type.split(",")] if args.type else ["ppi", "sleep"]
    include_all = types == ["all"]
    since_d = date.fromisoformat(args.since) if args.since else None
    since_ts = (
        datetime(since_d.year, since_d.month, since_d.day).timestamp()
        if since_d
        else None
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

    # Pre-filter by type/mac/capture/since.
    pending: list[Path] = []
    skipped_unknown = skipped_filter = 0
    for p in files:
        kind = classify(p.name)
        if kind == "unknown":
            skipped_unknown += 1
            continue
        if not include_all and kind not in types:
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
        pending.append(p)

    # --limit: most-recent N by embedded capture time (mirrors cmd_pull).
    if args.limit and args.limit > 0 and not include_all:
        pending.sort(key=lambda p: _file_ts(p.name) or datetime.min, reverse=True)
        pending = pending[: args.limit]

    log(
        f"to push: {len(pending)}  (skip unknown={skipped_unknown}, "
        f"filtered={skipped_filter})",
        quiet=args.quiet,
    )
    if args.dry_run:
        log(f"DRY-RUN: no POSTs. {len(pending)} files would be sent.", quiet=args.quiet)
        return 0

    # Query the server for what it already has and skip those (server-side
    # idempotency, no local state file). A failed query just means "skip
    # nothing" -- re-uploading is safe because the DB upserts by (device_id, time).
    uploaded_hashes, hash_err = fetch_uploaded_hashes(target, args.retry)
    if hash_err:
        log(
            f"WARN: could not query uploaded files ({hash_err}); skip disabled, "
            "re-uploading everything (idempotent)",
            quiet=args.quiet,
        )
    else:
        log(
            f"server has {len(uploaded_hashes)} uploaded file(s); skipping matches",
            quiet=args.quiet,
        )

    ok = err = 0
    err_lock = threading.Lock()
    err_counts: dict[str, int] = {}
    skip_counts: dict[str, int] = {}

    def work(p: Path) -> None:
        nonlocal ok, err
        dtype = "ppi" if p.name.endswith(".ppi") else "sleep"
        try:
            raw = p.read_bytes()
        except OSError as e:
            with err_lock:
                skip_counts["read"] = skip_counts.get("read", 0) + 1
            log(f"  SKIP {p.name}: read: {e}", quiet=args.quiet)
            return
        # Refuse to POST undecodable bytes: a truncated/empty capture can never
        # import, so skip it rather than 422-storm the server.
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as e:
            with err_lock:
                skip_counts["parse"] = skip_counts.get("parse", 0) + 1
            log(f"  SKIP {p.name}: parse: {e}", quiet=args.quiet)
            return
        # Server-side idempotency: skip if this exact content is already saved.
        if _content_hash(parsed) in uploaded_hashes:
            with err_lock:
                skip_counts["uploaded"] = skip_counts.get("uploaded", 0) + 1
            log(f"  SKIP {p.name}: already uploaded", quiet=args.quiet)
            return
        ok_flag, _, errmsg = _post_one(p, target, dtype, raw, args.retry)
        if ok_flag:
            with err_lock:
                ok += 1
            if ok % 100 == 0:
                log(f"pushed {ok} ok / {err} err", quiet=args.quiet)
        else:
            with err_lock:
                err += 1
                err_counts[errmsg] = err_counts.get(errmsg, 0) + 1
            log(f"  FAIL {p.name}: {errmsg}", quiet=args.quiet)

    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        list(ex.map(work, pending))

    skipped = sum(skip_counts.values())
    skip_detail = ", ".join(f"{k}={v}" for k, v in sorted(skip_counts.items())) or "none"
    log(
        f"DONE: ok={ok} err={err} skipped={skipped} "
        f"skip_unknown={skipped_unknown} skip_filtered={skipped_filter} "
        f"skip[{skip_detail}]",
        quiet=args.quiet,
    )
    if err:
        log(f"{err} file(s) failed this run:")
        for msg, n in sorted(err_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]:
            log(f"  {n:6d}  {msg}", quiet=args.quiet)
    return 1 if err else 0


# --------------------------------------------------------------------------- #
# pull (rsync over ssh, filtered + resumable)
# --------------------------------------------------------------------------- #


def _file_ts(name: str) -> datetime | None:
    """Parse the capture timestamp embedded in a named file:
    ``{MAC}.{YYYYMMDD-HHMMSS}.{hash}.{ext}``. An unparseable name yields None."""
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
    list_cmd = build_find_cmd(args.repo, args.mac or None, capture, since_d, "f", *exts)
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

    candidates: list[str] = []
    for n in all_names:
        kind = classify(n)
        if kind == "unknown":
            continue
        if not include_all and kind not in types:
            continue
        candidates.append(n)

    if args.limit and args.limit > 0 and not include_all:
        candidates.sort(key=lambda n: _file_ts(n) or datetime.min, reverse=True)
        candidates = candidates[: args.limit]

    names = candidates
    log(f"matched {len(names)} file(s)", quiet=args.quiet)
    if not names:
        return 0

    # Feed the matched basenames to rsync over stdin (--files-from=-): no
    # scratch file is left in staging. -a + --partial = resumable.
    rsync = [
        "rsync",
        "-avz",
        "--progress",
        "--partial",
        "--files-from=-",
        f"{args.ssh}:{args.repo}/",
        f"{staging}/",
    ]
    if args.dry_run:
        rsync.insert(1, "-n")
    log(f"rsync: {' '.join(rsync)}  (+{len(names)} names on stdin)", quiet=args.quiet)
    return subprocess.run(rsync, check=False, input="\n".join(names) + "\n").returncode


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
        help="comma list: ppi / sleep / all",
    )
    q.add_argument("--mac", default="", help="MAC filter, e.g. D6:02:32:72:6A:81")
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
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_pull)

    # push (local -> REST target; reuses pull's filters + the POST pipeline)
    u = sub.add_parser(
        "push", help="POST a local staging dir verbatim -> a newboom REST target"
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
        help="comma list: ppi / sleep / all",
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
    u.add_argument("--concurrency", type=int, default=DEFAULTS["concurrency"])
    u.add_argument("--retry", type=int, default=DEFAULTS["retry"])
    u.add_argument(
        "--http-timeout",
        dest="http_timeout",
        type=int,
        default=DEFAULTS["http_timeout"],
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
