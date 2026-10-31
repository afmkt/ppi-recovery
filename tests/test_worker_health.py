"""Worker health surfacing.

The background worker must make a *persistently* failing cycle observable (via
``WorkerHealth`` and the ``/healthz`` endpoint) instead of looping forever while
silently failing to persist. That is the situation that let a read-only
database go unnoticed in production.
"""

from __future__ import annotations

import asyncio
import json

from newboom import api as api_module
from newboom import worker as worker_module
from newboom.worker import WorkerHealth

# --- WorkerHealth bookkeeping ----------------------------------------------


def test_starts_unhealthy_before_any_success():
    h = WorkerHealth()
    assert h.is_healthy() is False
    snap = h.snapshot()
    assert snap["last_error"] is None
    assert snap["seconds_since_last_success"] is None


def test_success_marks_healthy():
    h = WorkerHealth()
    h.record_success()
    assert h.is_healthy() is True
    assert h.total_cycles == 1
    assert h.total_failures == 0
    assert h.consecutive_failures == 0
    assert h.last_error is None


def test_single_failure_stays_healthy_within_window():
    h = WorkerHealth(stale_after_s=30.0)
    h.record_success()
    h.record_failure(RuntimeError("attempt to write a readonly database"))
    assert h.total_cycles == 2
    assert h.total_failures == 1
    assert h.consecutive_failures == 1
    assert "readonly" in h.last_error
    # A lone failed cycle does not trip health: the recent success is fresh.
    assert h.is_healthy() is True


def test_stale_success_is_unhealthy():
    # A negative window makes any already-recorded success immediately stale.
    h = WorkerHealth(stale_after_s=-1.0)
    h.record_success()
    assert h.is_healthy() is False


def test_success_clears_failure_streak():
    h = WorkerHealth()
    h.record_failure(ValueError("a"))
    h.record_failure(ValueError("b"))
    assert h.consecutive_failures == 2
    assert h.total_failures == 2
    h.record_success()
    assert h.consecutive_failures == 0
    assert h.is_healthy() is True


def test_reset_clears_state():
    h = WorkerHealth()
    h.mark_started()
    h.record_success()
    h.record_failure(RuntimeError("x"))
    h.reset()
    assert h.total_cycles == 0
    assert h.total_failures == 0
    assert h.consecutive_failures == 0
    assert h.last_error is None
    assert h.is_healthy() is False


# --- the loop records, it does not swallow ---------------------------------


def test_cycle_records_failure_without_raising(monkeypatch):
    def boom(_devices):
        raise RuntimeError("attempt to write a readonly database")

    monkeypatch.setattr(worker_module, "process_pipeline", boom)
    worker_module.worker_health.reset()
    # A failing cycle must be recorded but must NOT propagate out of the loop.
    asyncio.run(worker_module._run_cycle_once())
    wh = worker_module.worker_health
    assert wh.total_cycles == 1
    assert wh.total_failures == 1
    assert wh.consecutive_failures == 1
    assert "readonly" in wh.last_error
    assert wh.is_healthy() is False


def test_cycle_records_success(monkeypatch):
    monkeypatch.setattr(
        worker_module, "process_pipeline", lambda _devices: None
    )
    worker_module.worker_health.reset()
    asyncio.run(worker_module._run_cycle_once())
    wh = worker_module.worker_health
    assert wh.total_cycles == 1
    assert wh.total_failures == 0
    assert wh.is_healthy() is True


# --- /healthz endpoint -----------------------------------------------------


def test_healthz_503_when_unhealthy():
    worker_module.worker_health.reset()
    resp = api_module.healthz()
    assert resp.status_code == 503
    body = json.loads(resp.body)
    assert body["ok"] is False


def test_healthz_503_body_reports_last_error():
    worker_module.worker_health.reset()
    worker_module.worker_health.record_failure(
        RuntimeError("attempt to write a readonly database")
    )
    resp = api_module.healthz()
    assert resp.status_code == 503
    body = json.loads(resp.body)
    assert body["ok"] is False
    assert "readonly" in body["worker"]["last_error"]


def test_healthz_200_when_healthy():
    worker_module.worker_health.reset()
    worker_module.worker_health.record_success()
    resp = api_module.healthz()
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert "worker" in body
