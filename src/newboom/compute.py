from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import numpy as np
from scipy.interpolate import interp1d
from scipy.signal import windows
from sqlmodel import Session, col, select

from newboom.database import engine
from newboom.model import (
    PPI,
    Device,
    Progress,
    RmssdSegment,
    TrendType,
    WindowMetric,
    WindowState,
)


def clean_ppi_series(ppi_list: list[float]) -> list[float | None]:
    """
    # 4. PPI 清洗
    频域和 RMSSD 都依赖高质量 PPI。每个窗口内先清洗 PPI，再判断窗口是否可用。
    valid_range_i = 350 <= ppi_i <= 2000
    m_i = median(ppi_{i-5}, ..., ppi_i, ..., ppi_{i+5})
    local_valid_i = abs(ppi_i - m_i) <= max(250, 0.25 * m_i)
    ppi_clean_i = ppi_i if valid_range_i and local_valid_i else NaN
    valid_ratio = count(valid ppi_clean_i) / count(ppi_i)
    """
    arr = np.array(ppi_list, dtype=float)
    n = len(arr)
    cleaned = np.full(n, np.nan)

    # Step 1: Physiological range mask
    valid_range_mask = (arr >= 350) & (arr <= 2000)

    for i in range(n):
        if not valid_range_mask[i]:
            continue

        # Define local window of +/- 5 elements (11 points total)
        start_idx = max(0, i - 5)
        end_idx = min(n, i + 6)
        local_slice = arr[start_idx:end_idx]

        # Compute local median
        m_i = np.median(local_slice)

        # Local valid check: abs(ppi_i - m_i) <= max(250, 0.25 * m_i)
        if abs(arr[i] - m_i) <= max(250.0, 0.25 * m_i):
            cleaned[i] = arr[i]

    # Calculate overall valid ratio for the sequence
    # valid_count = np.count_nonzero(~np.isnan(cleaned))
    # valid_ratio = valid_count / n if n > 0 else 0.0

    # Convert back to list with None for NaN values for easy database storage
    cleaned_list = [None if np.isnan(x) else x for x in cleaned]
    return cleaned_list


def all_devides() -> list[Device]:
    with Session(engine) as session:
        return list(session.exec(select(Device)).all())


def clean_ppi(all_devices: list[Device]) -> None:
    with Session(engine) as session:
        for device in all_devices:
            device_id = device.id
            fresh_ppi = session.exec(
                select(PPI)
                .where(PPI.fresh == True)
                .where(PPI.device_id == device_id)
                .order_by(col(PPI.time).asc())
                .limit(5000)
            ).all()
            if fresh_ppi:
                ppi_values = [float(p.ppi) for p in fresh_ppi]
                clean_ppi_values = clean_ppi_series(ppi_values)
                try:
                    for entry, cleaned_value in zip(fresh_ppi, clean_ppi_values):
                        entry.ppi_clean = cleaned_value
                        entry.fresh = False
                        session.add(entry)
                    session.commit()
                except Exception as e:
                    session.rollback()
                    print(f"Error cleaning PPI data for device {device_id}: {e}")
                    raise


