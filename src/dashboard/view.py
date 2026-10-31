import altair as alt
import pandas as pd
import streamlit as st

from dashboard.data import load_night_ribbon


def night_ribbon_chart(device_id: int, night_date):
    data = load_night_ribbon(device_id, night_date)
    windows = data["windows"]
    segments = data["segments"]
    episodes = data["episodes"]

    if windows.empty:
        st.warning("No window metrics for this night")
        return

    # ---------- Color scales ----------
    state_colors = {
        "sympathetic": "#D64545",
        "parasympathetic": "#2F80ED",
        "transition": "#A3A9B3",
        "invalid": "#E4E7EC",
    }

    trend_colors = {
        "rising": "#27AE60",
        "falling": "#E67E22",
        "stable": "#7F8C8D",
    }

    # ---------- 1. State ribbon (background) ----------
    # We turn discrete windows into a continuous ribbon by using mark_bar
    ribbon = (
        alt.Chart(windows)
        .mark_bar(opacity=0.55, size=8)  # size controls thickness
        .encode(
            x=alt.X("window_mid_h:Q", title="Hours from night start"),
            y=alt.value(0),
            y2=alt.value(1),  # fixed height band
            color=alt.Color(
                "state:N",
                scale=alt.Scale(
                    domain=list(state_colors.keys()), range=list(state_colors.values())
                ),
                legend=alt.Legend(title="Autonomic state"),
            ),
            tooltip=["window_mid_h", "state", "valid_ratio", "ln_rmssd", "lf_hf"],
        )
    )

    # ---------- 2. lnRMSSD line ----------
    lnrmssd_line = (
        alt.Chart(windows)
        .mark_line(color="#1A1A2E", strokeWidth=2)
        .encode(
            x="window_mid_h:Q",
            y=alt.Y("ln_rmssd:Q", title="ln(RMSSD)", scale=alt.Scale(zero=False)),
            tooltip=["window_mid_h", "ln_rmssd", "state"],
        )
    )

    # ---------- 3. RMSSD trend segments (overlaid lines) ----------
    # Create a long-form dataframe for the segment lines
    seg_lines = []
    for _, row in segments.iterrows():
        seg_lines.append(
            {
                "x": row.start_h,
                "y": row.lnrmssd_start,
                "trend": row.trend_type,
                "seg": id(row),
            }
        )
        seg_lines.append(
            {
                "x": row.end_h,
                "y": row.lnrmssd_end,
                "trend": row.trend_type,
                "seg": id(row),
            }
        )
    seg_df = pd.DataFrame(seg_lines)

    segment_lines = (
        alt.Chart(seg_df)
        .mark_line(strokeWidth=3.5, opacity=0.9)
        .encode(
            x="x:Q",
            y="y:Q",
            color=alt.Color(
                "trend:N",
                scale=alt.Scale(
                    domain=list(trend_colors.keys()), range=list(trend_colors.values())
                ),
                legend=alt.Legend(title="RMSSD trend"),
            ),
            detail="seg:N",  # one line per segment
            tooltip=["trend", "x", "y"],
        )
    )

    # ---------- 4. Autonomic episode blocks (optional layer) ----------
    # Shown as thin rectangles at the top
    episode_rects = (
        alt.Chart(episodes)
        .mark_rect(opacity=0.35, height=12)
        .encode(
            x="start_h:Q",
            x2="end_h:Q",
            y=alt.value(1.05),  # slightly above the ribbon
            y2=alt.value(1.12),
            color=alt.Color(
                "state:N",
                scale=alt.Scale(
                    domain=list(state_colors.keys()), range=list(state_colors.values())
                ),
                legend=None,
            ),
            tooltip=["state", "start_h", "end_h", "duration_min", "lf_hf_median"],
        )
    )

    # ---------- Compose ----------
    chart = (
        alt.layer(ribbon, lnrmssd_line, segment_lines, episode_rects)
        .resolve_scale(y="independent")  # ribbon uses 0-1, line uses real scale
        .properties(width="container", height=380, title=f"Night Ribbon — {night_date}")
        .interactive()  # zoom + pan
    )

    st.altair_chart(chart, use_container_width=True)

    # Optional: quick stats
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Windows", len(windows))
    col2.metric("Valid %", f"{windows['valid_ratio'].mean() * 100:.1f}%")
    col3.metric("Segments", len(segments))
    col4.metric("Episodes", len(episodes))
