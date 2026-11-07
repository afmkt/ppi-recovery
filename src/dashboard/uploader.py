"""HTTP client for the newboom upload API, used by the dashboard's Upload tab.

The marimo dashboard is mounted in-process in the FastAPI app, so it can reach the
very same ``/api/upload/ppi`` / ``/api/upload/sleep`` endpoints that a device or
``tools/newboom_sync.py`` would call. Rather than re-implement persistence, the
Upload tab POSTs a selected file's bytes to that endpoint, so request validation,
the hash-named file write and the ``(device_id, time)`` upsert all happen in the
API — the dashboard only moves bytes.

Kept marimo-free (pure stdlib) so it is importable and testable without a running
notebook, mirroring :mod:`dashboard.metrics`. The transport mirrors
``tools/newboom_sync.py``'s ``HttpTarget`` (a per-call ``http.client`` POST).
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

# Default target: the server this dashboard runs inside. The app binds HOST:PORT
# (defaults 0.0.0.0:8000); a loopback client talks to 127.0.0.1 on that PORT.
# Overridable so a dashboard deployed elsewhere can point at a remote API.
_DEFAULT_PORT = os.getenv("PORT", "8000")
DEFAULT_BASE = os.getenv("DASHBOARD_API_BASE", f"http://127.0.0.1:{_DEFAULT_PORT}")
# ``/api/upload`` returns ``ApiResponse`` (ok / processed / device_id / file) — richer
# than the ``/upload`` BoomMessage (code / msg) and the same path
# ``tools/newboom_sync.py`` posts to. Point DASHBOARD_UPLOAD_PREFIX at ``/upload`` to
# use the BoomResponse-shaped endpoint instead; both persist identically.
DEFAULT_PREFIX = os.getenv("DASHBOARD_UPLOAD_PREFIX", "/api/upload")
# A per-second PPI capture is several MB; give the upload a generous deadline.
DEFAULT_TIMEOUT = float(os.getenv("DASHBOARD_UPLOAD_TIMEOUT", "120"))

TYPE_TO_ENDPOINT = {"ppi": "ppi", "sleep": "sleep"}


@dataclass(frozen=True)
class UploadResult:
    """One upload attempt's outcome, rendered for the UI by :meth:`as_markdown`."""

    ok: bool
    status: int
    message: str = ""
    dtype: str = ""
    processed: int | None = None
    device_id: int | None = None
    file: str | None = None
    error: str | None = None

    def as_markdown(self) -> str:
        """A one-line, UI-ready status (emoji + key facts)."""
        label = self.dtype.upper() if self.dtype else "upload"
        if self.ok:
            parts = [f"✅ **{label}** uploaded"]
            if self.processed is not None:
                parts.append(f"{self.processed:,} row(s)")
            if self.device_id is not None:
                parts.append(f"device #{self.device_id}")
            if self.file:
                parts.append(f"saved `{self.file}`")
            return " · ".join(parts)
        code = f"HTTP {self.status} " if self.status else ""
        reason = self.error or self.message or "unknown error"
        return f"❌ **{label}** failed ({code}{reason})"


def parse_payload(data: bytes | str) -> dict:
    """Decode an upload body to a dict; raise ``ValueError`` with a clear message.

    A cheap client-side gate so a malformed file fails fast in the UI instead of
    round-tripping to the API and coming back as a cryptic 422. The API still
    validates every field on receipt, so this is a convenience, not a guarantee.
    """
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid JSON ({e})") from None
    if not isinstance(obj, dict):
        raise ValueError("expected a JSON object with device/address/timezone/datas")
    return obj


