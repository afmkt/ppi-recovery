"""Dashboard Upload-tab client (``dashboard.uploader``) tests.

Hermetic: no FastAPI app. ``post_upload`` is exercised against a throwaway
``http.server`` so the ``http.client`` transport and response parsing are covered
without a running server; ``parse_payload`` / ``as_markdown`` / ``_parse_result``
are pure.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dashboard import uploader


# --- pure: payload gate -----------------------------------------------------


def test_parse_payload_accepts_valid_object():
    obj = uploader.parse_payload(
        b'{"device": "D", "address": "AA", "timezone": "08:00", "datas": []}'
    )
    assert obj["device"] == "D"


def test_parse_payload_rejects_bad_json():
    with pytest.raises(ValueError):
        uploader.parse_payload(b"{not json")


def test_parse_payload_rejects_non_object():
    with pytest.raises(ValueError):
        uploader.parse_payload(b"[1, 2, 3]")


# --- pure: result rendering / parsing --------------------------------------


def test_as_markdown_success_ppi():
    r = uploader.UploadResult(
        ok=True,
        status=200,
        dtype="ppi",
        message="PPI data uploaded successfully",
        processed=500,
        device_id=3,
        file="D-x.ppi.json",
    )
    md = r.as_markdown()
    assert "✅" in md
    assert "PPI" in md
    assert "500" in md
    assert "device #3" in md
    assert "D-x.ppi.json" in md


def test_as_markdown_failure_surfaces_reason():
    r = uploader.UploadResult(ok=False, status=422, dtype="sleep", error="datas: required")
    md = r.as_markdown()
    assert "❌" in md
    assert "SLEEP" in md
    assert "HTTP 422" in md
    assert "datas: required" in md


def test_parse_result_apisuccess_shape():
    r = uploader._parse_result(
        200, {"ok": True, "processed": 10, "device_id": 1, "file": "f", "message": "m"}, "ppi"
    )
    assert r.ok is True
    assert r.processed == 10
    assert r.device_id == 1
    assert r.file == "f"


def test_parse_result_boommessage_success():
    r = uploader._parse_result(200, {"code": 0, "msg": ""}, "ppi")
    assert r.ok is True
    assert r.message == ""


def test_parse_result_boommessage_failure():
    r = uploader._parse_result(500, {"code": 1, "msg": "boom"}, "sleep")
    assert r.ok is False
    assert "boom" in r.error


def test_describe_error_flattens_validation_detail():
    body = {"detail": [{"loc": ["body", "datas", 0, "hr"], "msg": "field required"}]}
    assert "datas.0.hr: field required" in uploader._describe_error(body)


# --- pure: batch + summary --------------------------------------------------


def test_as_markdown_success_includes_filename():
    r = uploader.UploadResult(
        ok=True,
        status=200,
        dtype="ppi",
        processed=500,
        device_id=3,
        file="D-x.ppi.json",
        name="cap.ppi.json",
    )
    md = r.as_markdown()
    # The source file's name leads so a multi-file batch is traceable file-by-file.
    assert "cap.ppi.json" in md
    assert "PPI" in md


def test_as_markdown_failure_includes_filename():
    r = uploader.UploadResult(ok=False, status=422, dtype="sleep", error="datas: required", name="bad.json")
    md = r.as_markdown()
    assert "bad.json" in md
    assert "SLEEP" in md


def test_summarize_all_ok_counts_rows():
    res = [
        uploader.UploadResult(ok=True, status=200, dtype="ppi", processed=500, device_id=3, file="a", name="a.json"),
        uploader.UploadResult(ok=True, status=200, dtype="ppi", processed=700, device_id=3, file="b", name="b.json"),
    ]
    md = uploader.summarize(res)
    assert "2/2" in md
    assert "1,200" in md
    assert "✅" in md


def test_summarize_partial_names_failed_file():
    res = [
        uploader.UploadResult(ok=True, status=200, dtype="ppi", processed=10, device_id=3, file="a", name="a.json"),
        uploader.UploadResult(ok=False, status=422, dtype="ppi", error="datas: required", name="c.json"),
    ]
    md = uploader.summarize(res)
    assert "1/2" in md
    assert "1 failed" in md
    # The user must be able to tell *which* file failed and why.
    assert "c.json" in md
    assert "datas: required" in md


def test_summarize_empty_is_blank():
    assert uploader.summarize([]) == ""


# --- HTTP transport against a hermetic local server ------------------------


@pytest.fixture
def server():
    """A one-shot HTTP server; the yielded holder lets a test set the response
    and observe the request path the client actually hit."""
    holder: dict = {
        "status": 200,
        "response": {
            "ok": True,
            "message": "uploaded",
            "processed": 7,
            "device_id": 2,
            "file": "D-x.ppi.json",
        },
        "paths": [],
    }

    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            holder["paths"].append(self.path)
            self.send_response(holder["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(holder["response"]).encode())

        def log_message(self, *args) -> None:  # silence the test runner
            return

    httpd = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", holder
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_post_upload_success_hits_ppi_endpoint(server):
    base, holder = server
    body = b'{"device": "D", "address": "A", "timezone": "08:00", "datas": []}'
    r = uploader.post_upload(base, "/api/upload", "ppi", body)
    assert r.ok is True
    assert r.status == 200
    assert r.processed == 7
    assert r.device_id == 2
    assert r.file == "D-x.ppi.json"
    assert holder["paths"] == ["/api/upload/ppi"]


def test_post_upload_sleep_endpoint(server):
    base, _ = server
    r = uploader.post_upload(base, "/api/upload", "sleep", b"{}")
    assert r.ok is True
    assert r.status == 200
    # dtype selects the endpoint; nothing is sent that leaks ppi.
    assert r.status == 200


def test_post_upload_surfaces_error_body(server):
    base, holder = server
    holder["status"] = 422
    holder["response"] = {"ok": False, "message": "bad body"}
    r = uploader.post_upload(base, "/api/upload", "ppi", b"{}")
    assert r.ok is False
    assert r.status == 422
    assert "bad body" in (r.error or r.message)


def test_post_upload_batch_each_file_hits_endpoint(server):
    base, holder = server
    good = b'{"device": "D", "address": "A", "timezone": "08:00", "datas": []}'
    files = [
        ("a.ppi.json", good),
        ("not-json.ppi.json", b"not json"),  # bad JSON, gated client-side
        ("b.ppi.json", good),
    ]
    results = uploader.post_upload_batch(base, "/api/upload", "ppi", files)

    # One result per input file, in order, each tagged with its source name.
    assert [r.name for r in results] == ["a.ppi.json", "not-json.ppi.json", "b.ppi.json"]
    assert results[0].ok and results[2].ok
    # A bad file is gated before the transport, so a good file in a bad batch
    # still uploads: only the 2 good ones reached the endpoint.
    assert holder["paths"] == ["/api/upload/ppi", "/api/upload/ppi"]
    assert results[1].ok is False
    assert "not valid JSON" in (results[1].error or "")


def test_post_upload_transport_failure_returns_result_not_exception():
    # Bind a socket to claim a free port, then close it so a connect is refused.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = uploader.post_upload(f"http://127.0.0.1:{port}", "/api/upload", "ppi", b"{}")
    assert r.ok is False
    assert r.status == 0
    assert "could not reach" in r.error
