"""Pipeline progress/health reporting (Q1–Q6): the diagnostic read-models.

Pins the two pieces of non-trivial logic added for worker-progress recording:
the reconciliation PASS/WARN/FAIL band thresholds, and that ``record_diagnostics``
upserts (never duplicates) the three reporting tables.
"""
from datetime import datetime, timedelta

from sqlmodel import Session, select

from newboom.compute import _recon_verdict, record_diagnostics
from newboom.database import engine
from newboom.model import (
    PPI,
    ActivityState,
    DataGap,
    Device,
    Layer,
    LayerFreshness,
    Reconciliation,
    ReconVerdict,
    Sleep,
)

# --- _recon_verdict band boundaries (PASS <=10% <= WARN <=30% <= FAIL) ------


def test_recon_verdict_pass_at_10_percent_boundary():
    verdict, delta, _ = _recon_verdict(100, 110)
    assert verdict == ReconVerdict.PASS
    assert delta == 10.0


def test_recon_verdict_warn_at_30_percent_boundary():
    verdict, delta, _ = _recon_verdict(100, 130)
    assert verdict == ReconVerdict.WARN
    assert delta == 30.0


def test_recon_verdict_fail_beyond_30_percent():
    verdict, _, detail = _recon_verdict(100, 131)
    assert verdict == ReconVerdict.FAIL
    assert "observed=131" in detail


def test_recon_verdict_zero_expected():
    assert _recon_verdict(0, 0)[0] == ReconVerdict.PASS
    assert _recon_verdict(0, 5)[0] == ReconVerdict.FAIL


# --- record_diagnostics end-to-end -----------------------------------------


def _seed(device_id: int) -> None:
    """One device + 250 PPI (100 deep / 50 light / 20 other / 80 awake) on one
    calendar day, plus a Sleep summary that matches those counts exactly."""
    t0 = datetime(2026, 11, 1, 20, 0, 0)
    awake = [
        ActivityState.RELAXED,
        ActivityState.LOW_ACTIVITY,
        ActivityState.HIGH_ACTIVITY,
        ActivityState.EXCITED,
        ActivityState.STRESSED,
    ]
    plan = (
        [ActivityState.DEEP_SLEEP] * 100
        + [ActivityState.LIGHT_SLEEP] * 50
        + [ActivityState.OTHER_SLEEP] * 20
        + (awake * 16)[:80]
    )
    with Session(engine) as s:
        dev = Device(name="diag", mac="00:00:00:00:00:10", age=30)
        s.add(dev)
        s.commit()
        s.refresh(dev)
        device_id = int(dev.id)
        for i, act in enumerate(plan):
            s.add(
                PPI(
                    time=t0 + timedelta(seconds=i),
                    hr=66,
                    spo2=980,
                    ppi=900,
                    activity=act,
                    device_id=device_id,
                    fresh=True,
                )
            )
        s.add(
            Sleep(
                time=t0,
                timezone="00:00",
                sleep_onset_time=0,
                awake_time=80,
                light_sleep_period=50,
                deep_sleep_period=100,
                other_sleep_period=20,
                heart_rate_rest=60,
                fresh=True,
                device_id=device_id,
            )
        )
        s.commit()
    return device_id


def test_record_diagnostics_writes_all_layers_and_reconciles():
    device_id = _seed(0)
    record_diagnostics(device_id)
    with Session(engine) as s:
        layers = s.exec(
            select(LayerFreshness).where(LayerFreshness.device_id == device_id)
        ).all()
        assert len(layers) == 6
        by_layer = {r.layer: r for r in layers}
        assert by_layer[Layer.RAW_PPI].row_count == 250
        assert by_layer[Layer.RAW_PPI].latest_data_time == datetime(2026, 11, 1, 20, 4, 9)
        # CLEANED_PPI is tracked as its own layer even before clean_ppi runs.
        assert by_layer[Layer.CLEANED_PPI].row_count == 0

        checks = s.exec(
            select(Reconciliation).where(Reconciliation.device_id == device_id)
        ).all()
        assert len(checks) == 5
        assert all(c.verdict == ReconVerdict.PASS for c in checks)
        by_check = {c.check: c for c in checks}
        assert by_check["deep_sleep"].expected == 100.0
        assert by_check["deep_sleep"].observed == 100

        gaps = s.exec(
            select(DataGap).where(DataGap.device_id == device_id)
        ).all()
        assert len(gaps) == 1
        assert gaps[0].is_closed is False  # the in-progress current day


def test_record_diagnostics_is_idempotent():
    device_id = _seed(0)
    record_diagnostics(device_id)
    record_diagnostics(device_id)
    with Session(engine) as s:
        assert len(
            s.exec(select(LayerFreshness).where(LayerFreshness.device_id == device_id)).all()
        ) == 6
        assert len(
            s.exec(select(Reconciliation).where(Reconciliation.device_id == device_id)).all()
        ) == 5
        assert len(
            s.exec(select(DataGap).where(DataGap.device_id == device_id)).all()
        ) == 1
