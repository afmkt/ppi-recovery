from __future__ import annotations

from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# Night boundary
#
# A "night" is a bucket keyed to one local date so that a single night whose
# windows straddle midnight maps to exactly one row instead of two. The default
# (8) folds the early-morning tail (00:00–07:59) of a night back onto the date
# the night started in, so a 23:00–07:30 sleep is one row keyed to its start
# date rather than split across two. Tunable: set it to the population's latest
# typical wake hour +1. Reprocess existing data after changing it:
# WindowMetric.night_date / Sleep.night_date / DailySummary.summary_date re-key.
#
# Per-device timezone offset (e.g. "08:00") lives on Device.tz; it is not a
# global because different devices may report local time in different zones.
# ---------------------------------------------------------------------------
NIGHT_START_HOUR = 8


def night_key(dt: datetime, night_start_hour: int = NIGHT_START_HOUR) -> date:
    """Local date a night's windows/record are keyed to.

    Windows in the early morning (``hour < night_start_hour``) belong to the
    night that started the previous evening and are keyed to that previous
    date; all other windows key to their own calendar date. With the default
    (``8``) the 00:00–07:59 tail folds to the previous date; ``0`` disables the
    fold and keys every window to its own calendar date.
    """
    if dt.hour < night_start_hour:
        return dt.date() - timedelta(days=1)
    return dt.date()


# ---------------------------------------------------------------------------
# Energy store dynamics (boom.calculation.md §8)
# ---------------------------------------------------------------------------
SIGMOID_M = 125.0  # Sigmoid midpoint m
SIGMOID_S = 45.0  # Sigmoid scale s
ENERGY_K = 0.5  # Consumption coefficient k
TAU_BASE = 1.5  # Recovery time constant (hours) when F_d >= F_avg - F_std
TAU_LOW = 3.0  # Recovery time constant (hours) when F_d < F_avg - F_std
ENERGY_FLOOR = -0.1  # Internal F lower bound (retains over-draw info)
ENERGY_CEIL = 1.0  # Internal F upper bound
FIRST_ENERGY = 1.0  # E_1 at first enablement


# ---------------------------------------------------------------------------
# Training load (boom.calculation.md §7)
# ---------------------------------------------------------------------------
LOAD_THRESHOLD_FRAC = 0.45  # 45% of HRR separates S1 (non-exercise) from tss
S1_COEF = 0.6  # Non-exercise load coefficient
TSS_MALE_COEF = 0.4
TSS_MALE_EXP = 1.92
TSS_FEMALE_COEF = 0.5  # Female tss coefficient override
TSS_FEMALE_EXP = 1.64  # Female tss exponent override
TSS_DIV = 60.0  # Per-minute normalisation divisor


# ---------------------------------------------------------------------------
# Training Stress Balance / acute-chronic load
#
# TSB = ATL - CTL (acute 7-day minus chronic 42-day EMA). Positive => recent
# load above baseline. Sign is a product decision (spec does not fix it); it
# also flips the supercompensation-window interpretation.
# ---------------------------------------------------------------------------
ATL_WINDOW = 7.0
CTL_WINDOW = 42.0
ATL_DEFAULT = 37.0  # Seed when no history
CTL_DEFAULT = 54.0  # Seed when no history


# ---------------------------------------------------------------------------
# Heart-rate defaults
# ---------------------------------------------------------------------------
DEFAULT_HR_MAX = 190.0  # Fallback HR_max when no age and no observed HR
HR_MAX_FROM_AGE = 220.0  # HR_max = HR_MAX_FROM_AGE - age when age is known
DEFAULT_HR_REST = 60.0


# ---------------------------------------------------------------------------
# Heart-rate zones as HRR fractions (pending spec loadZones definitions)
# ---------------------------------------------------------------------------
ZONE_FRACTIONS: tuple[float, ...] = (0.5, 0.6, 0.7, 0.85)


# ---------------------------------------------------------------------------
# RMSSD segment trend thresholds
# ---------------------------------------------------------------------------
TREND_DELTA = 0.05
TREND_SLOPE = 0.03


# ---------------------------------------------------------------------------
# Status score (boom.calculation.md §1-§6)
#
# The base-score and sleep-score 4x4 weight grid is implemented below.
# Implemented: formal deduction/bonus (§3-§4), SpO2-dependent deltas, the
# per-hour dynamic correction (§5, process_hourly_status), and post-wake
# morning HRV (§6). Only the recovery_* fields remain pending.
# ---------------------------------------------------------------------------
STATUS_BASE = 50  # Numerator for both base and sleep score (score = 50 / weight)
STATUS_SLEEP_DEFAULT = 50  # Temp-mode sleep_score default
STATUS_LOAD_DEFAULT = 65  # UI load_score default
STATUS_TEMP_FIRST_DAY = 60  # Temporary status T_1 on the first valid day

# Non-exercise / non-sleep HR classification (boom §5 baseline; gap item 5).
# "Non-exercise" HR is ambiguous, so the definition is a switchable strategy
# that can be overridden to try variants:
#   "threshold" -- waking minute with HR < LOAD_THRESHOLD_FRAC*HRR + HR_rest
#                  and activity is not sleep (reuses the S1 load signal).
#   "activity"  -- PPI.activity in {RELAXED, LOW_ACTIVITY} (non-sport, awake).
NON_SPORT_STRATEGY = "threshold"

# 4x4 status weight grid (identical for the base-score §1 and sleep-score §2
# lookups). Rows/cols are 1-indexed severity bands 1..4 (0-indexed here);
# lower band = better = lower weight = higher score (50 / weight).
STATUS_WEIGHT_MATRIX: tuple[tuple[float, ...], ...] = (
    (1.0, 1.5, 2.0, 2.5),
    (1.5, 2.0, 2.5, 3.0),
    (2.0, 2.5, 3.0, 3.5),
    (2.5, 3.0, 3.5, 4.0),
)
