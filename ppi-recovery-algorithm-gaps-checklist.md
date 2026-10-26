# PPI-Recovery / Newboom — Algorithm Gaps Checklist & Data-Model Suggestions

**Repository:** https://github.com/afmkt/ppi-recovery  
**Primary Spec:** `docs/boom.calculation.md`  
**Main Implementation:** `src/newboom/boom_compute.py` + `config.py`  
**Generated:** 2026-10-26

This document lists the gaps between the current algorithm implementation and the specification, focused on calculation logic (APIs are largely unimplemented and out of scope). Each item includes recommended data-model changes.

---

## High Priority

### 1. Formal Status Deduction & Bonus Items (§3 / §4)

**Gap**  
Completely missing. Current status score is only `base_score + sleep_score`.

**Spec Requirements**
- Deduction (−5 / −10) for elevated RHR, low night HRV, low morning HRV, and SpO₂ thresholds.
- Bonus (+5 / +10) for the opposite conditions.

**Data-Model Suggestions**
- Add to `DailySummary` (or a dedicated `StatusAdjustment` table):
  - `rhr_delta: float | None`
  - `hrv_night_delta: float | None`
  - `hrv_morning_delta: float | None`
  - `spo2_night_avg: float | None` (or min)
  - `deduction_points: int = 0`
  - `bonus_points: int = 0`
- Prefer computing deltas on the fly from baselines + today’s metrics; persist the final adjustment totals for debugging and UI.

**Notes**  
SpO₂ support is a prerequisite for full fidelity (see item 3).

---

### 2. Hourly Dynamic Status Correction (§5 and temporary §6.3)

**Gap**  
No running status, no time-in-zone tracking, no hourly re-calculation.

**Spec Requirements**
- Dynamic adjustment based on time spent below/above baseline ± std (`Δ = 5×(t1−t2) + 10×(t3−t4)`).
- Temporary path: fixed penalties based on 5-min HR vs today’s RHR (−0 / −2 / −5).
- Always restart from the morning score (no carry-over of previous hour’s penalty).

**Data-Model Suggestions**
- New table `StatusSnapshot` (or lighter `HourlyStatus`):
  - `device_id`
  - `timestamp` (or hour bucket)
  - `morning_status: int`          # frozen morning score
  - `current_status: int`          # after dynamic adjustment
  - `t1, t2, t3, t4: float`        # hours below/above baseline ± std
  - `hr_5min_avg: float | None`
  - `is_sleep_or_exercise: bool`
- On `DailySummary`:
  - `status_morning: int | None`
  - `status_current: int | None`   # latest adjusted value
- Requires a reliable non-exercise / non-sleep HR baseline (see item 5).

---

### 3. SpO₂ Support (blocks formal + temporary paths)

**Gap**  
No night SpO₂ data anywhere in the model.

**Data-Model Suggestions**
- On `Sleep`:
  - `spo2_avg: float | None`
  - `spo2_min: float | None`
  - (optional) `spo2_time_below_95: int`  # seconds
- Alternative: store on `WindowMetric` or `MinuteMetric` if SpO₂ arrives at higher frequency.
- On `DailySummary`:
  - `spo2_night_avg: float | None`
  - `spo2_night_min: float | None`

**Notes**  
Temporary algorithm needs simple thresholds (≥95 / 90–95 / <90). Formal needs the −5/−10 rules.

---

### 4. Proper Post-Wake Morning HRV Extraction

**Gap**  
Currently approximates morning HRV as the average of the last 3 windows of the night. Spec requires detection of the sleep→wake transition plus interruption rules (duration >5 min, return to sleep, or +20 bpm HR rise in 5 s).

**Data-Model Suggestions**
- Add sleep/awake labeling:
  - Preferred: `is_sleep: bool | None` or `sleep_state` enum on `WindowMetric`.
  - Alternative: `WakeEvent` / `SleepTransition` table with confirmed wake timestamp.
- On `DailySummary` (or related):
  - `hrv_morning_start: datetime | None`
  - `hrv_morning_end: datetime | None`
  - `hrv_morning_valid: bool`
- Need short-term (≈5 s) HR for the interruption rule. Consider a downsampled HR series or on-the-fly computation from PPI.

