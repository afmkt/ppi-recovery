# PPI-Recovery Spec Verification Checklist

**Repository:** https://github.com/afmkt/ppi-recovery  
**Specs reviewed:** `docs/boom.calculation.md`, `docs/new-interface.md`  
**Date:** 2026-10-30  
**Last verified against code:** 2026-10-30 — SpO2 scale fixed, autonomic-episode persistence corrected, `training_duration` synced  

Use this checklist to track resolution of deviations between the implementation and the written specifications.

---

## Critical

- [x] **SpO2 scale mismatch** — RESOLVED 2026-10-30  
  Device sends raw ×10 values (`950` = 95.0%). Stored correctly as raw, but status deductions/bonuses and temporary Δ_SpO2 compare against absolute thresholds 90 / 95.  
  **Impact:** Real SpO2 values never trigger intended −5 / −10 deductions.  
  **Fix applied (2026-10-30):** fold the raw value to a percentage (`spo2 / 10.0`) before the 90/95 tiers in both `_deduction_bonus` and `_temporary_status` — consistent with the "store raw, divide on display" convention (the display layer already divides by 10). `0` (not reported) stays filtered upstream by `_aggregate_night_spo2`. Regression tests `test_spo2_deduction_folds_raw_x10_scale` / `test_temporary_spo2_delta_folds_raw_x10_scale` pin it (880 raw→−10, 920 raw→−5, 950/980 raw→0).  
  **Locations (fixed):** `_deduction_bonus`, `_temporary_status` in `boom_compute.py`; regression tests in `tests/test_status_contract.py`.

---

## High / Medium

- [ ] **Formal status score additive tiers**  
  Spec lists factors without an explicit “sum every independent factor” rule. Code applies worst-tier ±5/±10 *per metric* then *sums across metrics* (can reach −40/+30 before 0–100 clamp).  
  **Action:** Confirm product intent (additive vs single-tier / capped). Document decision.

- [ ] **Dynamic hourly correction baseline**  
  Spec uses non-sport/non-sleep HR baseline. Implementation prefers 7-day non-sport average with fallback to RHR, plus configurable strategy (`threshold` vs `activity`).  
  **Action:** Confirm fallback behaviour matches intended product rules.

- [ ] **HR_rest for load calculation**  
  Spec: past 7-day resting-heart-rate baseline.  
  Implementation: multi-level fallback (day rolling avg → latest closed day → sleep-derived RHR → default 60).  
  **Action:** Decide whether the richer fallback chain is acceptable or should be tightened.

- [x] **Temporary algorithm – SpO2**  
  Same scale problem as the formal path; RESOLVED 2026-10-30 via the ÷10 fold in `_temporary_status`. Other temporary rules (first-day 60, carry previous morning score, Δ_sleep / Δ_RHR / Δ_HRV, clamp 30–90, hourly restart from morning) confirmed correct.

- [ ] **Temporary → formal switch**  
  Occurs after 7 valid days *with* HRV baselines present. Edge cases around missing data should be re-checked against “continue temporary until 7 effective days” wording.

---

## Low / Informational

- [ ] **Energy model – totalCharge / totalConsume**  
  Core formulas (sigmoid, T_d, F clamp, recovery τ) match.  
  API fields `totalCharge` / `totalConsume` use a provisional mapping (morning E_d and E_d−F_d) because the calculation doc gives no formula.  
  **Action:** Confirm or replace with product-defined formulas.

- [ ] **ATL / CTL / TSB**  
  Implemented (7-day / 42-day EMA, TSB = ATL − CTL) and exposed via super-recovery endpoints.  
  **Note:** Not present in `boom.calculation.md`; appears to come from broader product / recovery.v0.6 context. Treat as intentional extension unless contradicted.

- [ ] **Readiness chart source**  
  Configurable (`hourly` max of day vs frozen daily score). Default matches “取每天最高的状态值”. No action required unless product wants the other source.

- [ ] **Morning HRV interruption rules**  
  Implementation closely follows the documented conditions (return to sleep, >5 min, or sustained +20 bpm rise). High fidelity.

- [ ] **PPI cleaning & window metrics**  
  Local-median ±5 cleaner, physiological range, valid-ratio ≥0.80, LF/HF + 9-window rolling median, RMSSD segments align with reference approach. Low risk.

---

## Incomplete / Pending Features

- [ ] **recovery_* fields**  
  Still placeholders / hard-coded (e.g. `recovery_sleep_percentage = 55`). Explicitly noted as pending in code comments.

- [ ] **Coaching / super-compensation text**  
  Static strings rather than model-derived.  
  **Action:** Implement once recovery.v0.6 / boom.v1.0 details are clarified.

- [ ] **Training suggestions, duration, target**  
  Hard-coded placeholder values on DailySummary.  
  **Synced 2026-10-30:** `training_duration` disagreed between the two upsert branches (`"60-90分钟"` ASCII hyphen vs `"60–90分钟"` en-dash); unified to the en-dash form. Still a placeholder pending the real training plan.

- [ ] **Full autonomic episode detail** — persisted, not API-exposed  
  **Corrected 2026-10-30:** full episode structure (state / start_h / end_h / duration_min / lf_hf_median) from `recovery_v06.py` *is now persisted* to the `AutonomicEpisode` table by `process_autonomic_episodes` / `build_autonomic_episodes` in `compute.py` (P1-1), beyond the per-night percentages on `DailySummary`. It is **not yet exposed via any API endpoint** (no `boom/*` route reads `AutonomicEpisode`). Action: add an endpoint only if the UI consumes the ribbon detail.

- [ ] **Steps / calories / some zone paths**  
  **Verified 2026-10-30:** `MinuteMetric.steps` / `calories` are hardcoded `0` in `_build_minute_metric`, so `load_status` sums are always 0. HR-zone paths (`_hr_zone_items`, `_load_zones`, `_hourly_hr`) *are* computed from real data. Action: wire steps/calories from a real source.

---

## Interface Alignment (new-interface.md)

- [x] `/upload/ppi` and `/upload/sleep` request shapes, field meanings, activity encoding (0–7), relative sleep times.  
- [x] SpO2 raw storage (correct).  
- [x] Timezone handling and night-keying (`NIGHT_START_HOUR = 8`).  
- [x] SpO2 *calculation* scale — RESOLVED 2026-10-30 (÷10 fold in `_deduction_bonus` / `_temporary_status`; see Critical item).

---

## Suggested Priority Order

1. ~~Fix SpO2 scale (Critical).~~ **DONE 2026-10-30** — ÷10 fold in `_deduction_bonus` / `_temporary_status` + 2 regression tests.  
2. Confirm product intent on formal status additive tiers.  
3. Review temporary → formal switch edge cases.  
4. Flesh out recovery_* fields and coaching logic.  
5. Decide on ATL/CTL/TSB as permanent extension vs out-of-scope.

---

*Generated from verification of implementation against `docs/boom.calculation.md` and `docs/new-interface.md`.*
