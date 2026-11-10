Accessed at **`/dashboard/`** when the API runs (`uvicorn newboom.api:app` /
`newboom` console script) — the marimo sub-app is mounted into the FastAPI app by
`newboom.dashboard`, alongside the API and the ingestion worker. It reads
`data/newboom.db` read-only, so it works both live and from a historical DB.

The left sidebar sets the **global time range** and selects a device. At the top
are two controls shared by *every* tab and sub-tab:

- **Now** — a full datetime picker (date + hour/minute/second), defaulting to
  wall-clock now. It is the anchor every window is measured from.
- **Time window** — a preset dropdown (`1h` / `6h` / `24h` / `48h` / `7d`, no
  "All"; the presets top out at 7d). The window is always **`[now − window,
  now]`**, resolved once and applied identically by every graph and data table,
  so the whole dashboard reads one consistent range. Set "Now" back to a past
  instant to view a historical device.

The main area has tabs (Overview / Tables / Load & Energy / Upload / API Docs).

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

- **Time window** is the *global* control in the left sidebar (shared with the
  Load & Energy tab), not a per-tab one. It resolves to the range
  **`[now − window, now]`**, so every sub-tab reads the same slice.
- Each builder also applies a **fixed internal cap** (`ORDER BY <time> DESC
  LIMIT N`) — the per-second `Cleaned PPI` table caps at `2000` rows, the
  per-minute / per-window / per-night / per-hour tables at `5000` — so a `7d`
  window never reaches the browser and no slice blows past Altair's per-chart
  row guard. There is no user-facing row-count control and no per-table
  filters; RMSSD keys its window on the night date (night-granular), and Daily
  shows every day (at most one row/device/day).

**Self-explaining empty states.** A chart that has nothing to plot does not fail
silently: the panel shows a caption stating the time window, how many data
points it holds, and *why* the chart is unavailable. The Windows (LF/HF) ribbon
is the common case — HRV is computed only for *valid* windows, so a recent
all-invalid gap leaves the chart empty even though the window is full of
windows. The caption then says "0 with LF/HF · most recent LF/HF was
`<time>` `<gap>` before this window" (or "this device has no LF/HF windows at
all"), and the table below shows the (invalid) windows so the reason is visible.

## Load & Energy tab

The reference view: three stacked panels that **share the time axis with
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
(`dashboard.metrics.hr_load_energy_chart`); a paginated table sits below. Like
the Tables sub-tabs it is driven by the **global** `now` + `time window`
controls in the left sidebar — the same window every tab shares — so no per-tab
dropdown is needed. Builders are tested in `tests/test_dashboard_tables.py`.

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

## API Docs tab

A static reference page (no day selection) that renders the mobile-app
screenshots — extracted from `docs/boom.v1.0.docx` into
`src/dashboard/resources/` — **paired with the endpoint that renders
each one**. The doc is a per-screen API reference; `dashboard.boom_docs` walks it
in document order and, for every app screenshot, grabs the HTTP request that
immediately follows it (method + URL). Inline *formula diagrams* are not app
screenshots and carry no single endpoint, so a screenshot group becomes a pair
only when a request follows it before the next heading — the nine formula PNGs
drop out. It reads the device selected in the sidebar only to fill the
`{address}` template in each link, so with no device selected the template stays
literal and the tab never demands a selection.

Each pair renders its screenshot(s) (a screen may have two) and a **clickable
link to the endpoint**. The link is **origin-relative** (`Pair.path` strips the
scheme/host/port, e.g. `/devices/{address}/go`), so a click lands on the endpoint
served by the *same instance* that serves the dashboard — `http://localhost:8000`
or `https://boom.sparkpos.cn` both resolve correctly, and every path already maps
to a real route in `newboom.boom`. The `{address}` placeholder is filled with the
MAC of the device selected in the sidebar
(`dashboard.boom_docs.fill_address`, falling back to the literal `{address}` when
none is selected), and the link opens in a **new browser tab**
(`target="_blank"` — marimo's HTML sanitizer preserves it and adds
`rel="noopener noreferrer"`) rather than navigating the dashboard away.

Both the screenshot binaries **and** the pair manifest (`pairs.json`) live in
the committed asset dir `src/dashboard/resources/`, and the tab loads everything
from there — screenshots via
`mo.image(src=dashboard.boom_docs.image_path(name))` and the pairs via
`dashboard.boom_docs.load_pairs()`. The runtime therefore **never reads the
docx**; the docx exists only to regenerate the assets. Regenerate both with
`python tools/extract_boom_images.py` (add `--prune` to drop screenshots the doc
no longer references); the tool is idempotent and opens the docx only to rebuild
`resources/`.

- `BOOM_DOCX` — the source docx for the *regeneration tool* only (default
  `docs/boom.v1.0.docx`, CWD-relative like `DATABASE_URL` / `UPLOAD_DIR`); it is
  **not** read at runtime. If `pairs.json` is missing the tab degrades to a
  "pair manifest not found" note; a missing screenshot in `resources/` shows an
  inline "missing from resources/" placeholder.

The parser (`dashboard.boom_docs`) is pure stdlib (`zipfile` + `xml.etree`, no
third-party `.docx` dependency) and is tested in `tests/test_boom_docs.py`.

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
