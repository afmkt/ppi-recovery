# PPI-Recovery Spec Verification Checklist

**Repository:** https://github.com/afmkt/ppi-recovery  
**Specs reviewed:** `docs/boom.calculation.md`, `docs/new-interface.md`  
**Date:** 2026-10-30  

Use this checklist to track resolution of deviations between the implementation and the written specifications.

---

## Critical

- [ ] **SpO2 scale mismatch**  
  Device sends raw ×10 values (`950` = 95.0%). Stored correctly as raw, but status deductions/bonuses and temporary Δ_SpO2 compare against absolute thresholds 90 / 95.  
  **Impact:** Real SpO2 values never trigger intended −5 / −10 deductions.  
  **Fix options:** Divide by 10 on ingestion for calculation fields, *or* change all thresholds to 900 / 950 and treat `0` as missing.  
  **Locations:** `_aggregate_night_spo2`, `_deduction_bonus`, `_temporary_status` in `boom_compute.py`.

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

- [ ] **Temporary algorithm – SpO2**  
  Same scale problem as formal path. Other temporary rules (first-day 60, carry previous morning score, Δ_sleep / Δ_RHR / Δ_HRV, clamp 30–90, hourly restart from morning) are largely correct.

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

- [ ] **Full autonomic episode detail**  
  Only percentages are persisted; richer start/end/duration structure from recovery_v06.py is not fully exposed to the API layer.

- [ ] **Steps / calories / some zone paths**  
  Several UI-facing aggregates have incomplete or zeroed data paths.

---

## Interface Alignment (new-interface.md)

- [x] `/upload/ppi` and `/upload/sleep` request shapes, field meanings, activity encoding (0–7), relative sleep times.  
- [x] SpO2 raw storage (correct).  
- [x] Timezone handling and night-keying (`NIGHT_START_HOUR = 8`).  
- [ ] SpO2 *calculation* scale (see Critical item).

---

## Suggested Priority Order

1. Fix SpO2 scale (Critical).  
2. Confirm product intent on formal status additive tiers.  
3. Review temporary → formal switch edge cases.  
4. Flesh out recovery_* fields and coaching logic.  
5. Decide on ATL/CTL/TSB as permanent extension vs out-of-scope.

---

*Generated from verification of implementation against `docs/boom.calculation.md` and `docs/new-interface.md`.*
