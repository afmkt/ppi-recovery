# pages/1_Night_Ribbon.py
from datetime import date

import streamlit as st

from dashboard.data import load_night_ribbon
from dashboard.view import night_ribbon_chart

st.title("Night Ribbon Debugger")

device_id = st.sidebar.number_input("Device ID", min_value=1, value=1)
night = st.sidebar.date_input("Night date", value=date.today())

if st.sidebar.button("Load"):
    night_ribbon_chart(device_id, night)
