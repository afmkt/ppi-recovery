#!/usr/bin/env python3
"""gen_upload_data -- convert the 48h HR/PPI sample CSV into newboom upload payloads.

Reads ``samples/test_hr_ppi_48h_20261107.csv`` (columns ``timestamp,hr_bpm,ppi_ms``,
one row per second, 48h of data) and emits, per calendar day, a complete
``POST /upload/ppi`` and ``POST /upload/sleep`` body (``docs/new-interface.md``):

    ppi   -> {device, address, timezone, datas:[{time, hr, spo2, ppi, activity}]}
    sleep -> {device, address, timezone, datas:[{time, sleepOnsetTime,
              awakeTime, lightSleepPeriod, deepSleepPeriod, otherSleepPeriod,
              heartRateRest}]}

The CSV only carries hr + ppi. ``spo2`` and the per-second ``activity`` sleep-stage
curve are synthesized so the two uploads stay mutually consistent and pass the
pipeline's Q6 reconciliation (``newboom.compute.record_diagnostics``), which for
the most-recent Sleep row counts the *whole calendar day's* raw PPI activity and
compares it to the four Sleep durations:

    DEEP  = count(activity==0)      == deep_sleep_period
    LIGHT = count(activity==1)      == light_sleep_period
    OTHER = count(activity==2)      == other_sleep_period
    AWAKE = count(activity in 3..7) == awake_time
    TOTAL = every PPI row that day  == sum of the four

So for a full-day PPI (86400 s) with an 00:00-08:00 sleep window the summary
carries ``awakeTime = 57600`` and ``TOTAL = 86400`` -- NOT the 1800/25200
night-episode numbers from the new-interface.md example, which assume PPI is only
the night. (Q6-consistent choice, confirmed with the user.)

Activity layout (device status low 3 bits), same every day:
    00:00-08:00  sleep, staged 0/1/2 as a 2-cycle deep/light/REM pattern
    08:00-12:00  rest      -> 4 (low activity)
    12:00-14:00  exercise  -> 5 (high activity)
    14:00-24:00  rest      -> 4 (low activity)

spo2 is the device-raw x10 value (950 == 95.0%), synthesized per second and never
left as 0 ("0" means the device did not report spo2 that second -- new-interface.md
3.1). Values are clamped to [880, 990].

Stdlib only; no new dependencies.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from pathlib import Path

DEVICE = "BOOM-0001"
ADDRESS = "AA:BB:CC:DD:EE:FF"
TIMEZONE = "08:00"

S = 3600  # seconds per hour

# --- per-second `activity` over a 24h day ----------------------------------
# 00:00-08:00 sleep staged as a 2-cycle pattern -> light 14400, deep 7200,
# other 7200 (28800 s = 8h total).  (start_s, end_s, activity_code)
SLEEP_STAGE_BINS = [
    (0, 1800, 1),     # light  (falling asleep)   0.30 h
    (1800, 5400, 0),  # deep                     1.00 h
    (5400, 9000, 1),  # light                    1.00 h
    (9000, 12600, 0), # deep                     1.00 h
    (12600, 18000, 1),# light                    1.50 h
    (18000, 21600, 2),# other / REM              1.00 h
    (21600, 25200, 1),# light                    1.00 h
    (25200, 28800, 2),# other / REM              1.00 h
]
REST_CODE = 4
EXERCISE_CODE = 5
SLEEP_END_S = 8 * S     # 28800
REST1_END_S = 12 * S    # 43200
EXERCISE_END_S = 14 * S  # 50400
DAY_S = 24 * S          # 86400

# Sleep summary: settlement = wake (08:00) + 30 min buffer = 08:30.
SETTLE_HOUR, SETTLE_MIN = 8, 30
SETTLE_S = SETTLE_HOUR * S + SETTLE_MIN * 60  # 30600
# onset = 00:00 of the calendar day, so sleepOnsetTime = SETTLE_S.
SLEEP_ONSET_TIME = SETTLE_S  # 30600


def activity_at(sec_in_day: int) -> int:
    """Activity code for a second of the day (0..86399)."""
    if sec_in_day < SLEEP_END_S:
        for lo, hi, code in SLEEP_STAGE_BINS:
            if lo <= sec_in_day < hi:
                return code
        return 1  # unreachable: bins cover [0, 28800)
    if sec_in_day < REST1_END_S:
        return REST_CODE
    if sec_in_day < EXERCISE_END_S:
        return EXERCISE_CODE
    return REST_CODE


def sleep_windows(n: int) -> list[tuple[int, int]]:
    """Absolute row-index [start, end) of each 00:00-08:00 sleep window."""
    return [(d * DAY_S, d * DAY_S + SLEEP_END_S) for d in range((n - 1) // DAY_S + 1)]


def synth_spo2(
    hr: list[int], activity: list[int], seed: int
) -> list[int]:
    """Per-second spo2 (device-raw x10), realistic and all-filled.

    Baseline by phase (sleep 950 / rest 965 / exercise HR-depressed) + a slow
    mean-reverting random walk + per-second noise + a few sleep desaturation
    dips; clamped to [880, 990] and rounded. Deterministic in ``seed``.
    """
    n = len(hr)
    rng = random.Random(seed)

    # slow mean-reverting drift (stationary std ~ sigma/sqrt(2*theta) ~ +-4)
    theta, sigma = 0.0002, 0.08
    drift = 0.0
    base = [0.0] * n
    for i in range(n):
        drift += -theta * drift + rng.gauss(0, sigma)
        act = activity[i]
        if act < 3:
            b = 950.0            # sleep
        elif act == EXERCISE_CODE:
            b = 950.0 - max(0, hr[i] - 120) * 0.30  # HR-depressed during exercise
        else:
            b = 965.0            # awake rest
        base[i] = b + drift

    # sleep desaturation dips (4 per night): Gaussian, depth 15-35 x10, width 15-40 s
    dips = [0.0] * n
    for ws, we in sleep_windows(n):
        for _ in range(4):
            center = rng.randint(ws + 600, we - 600)
            depth = rng.uniform(15, 35)
            width = rng.uniform(15, 40)
            lo = max(ws, center - 3 * int(width))
            hi = min(we, center + 3 * int(width))
            inv = 1.0 / width
            for j in range(lo, hi):
                x = (j - center) * inv
                dips[j] = max(dips[j], depth * math.exp(-0.5 * x * x))

    out = [0] * n
    for i in range(n):
        v = base[i] - dips[i] + rng.gauss(0, 2.0)
        if v < 880.0:
            v = 880.0
        elif v > 990.0:
            v = 990.0
        out[i] = round(v)
    return out


def build_days(csv_path: str, seed: int) -> dict[str, dict]:
    """Read the CSV and build per-day PPI records + computed activity counts."""
    timestamps: list[str] = []
    hr_raw: list[float] = []
    ppi_raw: list[float] = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        next(reader)  # header
        for row in reader:
            if not row:
                continue
            timestamps.append(row[0])
            hr_raw.append(float(row[1]))
            ppi_raw.append(float(row[2]))

    hr = [round(v) for v in hr_raw]
    ppi = [round(v) for v in ppi_raw]

    # activity by time-of-day (seconds into the calendar day, parsed from the
    # fixed "YYYY-MM-DD HH:MM:SS" layout -- no per-row strptime)
    def sec_of_day(t: str) -> int:
        return int(t[11:13]) * 3600 + int(t[14:16]) * 60 + int(t[17:19])

    activity = [activity_at(sec_of_day(t)) for t in timestamps]
    spo2 = synth_spo2(hr, activity, seed)

    # group per calendar day (date string = first 10 chars of the timestamp)
    by_day: dict[str, dict] = {}
    for t in timestamps:
        day = t[:10]
        by_day.setdefault(
            day,
            {
                "records": [],
                "act_counts": {i: 0 for i in range(8)},
                "sleep_hr_sum": 0,
                "sleep_hr_n": 0,
            },
        )
    for t, h, p, s, a in zip(timestamps, hr, ppi, spo2, activity):
        d = by_day[t[:10]]
        d["records"].append(
            {"time": t, "hr": h, "spo2": s, "ppi": p, "activity": a}
        )
        d["act_counts"][a] += 1
        if a < 3:  # sleep second
            d["sleep_hr_sum"] += h
            d["sleep_hr_n"] += 1
    return by_day


def build_sleep(day: str, info: dict) -> dict:
    """Sleep summary for one calendar day, consistent with that day's PPI counts."""
    c = info["act_counts"]
    deep, light, other = c[0], c[1], c[2]
    awake = sum(c[i] for i in (3, 4, 5, 6, 7))
    assert deep + light + other + awake == DAY_S, "activity counts must fill the day"
    rhr = round(info["sleep_hr_sum"] / info["sleep_hr_n"]) if info["sleep_hr_n"] else 0
    return {
        "device": DEVICE,
        "address": ADDRESS,
        "timezone": TIMEZONE,
        "datas": [
            {
                "time": f"{day} {SETTLE_HOUR:02d}:{SETTLE_MIN:02d}:00",
                "sleepOnsetTime": SLEEP_ONSET_TIME,
                "awakeTime": awake,
                "lightSleepPeriod": light,
                "deepSleepPeriod": deep,
                "otherSleepPeriod": other,
                "heartRateRest": rhr,
            }
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", default="samples/test_hr_ppi_48h_20261107.csv")
    ap.add_argument("--out", default="samples", help="output directory")
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--pretty", action="store_true", help="pretty-print PPI files too")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    by_day = build_days(args.csv, args.seed)

    # sanity: every day must be a full 86400 s
    for day, info in by_day.items():
        got = len(info["records"])
        assert got == DAY_S, f"{day}: expected {DAY_S} PPI rows, got {got}"

    for day in sorted(by_day):
        info = by_day[day]
        c = info["act_counts"]
        ppi_payload = {
            "device": DEVICE,
            "address": ADDRESS,
            "timezone": TIMEZONE,
            "datas": info["records"],
        }
        ppi_path = out / f"ppi_{day}.json"
        with open(ppi_path, "w", encoding="utf-8") as f:
            if args.pretty:
                json.dump(ppi_payload, f, ensure_ascii=False, indent=2)
            else:
                json.dump(ppi_payload, f, ensure_ascii=False, separators=(",", ":"))

        sleep = build_sleep(day, info)
        sleep_path = out / f"sleep_{day}.json"
        with open(sleep_path, "w", encoding="utf-8") as f:
            json.dump(sleep, f, ensure_ascii=False, indent=2)

        s = sleep["datas"][0]
        spo2_vals = [r["spo2"] for r in info["records"]]
        total = sum(c.values())
        print(f"[{day}] PPI rows={len(info['records'])}")
        print("   activity 0/1/2/3/4/5/6/7 = "
              f"{c[0]}/{c[1]}/{c[2]}/{c[3]}/{c[4]}/{c[5]}/{c[6]}/{c[7]}")
        print(f"   sleep: time={s['time']}  onset={s['sleepOnsetTime']}s  "
              f"awake={s['awakeTime']}s")
        print(f"   L/D/O={s['lightSleepPeriod']}/"
              f"{s['deepSleepPeriod']}/{s['otherSleepPeriod']}  RHR={s['heartRateRest']}")
        print(f"   spo2 min={min(spo2_vals)} max={max(spo2_vals)} "
              f"zeros={spo2_vals.count(0)}  Q6 total={total}")
        print(f"   wrote {ppi_path.name} + {sleep_path.name}")


if __name__ == "__main__":
    main()
