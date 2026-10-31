from datetime import date

import pandas as pd
from sqlmodel import Session, select

from newboom.database import engine  # adjust to your session factory
from newboom.model import (
    AutonomicEpisode,
    RmssdSegment,
    TrendType,
    WindowMetric,
    WindowState,
)


def load_night_ribbon(device_id: int, night_date: date) -> dict[str, pd.DataFrame]:
    with Session(engine) as session:
        # Windows
        windows = session.exec(
            select(WindowMetric)
            .where(WindowMetric.device_id == device_id)
            .where(WindowMetric.night_date == night_date)
            .order_by(WindowMetric.window_start)
        ).all()

        # Segments
        segments = session.exec(
            select(RmssdSegment)
            .where(RmssdSegment.device_id == device_id)
            .where(RmssdSegment.night_date == night_date)
            .order_by(RmssdSegment.segment_id)
        ).all()

        # Episodes
        episodes = session.exec(
            select(AutonomicEpisode)
            .where(AutonomicEpisode.device_id == device_id)
            .where(AutonomicEpisode.night_date == night_date)
            .order_by(AutonomicEpisode.episode_id)
        ).all()

    # Convert to DataFrames
    windows_df = pd.DataFrame(
        [
            {
                "window_mid_h": w.window_mid_h,
                "ln_rmssd": w.ln_rmssd,
                "lf_hf": w.lf_hf,
                "valid_ratio": w.valid_ratio,
                "state": w.state.value if w.state else "invalid",
                "is_sleep": w.is_sleep,
            }
            for w in windows
        ]
    )

    segments_df = pd.DataFrame(
        [
            {
                "start_h": s.start_h,
                "end_h": s.end_h,
                "trend_type": s.trend_type.value,
                "lnrmssd_start": s.lnrmssd_fitted_start,
                "lnrmssd_end": s.lnrmssd_fitted_end,
                "slope": s.lnrmssd_slope_per_h,
                "volatility": s.segment_volatility,
            }
            for s in segments
        ]
    )

    episodes_df = pd.DataFrame(
        [
            {
                "start_h": e.start_h,
                "end_h": e.end_h,
                "state": e.state.value,
                "duration_min": e.duration_min,
                "lf_hf_median": e.lf_hf_median,
            }
            for e in episodes
        ]
    )

    return {
        "windows": windows_df,
        "segments": segments_df,
        "episodes": episodes_df,
    }