**Notes**  
This is the largest data-model prerequisite for accurate morning HRV.

---

## Medium Priority

### 5. Non-Exercise / Non-Sleep HR Baseline (`non_SportSleepHR`)

**Gap**  
Listed in the baselines table of the spec but never computed or stored.

**Data-Model Suggestions**
- On `DailySummary`:
  - `non_sport_sleep_hr_avg: float | None`
  - `non_sport_sleep_hr_std: float | None`
- Possibly maintain a rolling version at device level.

**Notes**  
Required for the dynamic hourly correction (§5) and potentially improved load classification.

---

### 6. Energy Day-End Finalization & Incomplete-Data Handling (§8)

**Gap**  
Live energy is computed per minute, but final `energy_end_f` is only written for closed days. Missing-data rules (“do not treat missing load as zero; retain last valid energy”) are not enforced or surfaced.

**Data-Model Suggestions**
- On `DailySummary`:
  - `energy_end_f: float | None` (already exists)
  - `energy_incomplete: bool = False`
  - `last_valid_energy_ts: datetime | None`
- On `MinuteMetric` (optional):
  - `is_valid_load: bool = True`

---

### 7. Recovery / Coaching Fields

**Gap**  
Hard-coded placeholders (`recovery_sleep_percentage = 55`, static coaching text, etc.).

**Data-Model Suggestions**
- Keep existing fields on `DailySummary`:
  - `recovery_sleep_percentage`
  - `recovery_sleep_duration_min`
  - `boom_go_text`
  - `supercomp_time`
  - `training_suggestion`, `training_duration`, `training_target`
- Make them nullable and populate only when the algorithm can produce a real value.
- Optional: move free-text coaching into a separate `CoachingRecommendation` table for versioning / multi-language support later.

---

## Lower Priority / Cleanup

### 8. Load Formula Precision & HR_max Updates (§7)

- Explicit `t_i` weighting (currently implicit = 1 minute).
- Clearer “non-abnormal” HR_max update rules.
- **Data-model**: Already largely supported via `hr_max` on `DailySummary` and `Device.age`. Consider adding `hr_max_observed` and `hr_max_source` for auditability if needed.

### 9. Heart-Rate Zones

- Zone boundaries (`ZONE_FRACTIONS`) are provisional.
- **Data-model**: `heart_rate_zone` already exists on `MinuteMetric`. Make thresholds configurable or store the actual %HRR used.

### 10. ATL / CTL / TSB & Supercompensation

- Implemented as a product extension (not defined in the core boom.calculation.md).
- Fields already present on `DailySummary`. Document the sign convention (`TSB = ATL − CTL`) and seeding strategy more explicitly. Supercompensation window text is currently static.

---

## Suggested Implementation Order

1. **Data-model changes first**  
   Especially SpO₂ on `Sleep`, sleep/awake labeling on windows, and the non-exercise HR baseline. These unblock multiple algorithms.

2. Proper morning HRV extraction.

3. Formal deduction / bonus items.

4. Hourly dynamic status (depends on non-exercise baseline).

5. Energy incomplete-data handling and finalization polish.

6. Real recovery / coaching logic and remaining clean-ups.

---

## Quick Reference: Current vs Spec Coverage

| Area                        | Status                          | Notes                                      |
|----------------------------|----------------------------------|--------------------------------------------|
| Formal base score (matrix) | Implemented                     | CV-band 4×4                                |
| Formal sleep score (matrix)| Implemented                     | Duration × lateness                        |
| Formal deduction/bonus     | Missing                         | High priority                              |
| Hourly dynamic correction  | Missing                         | High priority                              |
| Temporary status core      | Mostly implemented              | SpO₂ & hourly missing                      |
| Morning HRV                | Approximate (last 3 windows)    | Needs proper wake detection                |
| Load (S1 + tss)            | Good                            | Minor precision points                     |
| Energy model (E / F / T)   | Good                            | Finalization & incomplete-data polish      |
| ATL/CTL/TSB                | Implemented (extension)         | Not in core spec                           |
| SpO₂                       | Completely missing              | Blocks status rules                        |
| Non-exercise HR baseline   | Missing                         | Blocks dynamic status                      |

---

*End of checklist.*