@dataclass
class Window:
    window_start_s: float
    window_end_s: float
    ppi_clean_arr: np.ndarray  # Cleaned PPI values (containing NaNs for invalid points)

    @property
    def total_points(self) -> int:
        return len(self.ppi_clean_arr)

    @property
    def valid_count(self) -> int:
        return int(np.count_nonzero(~np.isnan(self.ppi_clean_arr)))

    @property
    def valid_ratio(self) -> float:
        if self.total_points == 0:
            return 0.0
        return self.valid_count / self.total_points

    @property
    def window_mid_h(self) -> float:
        return (self.window_start_s + self.window_end_s) / 2.0 / 3600.0

    def compute_rmssd(self) -> tuple[float | None, float | None]:
        """Calculate rmssd_ms and ln_rmssd from valid PPIs in the window."""
        valid_ppis = self.ppi_clean_arr[~np.isnan(self.ppi_clean_arr)]
        if len(valid_ppis) <= 1:
            return None, None

        diffs = np.diff(valid_ppis)
        rmssd = np.sqrt(np.mean(diffs**2))
        if rmssd <= 0:
            return None, None

        return float(rmssd), float(np.log(rmssd))

    def compute_frequency_domain(
        self,
    ) -> tuple[float | None, float | None, float | None, float | None]:
        """Interpolate to 4 Hz, apply Hann window, run FFT to get LF, HF, LF/HF, HFnu."""
        sub_ppis = self.ppi_clean_arr[~np.isnan(self.ppi_clean_arr)]

        if len(sub_ppis) < 10:
            return None, None, None, None

        try:
            # Reconstruct time axis using cumulative sum of PPIs (in seconds)
            t_i = np.cumsum(sub_ppis) / 1000.0
            t_i = t_i - t_i[0]

            fs = 4.0  # 4 Hz sampling rate
            dt = 1.0 / fs
            tau_j = np.arange(0, t_i[-1], dt)

            if len(tau_j) <= 10:
                return None, None, None, None

            f_interp = interp1d(t_i, sub_ppis, kind="linear", fill_value="extrapolate")
            rr_j = f_interp(tau_j)

            # Detrend and apply Hann window
            mean_rr = np.mean(rr_j)
            x_j = rr_j - mean_rr
            p = np.polyfit(tau_j, x_j, 1)
            trend_j = np.polyval(p, tau_j)
            x_detrend = x_j - trend_j

            w_j = windows.hann(len(x_detrend))
            xw_j = x_detrend * w_j

            # FFT computation
            X_n = np.fft.fft(xw_j)
            M = len(xw_j)
            f_n = np.fft.fftfreq(M, d=dt)
            PSD_n = (np.abs(X_n) ** 2) / (fs * np.sum(w_j**2))

            # Integrate LF (0.04 - 0.15 Hz) and HF (0.15 - 0.40 Hz)
            lf_mask = (f_n >= 0.04) & (f_n < 0.15)
            hf_mask = (f_n >= 0.15) & (f_n < 0.40)

            freq_res = fs / M
            lf = float(np.sum(PSD_n[lf_mask]) * freq_res)
            hf = float(np.sum(PSD_n[hf_mask]) * freq_res)

            if hf > 0:
                lf_hf = lf / hf
                hf_nu = hf / (lf + hf)
                return lf, hf, lf_hf, hf_nu
        except Exception as e:
            print(f"Warning: Frequency domain calculation failed: {e}")

        return None, None, None, None

    @staticmethod
    def from_ppi(
        ppi_records: list[PPI], window_duration_s: float = 300.0, step_s: float = 60.0
    ) -> list[Window]:
        if not ppi_records:
            return []

        start_time = ppi_records[0].time
        times_s = np.array(
            [(p.time - start_time).total_seconds() for p in ppi_records], dtype=float
        )
        ppi_clean_arr = np.array(
            [p.ppi_clean if p.ppi_clean is not None else np.nan for p in ppi_records],
            dtype=float,
        )

        total_duration_s = times_s[-1] - times_s[0]
        if total_duration_s < window_duration_s:
            return []

        windows_list = []
        current_start_s = 0.0

        while current_start_s + window_duration_s <= total_duration_s + step_s:
            current_end_s = current_start_s + window_duration_s
            mask = (times_s >= current_start_s) & (times_s < current_end_s)

            win_ppis = ppi_clean_arr[mask]

            windows_list.append(
                Window(
                    window_start_s=current_start_s,
                    window_end_s=current_end_s,
                    ppi_clean_arr=win_ppis,
                )
            )
            current_start_s += step_s

        return windows_list


