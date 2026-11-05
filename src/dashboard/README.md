Accessed at **`/dashboard/`** when the API runs (`uvicorn newboom.api:app` /
`newboom` console script) — the marimo sub-app is mounted into the FastAPI app by
`newboom.dashboard`, alongside the API and the ingestion worker. It reads
`data/newboom.db` read-only, so it works both live and from a historical DB.

The left sidebar selects a device. The main area has two tabs.

## Overview tab

The existing health/pipeline/quality view:

1. Ingestion Health & Pipeline Throughput
2. Aggregation Pipeline Monitoring
3. Data Quality & Statistical Sanity

## Tables tab

A data browser for the four derived tables, each a sub-tab (Cleaned PPI /
Windows / Minutes / Daily). Every sub-tab shows a chart on top and a
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
  ratio; Cleaned PPI offers "cleaned only" and "fresh only". Daily has no
  controls — it shows every day (at most one row/device/day).

**Self-explaining empty states.** A chart that has nothing to plot does not fail
silently: the panel shows a caption stating the time window, how many data
points it holds, and *why* the chart is unavailable. The Windows (LF/HF) ribbon
is the common case — HRV is computed only for *valid* windows, so a recent
all-invalid gap leaves the chart empty even though the window is full of
windows. The caption then says "0 with LF/HF · most recent LF/HF was
`<time>` `<gap>` before this window" (or "this device has no LF/HF windows at
all"), and the table below shows the (invalid) windows so the reason is visible.

## Data model

The builders live in `dashboard.metrics` and take a `query` callable
(`SQL -> DataFrame`) so they are testable without a running notebook:

| sub-tab | table | time column | chart |
| --- | --- | --- | --- |
| Cleaned PPI | `ppi` (`ppi_clean` not null) | `time` | PPI ms over time, colored by activity |
| Windows | `windowmetric` | `window_start` | LF/HF ribbon over `window_mid_h`, colored by state |
| Minutes | `minutemetric` | `timestamp` | heart rate over time, colored by HR zone |
| Daily | `dailysummary` | `summary_date` | sympathetic/parasympathetic/transition stacked bars |

Tests for the builders, the time-window anchoring, and the empty-ribbon
explanation live in `tests/test_dashboard_tables.py`.
