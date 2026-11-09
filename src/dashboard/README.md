Accessed at **`/dashboard/`** when the API runs (`uvicorn newboom.api:app` /
`newboom` console script) — the marimo sub-app is mounted into the FastAPI app by
`newboom.dashboard`, alongside the API and the ingestion worker. It reads
`data/newboom.db` read-only, so it works both live and from a historical DB.

The left sidebar selects a device. The main area has three tabs.

## Overview tab

The per-device health/pipeline/quality view. At the top, right under the
ingestion banner, is the **Coverage & data quality** headline — the two direct
measures of the raw PPI feed (`dashboard.metrics.raw_input_quality`):

- **Coverage — received / expected**: seconds ingested against an expected
  per-second stream (`active_days × 86,400 s`). A complete day reads 100%; a
  broken feed reads near 0%. Expected is over the days the device was *active*,
  so a device that stops and resumes is not penalised for the gap between.
- **Invalid — of total received**: the received seconds the cleaner could not
  turn into a beat interval (`ppi_clean` is NULL among *processed* rows — out of
  physiological range or a local outlier the ±5 local-median check rejected; the
  inverse of the pipeline's `valid_ratio`). Not-yet-scored *fresh* seconds are
  counted as *pending* and excluded from invalid, so a device that just ingested
  a file is not misread as 100% invalid.

Both are device-wide over the whole PPI history; the per-day coverage chart lower
down (`coverage_chart`) drills completeness in per calendar day. The remaining
sections:

1. Pipeline Throughput & Task Watermarks
2. Aggregation Production (windowmetric vs minutemetric rows/day)
3. Daily Coverage & Gaps

## Tables tab

A data browser for the six derived tables, each a sub-tab (Cleaned PPI /
Windows / Minutes / RMSSD / Hourly / Daily). Every sub-tab shows a chart on top
and a
**client-side paginated table** below — `mo.ui.table` with `page_size=100`,
search and CSV/parquet download — so the UI never loads the whole table at
once.

Because the raw PPI table holds ~1M rows/device (per-second), the query is
**bounded server-side** so a sub-tab can only fetch a window of data:

- **Time window** (dropdown: `1h` / `6h` / `24h` / `7d` / `All`). For a single
  device it anchors to that device's *most recent* data time (not wall-clock
  "now"), so a historical device's `1h` is its own last hour of data.
- **Max rows** (default `2000`) hard-caps the query with `ORDER BY <time> DESC
  LIMIT N`. A capped panel notes it.
- **Per-table filters** on top: Windows offers state / sleep-only / min valid
  ratio; Cleaned PPI offers "cleaned only" and "fresh only". RMSSD (night
  segments) and Hourly (per-hour status) reuse the shared Time window / Max rows
  like the per-second tables and carry no per-table filters — RMSSD's time key is
  the night date, so its window is night-granular. Daily has no
  controls — it shows every day (at most one row/device/day).

**Self-explaining empty states.** A chart that has nothing to plot does not fail
silently: the panel shows a caption stating the time window, how many data
points it holds, and *why* the chart is unavailable. The Windows (LF/HF) ribbon
is the common case — HRV is computed only for *valid* windows, so a recent
all-invalid gap leaves the chart empty even though the window is full of
windows. The caption then says "0 with LF/HF · most recent LF/HF was
`<time>` `<gap>` before this window" (or "this device has no LF/HF windows at
all"), and the table below shows the (invalid) windows so the reason is visible.

## Load & Energy tab

The 48h reference view: three stacked panels that **share the time axis with
independent y-axes** (a single figure, three subplots), matching
`samples/test_hr_ppi_48h_20261107.png`:

- **Heart rate** — per-minute `heart_rate` plus a 5-minute smoothed series
  (the reference's 分钟均值 / 5分钟平滑).
- **Intraday cumulative load** — `total_load` (S1 + tss, the `docs/boom.calculation.md`
  §7 load), drawn **one line per night session** so the daily reset reads as a
  sawtooth break rather than a false plunge (the data already resets each night;
  per-session grouping keeps the JSON NaN-free).
- **Energy reserve** — `total_energy` (0-100), continuous: it drains under load
  and recovers overnight.

All three series live in `minutemetric`, so one bounded query
(`dashboard.metrics.hr_load_energy_table`) feeds the whole figure
(`dashboard.metrics.hr_load_energy_chart`); a paginated table sits below. The
tab carries its **own** `Time window` dropdown (default `48h` — the reference
span) because the `window_dd` / `row_cap` shared by the Tables sub-tabs are
headless (never embedded), so a figure that needs a long window can't rely on
them. Builders are tested in `tests/test_dashboard_tables.py`.

## Upload tab

Upload a device `ppi` / `sleep` JSON capture to the running server — the same
payload a device or `tools/newboom_sync.py` sends. A type dropdown (PPI / Sleep)
and a file picker (`.json`); **Upload** POSTs the file's bytes to the server's
upload endpoint and shows the result inline (rows processed, device id, saved
filename, or the error).

The tab does **not** re-implement persistence: the marimo kernel POSTs to the
very endpoint that owns the write, so request validation, the hash-named file
write, and the idempotent `(device_id, time)` upsert all happen in the API — the
UI only moves bytes. The client lives in `dashboard.uploader` (pure stdlib,
marimo-free, tested in `tests/test_dashboard_upload.py`); the UI in `vis.py`
wires the widgets.

Target and prefix are env-overridable (defaults match this in-process
deployment):

- `DASHBOARD_API_BASE` — server base URL, default `http://127.0.0.1:$PORT`
  (the in-process server; `PORT` defaults to `8000`).
- `DASHBOARD_UPLOAD_PREFIX` — default `/api/upload` (returns `ApiResponse` with a
  processed count / device id / saved filename). Set to `/upload` to use the
  `BoomMessage` endpoint instead; both persist identically.
- `DASHBOARD_UPLOAD_TIMEOUT` — per-request deadline in seconds, default `120`
  (a per-second PPI capture can be several MB).

Two caveats: a large PPI file makes the button briefly block the kernel while it
round-trips; and a **newly uploaded device** shows in the left sidebar only after
a reload — the device list is read once at connect.

## Data model

The builders live in `dashboard.metrics` and take a `query` callable
(`SQL -> DataFrame`) so they are testable without a running notebook:

| sub-tab | table | time column | chart |
| --- | --- | --- | --- |
| Cleaned PPI | `ppi` (`ppi_clean` not null) | `time` | PPI ms over time, colored by activity |
| Windows | `windowmetric` | `window_start` | LF/HF ribbon over `window_mid_h`, colored by state |
| Minutes | `minutemetric` | `timestamp` | heart rate over time, colored by HR zone |
| RMSSD | `rmssdsegment` | `night_date` | ln-RMSSD trajectory over `start_h`/`end_h`, colored by `trend_type` |
| Hourly | `hourlystatus` | `hour_start` | `current_status` vs frozen `morning_status` over time |
| Daily | `dailysummary` | `summary_date` | sympathetic/parasympathetic/transition stacked bars |

Tests for the builders, the time-window anchoring, and the empty-ribbon
explanation live in `tests/test_dashboard_tables.py`; the Upload-tab client
(`dashboard.uploader`) in `tests/test_dashboard_upload.py`.