def window_metrics_for_device(
    device_id: int, start_time: datetime, end_time: datetime
) -> list[WindowMetric]:
    def apply_rolling_smoothing_and_states(window_metrics: list[WindowMetric]) -> None:
        """
        Applies a 9-window rolling median to LF/HF and assigns the classified state
        to each WindowMetric instance in-place.
        """
        lf_hfs = np.array(
            [m.lf_hf if m.lf_hf is not None else np.nan for m in window_metrics],
            dtype=float,
        )
        smoothed_lf_hf = np.full_like(lf_hfs, np.nan)

        kernel_size = 9
        half_k = kernel_size // 2

        for i in range(len(lf_hfs)):
            start = max(0, i - half_k)
            end = min(len(lf_hfs), i + half_k + 1)
            slice_vals = lf_hfs[start:end]
            valid_slice = slice_vals[~np.isnan(slice_vals)]

            # Require at least 3 valid points in the rolling window (min_periods=3 from spec)
            if len(valid_slice) >= 3:
                smoothed_lf_hf[i] = np.median(valid_slice)

        for idx, metric in enumerate(window_metrics):
            metric.state = WindowState.from_float(smoothed_lf_hf[idx])

    def load_ppi_window(
        device_id: int, start_time: datetime, end_time: datetime
    ) -> list[PPI]:
        with Session(engine) as session:
            ppi_window = session.exec(
                select(PPI)
                .where(PPI.device_id == device_id)
                .where(PPI.time >= start_time)
                .where(PPI.time <= end_time)
                .where(PPI.fresh == False)
                .order_by(col(PPI.time).asc())
            ).all()
            return list(ppi_window)

    ppi_records = load_ppi_window(device_id, start_time, end_time)
    windows = Window.from_ppi(ppi_records)
    if not windows:
        return []

    anchor_time = ppi_records[0].time
    night_date = start_time.date()
    window_metrics = []

    for win in windows:
        window_start_dt = anchor_time + timedelta(seconds=win.window_start_s)
        window_end_dt = anchor_time + timedelta(seconds=win.window_end_s)

        rmssd_ms, ln_rmssd = None, None
        lf, hf, lf_hf, hf_nu = None, None, None, None

        # Check validity threshold before heavy math[cite: 2]
        if win.valid_ratio >= 0.80 and win.valid_count > 5:
            rmssd_ms, ln_rmssd = win.compute_rmssd()
            lf, hf, lf_hf, hf_nu = win.compute_frequency_domain()

        metric = WindowMetric(
            device_id=device_id,
            night_date=night_date,
            window_start=window_start_dt,
            window_end=window_end_dt,
            window_mid_h=win.window_mid_h,
            valid_ratio=win.valid_ratio,
            rmssd_ms=rmssd_ms,
            ln_rmssd=ln_rmssd,
            lf=lf,
            hf=hf,
            lf_hf=lf_hf,
            hf_nu=hf_nu,
            state=None,  # Assigned next during rolling smoothing
        )
        window_metrics.append(metric)

    # Apply rolling smoothing to LF/HF for state classification
    apply_rolling_smoothing_and_states(window_metrics)
    return window_metrics


def process_window_metrics(device_id: int) -> int:
    """
    Computes window metrics for a device starting from the last processed window time,
    persists them, and updates the progress tracker. Returns count of new windows processed.
    """
    with Session(engine) as session:
        # Get or initialize progress tracker for the device
        progress = session.exec(
            select(Progress).where(Progress.device_id == device_id)
        ).first()
        if not progress:
            progress = Progress(device_id=device_id, processing=True)
            session.add(progress)
            session.commit()
            session.refresh(progress)

        # Determine start time based on last processed window, or default to 7 days ago
        start_time = progress.last_window_time
        if not start_time:
            # Look at the earliest available PPI or default to 7 days ago
            earliest_ppi = session.exec(
                select(PPI)
                .where(PPI.device_id == device_id)
                .order_by(col(PPI.time).asc())
            ).first()
            start_time = earliest_ppi.time if earliest_ppi else datetime.now(UTC)

        end_time = datetime.now(UTC)

    # Build window metrics using our compute function
    metrics = window_metrics_for_device(device_id, start_time, end_time)
    if not metrics:
        return 0

    # Persist metrics and update progress in a session transaction
    with Session(engine) as session:
        for metric in metrics:
            # Use merge or upsert to handle potential duplicates on unique constraint (device_id, window_start)
            existing = session.exec(
                select(WindowMetric)
                .where(WindowMetric.device_id == device_id)
                .where(WindowMetric.window_start == metric.window_start)
            ).first()

            if existing:
                existing.valid_ratio = metric.valid_ratio
                existing.rmssd_ms = metric.rmssd_ms
                existing.ln_rmssd = metric.ln_rmssd
                existing.lf = metric.lf
                existing.hf = metric.hf
                existing.lf_hf = metric.lf_hf
                existing.hf_nu = metric.hf_nu
                existing.state = metric.state
                session.add(existing)
            else:
                session.add(metric)

        # Update progress record with the latest window end time processed
        latest_window_end = max(m.window_end for m in metrics)
        db_progress = session.exec(
            select(Progress).where(Progress.device_id == device_id)
        ).first()
        if db_progress:
            db_progress.last_window_time = latest_window_end
            session.add(db_progress)

        session.commit()

    return len(metrics)


