"""Streamlit Community Cloud entry point.

Deploy with Main file path: streamlit_app.py (or swish.py — both work).
Pushes to GitHub redeploy the app after the repo is connected at share.streamlit.io.
"""
from __future__ import annotations

import runpy
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
runpy.run_path(str(_ROOT / "swish.py"), run_name="__main__")
