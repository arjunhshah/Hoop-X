"""Hoop-X — Streamlit Community Cloud entry point.

Deploy with Main file path: streamlit_app.py (or swish.py — both work).
Pushes to GitHub redeploy the app after the repo is connected at share.streamlit.io.

If you still see errors mentioning streamlit_drawable_canvas or image_to_url:
- Confirm this directory's requirements.txt is what Cloud installs (repo root must not pin
  streamlit-drawable-canvas), then use "Reboot app" / clear cache on Streamlit Community Cloud.
"""
from __future__ import annotations

import runpy
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
runpy.run_path(str(_ROOT / "swish.py"), run_name="__main__")