def piecewise_linear_segments(
    device_id: int,
    night_date: date,
    time_h: np.ndarray,
    y: np.ndarray,
    min_duration_min: float = 40.0,
    max_segments: int = 4,
) -> list[RmssdSegment]:
    def segment_cost_arrays(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        n = len(x)
        sx = np.r_[0.0, np.cumsum(x)]
        sy = np.r_[0.0, np.cumsum(y)]
        sxx = np.r_[0.0, np.cumsum(x * x)]
        sxy = np.r_[0.0, np.cumsum(x * y)]
        syy = np.r_[0.0, np.cumsum(y * y)]
        cost = np.full((n + 1, n + 1), np.inf)
        for i in range(n):
            ends = np.arange(i + 2, n + 1)
            m = ends - i
            sum_x = sx[ends] - sx[i]
            sum_y = sy[ends] - sy[i]
            sum_xx = sxx[ends] - sxx[i]
            sum_xy = sxy[ends] - sxy[i]
            sum_yy = syy[ends] - syy[i]
            denom = m * sum_xx - sum_x * sum_x
            slope = np.divide(
                m * sum_xy - sum_x * sum_y,
                denom,
                out=np.zeros_like(sum_x, dtype=float),
                where=np.abs(denom) > 1e-12,
            )
            intercept = (sum_y - slope * sum_x) / m
            sse = sum_yy + slope * slope * sum_xx + m * intercept * intercept
            sse -= (
                2 * slope * sum_xy
                + 2 * intercept * sum_y
                - 2 * slope * intercept * sum_x
            )
            cost[i, ends] = np.maximum(sse, 0.0)
        return cost

    mask = np.isfinite(time_h) & np.isfinite(y)
    time_h = time_h[mask]
    y = y[mask]
    n = len(y)
    if n < 8:
        return []

    diffs_min = np.diff(time_h) * 60.0
    step_min = float(np.nanmedian(diffs_min)) if len(diffs_min) else 1.0
    if not np.isfinite(step_min) or step_min <= 0:
        step_min = 1.0
    min_pts = max(5, round(min_duration_min / step_min))
    min_pts = min(min_pts, max(5, n // 2)) if n >= 10 else max(2, n)
    max_k = max(1, min(max_segments, n // min_pts))

    cost = segment_cost_arrays(time_h, y)
    dp = np.full((max_k + 1, n + 1), np.inf)
    prev = np.full((max_k + 1, n + 1), -1, dtype=int)
    dp[0, 0] = 0.0
    for k in range(1, max_k + 1):
        min_end = k * min_pts
        for end in range(min_end, n + 1):
            starts = np.arange((k - 1) * min_pts, end - min_pts + 1)
            vals = dp[k - 1, starts] + cost[starts, end]
            best_idx = int(np.argmin(vals))
            dp[k, end] = float(vals[best_idx])
            prev[k, end] = int(starts[best_idx])

    bics = []
    for k in range(1, max_k + 1):
        sse = dp[k, n]
        if not np.isfinite(sse):
            continue
        bic = n * math.log(sse / n + 1e-9) + k * 8.0 * math.log(n)
        bics.append((bic, k))
    best_k = min(bics)[1] if bics else 1

    bounds = []
    end = n
    for k in range(best_k, 0, -1):
        start = max(0, prev[k, end])
        bounds.append((start, end))
        end = start
    bounds.reverse()

    rows = []
    for idx, (start, end) in enumerate(bounds, 1):
        xs = time_h[start:end]
        ys = y[start:end]
        slope, intercept = (
            np.polyfit(xs, ys, 1) if len(xs) >= 2 else (0.0, float(np.mean(ys)))
        )
        fitted_start = slope * xs[0] + intercept
        fitted_end = slope * xs[-1] + intercept
        delta = fitted_end - fitted_start
        duration_h = max(float(xs[-1] - xs[0]), step_min / 60.0)
        trend = TrendType.from_float(delta, slope)
        residual = ys - (slope * xs + intercept)

        rows.append(
            RmssdSegment(
                device_id=device_id,
                night_date=night_date,
                segment_id=idx,
                start_h=float(xs[0]),
                end_h=float(xs[-1]),
                duration_min=duration_h * 60.0,
                trend_type=trend,
                lnrmssd_start=float(ys[0]),
                lnrmssd_end=float(ys[-1]),
                lnrmssd_fitted_start=float(fitted_start),
                lnrmssd_fitted_end=float(fitted_end),
                lnrmssd_delta=float(delta),
                lnrmssd_slope_per_h=float(slope),
                segment_volatility=float(np.std(residual, ddof=1))
                if len(residual) > 1
                else 0.0,
                segment_sse=float(np.sum(residual * residual)),
                window_count=int(end - start),
            )
        )
    return rows


def compute_rmssd_segments_from_metrics(
    device_id: int, night_date: date, metrics: list[WindowMetric]
) -> list[RmssdSegment]:
    """
    Takes a list of WindowMetrics for a specific night, applies 9-window rolling median
    smoothing to ln_rmssd, runs the dynamic programming piecewise linear segmentation,
    and returns a list of RmssdSegment instances.
    """
    # Sort metrics chronologically just in case
    sorted_metrics = sorted(metrics, key=lambda w: w.window_start)
    if len(sorted_metrics) < 8:
        return []

    times_h = np.array([w.window_mid_h for w in sorted_metrics], dtype=float)
    ln_rmssds = np.array(
        [w.ln_rmssd if w.ln_rmssd is not None else np.nan for w in sorted_metrics],
        dtype=float,
    )

    # 9-window rolling median smoothing on lnRMSSD
    smoothed_ln_rmssd = np.full_like(ln_rmssds, np.nan)
    kernel_size = 9
    half_k = kernel_size // 2
    for i in range(len(ln_rmssds)):
        start = max(0, i - half_k)
        end = min(len(ln_rmssds), i + half_k + 1)
        slice_vals = ln_rmssds[start:end]
        valid_slice = slice_vals[~np.isnan(slice_vals)]
        if len(valid_slice) >= 3:
            smoothed_ln_rmssd[i] = np.median(valid_slice)

    # Run dynamic programming segmentation search
    segments = piecewise_linear_segments(
        device_id, night_date, times_h, smoothed_ln_rmssd
    )
    return segments


def process_rmssd_segments(device_id: int) -> int:
    """
    Checks for fresh WindowMetrics, groups them by night, generates macro-segments,
    persists RmssdSegments, and resets the fresh flag on WindowMetrics.
    """
    with Session(engine) as session:
        fresh_windows = session.exec(
            select(WindowMetric)
            .where(WindowMetric.device_id == device_id)
            .where(WindowMetric.fresh == True)
            .order_by(col(WindowMetric.window_start).asc())
        ).all()

        if not fresh_windows:
            return 0

        # Group fresh windows by night_date
        night_groups: dict[date, list[WindowMetric]] = {}
        for w in fresh_windows:
            night_groups.setdefault(w.night_date, []).append(w)

        total_segments_created = 0

        for night_date, metrics in night_groups.items():
            # Fetch ALL metrics for this night to ensure smooth rolling median calculation
            all_night_metrics = session.exec(
                select(WindowMetric)
                .where(WindowMetric.device_id == device_id)
                .where(WindowMetric.night_date == night_date)
                .order_by(col(WindowMetric.window_start).asc())
            ).all()

            # Compute segments using the pure functional helper
            segments = compute_rmssd_segments_from_metrics(
                device_id, night_date, list(all_night_metrics)
            )

            # Clear old segments for this night to avoid duplication
            existing_segs = session.exec(
                select(RmssdSegment)
                .where(RmssdSegment.device_id == device_id)
                .where(RmssdSegment.night_date == night_date)
            ).all()
            for es in existing_segs:
                session.delete(es)

            # Insert new segments
            for seg in segments:
                session.add(seg)
                total_segments_created += 1

            # Update the fresh flag for this night's processed window metrics
            for mw in metrics:
                mw.fresh = False
                session.add(mw)

        session.commit()
        return total_segments_created


def process_pipeline(all_devices: list[Device]) -> None:
    clean_ppi(all_devices)
    for device in all_devices:
        if device.id is not None:
            count = process_window_metrics(device.id)
            print(f"Processed {count} window metrics for device {device.id}")
            seg_count = process_rmssd_segments(device.id)
            if seg_count > 0:
                print(
                    f"Generated {seg_count} RMSSD macro-segments for device {device.id}"
                )