def _describe_error(body: dict) -> str:
    """Human-readable reason from an error body.

    FastAPI validation errors arrive as ``{"detail": [{loc,msg,type}, ...]}``;
    newboom's own error handlers use ``{"ok": false, "message": ...}``. Without
    this a 422 logs as a blank reason and the cause is lost.
    """
    detail = body.get("detail")
    if detail:
        items = detail if isinstance(detail, list) else [detail]
        parts: list[str] = []
        for item in items:
            if isinstance(item, dict):
                loc = ".".join(
                    str(x)
                    for x in item.get("loc", [])
                    if x not in ("body", "query", "path", "url")
                )
                msg = item.get("msg", "")
                parts.append(f"{loc}: {msg}" if loc else str(msg))
            else:
                parts.append(str(item))
        return "; ".join(parts)
    return str(body.get("message") or body.get("msg") or body.get("raw") or "")


def _parse_result(status: int, body: object, dtype: str) -> UploadResult:
    """Normalise both the ``ApiResponse`` (``/api/upload``) and ``BoomMessage``
    (``/upload``) success shapes, plus error bodies, into one :class:`UploadResult`."""
    if isinstance(body, dict):
        # ApiResponse shape: {"ok", "message", "processed", "device_id", "file"}.
        if "ok" in body:
            ok = bool(body.get("ok"))
            message = body.get("message") or ""
            return UploadResult(
                ok=ok,
                status=status,
                message=message,
                dtype=dtype,
                processed=body.get("processed"),
                device_id=body.get("device_id"),
                file=body.get("file"),
                error=None if ok else _describe_error(body),
            )
        # BoomMessage shape: {"code", "msg"} — success is code == 0.
        if "code" in body:
            ok = body.get("code") == 0 and 200 <= status < 300
            return UploadResult(
                ok=ok,
                status=status,
                message=body.get("msg") or "",
                dtype=dtype,
                error=None if ok else _describe_error(body),
            )
        # An error body we did not anticipate: surface whatever it says.
        return UploadResult(
            ok=False,
            status=status,
            message="",
            dtype=dtype,
            error=_describe_error(body),
        )
    # Non-JSON / empty body on a non-2xx, or an unparseable 2xx.
    return UploadResult(
        ok=200 <= status < 300,
        status=status,
        message=str(body)[:300] if body else "",
        dtype=dtype,
        error=None if 200 <= status < 300 else "non-JSON response",
    )


def post_upload(
    base: str,
    prefix: str,
    dtype: str,
    data: bytes | str,
    timeout: float = DEFAULT_TIMEOUT,
) -> UploadResult:
    """POST one file's bytes to ``{base}{prefix}/{dtype}``; return an
    :class:`UploadResult`.

    ``dtype`` is ``"ppi"`` or ``"sleep"``. The bytes are sent verbatim
    (``Content-Type: application/json``), exactly as a device or
    ``tools/newboom_sync.py`` would send them, so the endpoint validates, writes
    the hash-named file and upserts as usual. Transport failures (connection
    refused, timeout) return a failed :class:`UploadResult` rather than raising,
    so the UI can show the reason.
    """
    parsed_type = TYPE_TO_ENDPOINT.get(dtype, dtype)
    body = data if isinstance(data, bytes) else data.encode("utf-8")
    u = urlsplit(base or DEFAULT_BASE)
    scheme = u.scheme or "http"
    host = u.hostname or "127.0.0.1"
    port = u.port or (443 if scheme == "https" else 80)
    path = f"{(prefix or DEFAULT_PREFIX).rstrip('/')}/{parsed_type}"

    status = 0
    raw = ""
    transport_error = None
    conn: http.client.HTTPConnection | None = None
    try:
        if scheme == "https":
            conn = http.client.HTTPSConnection(
                host,
                port,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("POST", path, body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        status = resp.status
        raw = resp.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException, TimeoutError) as e:
        transport_error = f"could not reach {host}:{port} ({e.__class__.__name__})"
    finally:
        if conn is not None:
            conn.close()

    if transport_error is not None:
        return UploadResult(ok=False, status=0, dtype=dtype, error=transport_error)
    try:
        body_obj = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return UploadResult(
            ok=False, status=status, dtype=dtype, error="non-JSON response", message=raw[:300]
        )
    return _parse_result(status, body_obj, dtype)
